//! Frozen rendered pages, private to one data root and expiring after five minutes.
use crate::{data::DataRoot, surface};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Record {
    pub line: String,
    pub family: String,
    pub score: f64,
    pub lane: String,
    #[serde(default)]
    pub tool: bool,
    #[serde(default)]
    pub identity: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Frozen {
    pub records: Vec<Record>,
    pub query: String,
    pub total: usize,
    pub exact: bool,
    pub shown: usize,
    pub requested: Option<usize>,
    pub chat_scoped: bool,
    #[serde(default)]
    pub full_argv: Vec<String>,
}
#[derive(Debug)]
pub struct Page {
    pub text: String,
    pub remainder: Frozen,
}
pub fn select(mut frozen: Frozen) -> Page {
    let mut text = String::new();
    let mut selected = Vec::new();
    let mut counts = HashMap::<String, usize>::new();
    let tool_cap = if frozen.records.iter().all(|r| r.tool) {
        4
    } else {
        2
    };
    let mut tools = 0;
    let mut identities = std::collections::HashSet::new();
    let first_score = frozen.records.first().map(|r| r.score).unwrap_or(0.);
    for (i, r) in frozen.records.iter().enumerate() {
        if !frozen.chat_scoped && !r.identity.is_empty() && identities.contains(&r.identity) {
            continue;
        }
        if frozen.requested.is_none() && r.tool && tools >= tool_cap {
            continue;
        }
        let count = counts.entry(r.family.clone()).or_default();
        if frozen.requested.is_none() && !frozen.chat_scoped && *count >= 3 {
            continue;
        }
        if selected.len() >= frozen.requested.filter(|n| *n > 0).unwrap_or(16).min(16) {
            break;
        }
        if frozen.requested.is_none() && selected.len() >= 4 && r.score < first_score * 0.35 {
            continue;
        }
        let identity_bytes = r.line.split_whitespace().next().map(str::len).unwrap_or(0);
        let line = surface::clip(&r.line, 848.max(identity_bytes + 100));
        if text.len() + line.len() + 1 > 3584 {
            continue;
        }
        text += &line;
        text.push('\n');
        selected.push(i);
        *count += 1;
        tools += usize::from(r.tool);
        identities.insert(r.identity.clone());
    }
    frozen.shown += selected.len();
    for i in selected.into_iter().rev() {
        frozen.records.remove(i);
    }
    Page {
        text,
        remainder: frozen,
    }
}
fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS pages(handle TEXT PRIMARY KEY,expires_ms INTEGER NOT NULL,body TEXT NOT NULL) WITHOUT ROWID;")?;
    Ok(())
}
pub fn save(root: &DataRoot, frozen: &Frozen) -> Result<Option<String>> {
    if frozen.records.is_empty() {
        return Ok(None);
    }
    if crate::data::read_only() {
        return Ok(None);
    }
    let c = Connection::open(root.path().join("pages.db"))?;
    c.busy_timeout(std::time::Duration::from_secs(5))?;
    schema(&c)?;
    let now = chrono::Utc::now().timestamp_millis();
    c.execute("DELETE FROM pages WHERE expires_ms<?", [now])?;
    let token: String = c.query_row("SELECT lower(hex(randomblob(4)))", [], |r| r.get(0))?;
    let handle = format!("m.{token}");
    c.execute(
        "INSERT INTO pages VALUES(?,?,?)",
        params![handle, now + 300_000, serde_json::to_string(frozen)?],
    )?;
    Ok(Some(handle))
}
pub fn load(root: &DataRoot, handle: &str) -> Result<Frozen> {
    if handle.len() != 10
        || !handle.starts_with("m.")
        || !handle[2..]
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
    {
        bail!("invalid continuation handle")
    }
    let c = Connection::open_with_flags(
        root.path().join("pages.db"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let exists: bool = c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='pages')",
        [],
        |r| r.get(0),
    )?;
    if !exists {
        bail!("continuation handle not found (expired, or issued in a different data directory)")
    }
    let body: Option<String> = c
        .query_row(
            "SELECT body FROM pages WHERE handle=? AND expires_ms>=?",
            params![handle, chrono::Utc::now().timestamp_millis()],
            |r| r.get(0),
        )
        .optional()?;
    let body = body.context(
        "continuation handle not found (expired, or issued in a different data directory)",
    )?;
    Ok(serde_json::from_str(&body)?)
}
pub fn footer(f: &Frozen, more: Option<&str>) -> String {
    let total = surface::number(f.total);
    let basis = if f.exact {
        format!("{total} {}", if f.total == 1 { "match" } else { "matches" })
    } else {
        format!("{total}+ matches (floor; -c exact)")
    };
    match more {
        Some(h) => format!("{basis} · more: agrep --more {h}"),
        None if !f.records.is_empty() && !f.full_argv.is_empty() => format!(
            "{basis} · full results (live rerun): {}",
            surface::shell(&f.full_argv)
        ),
        None => basis,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn page_preserves_whole_utf8_rows() {
        let f = Frozen {
            records: (0..40)
                .map(|i| Record {
                    line: format!("@session:{i} {}", "é".repeat(500)),
                    family: i.to_string(),
                    score: 1.,
                    lane: "phrase".into(),
                    tool: false,
                    identity: i.to_string(),
                })
                .collect(),
            query: "x".into(),
            total: 40,
            exact: true,
            shown: 0,
            requested: None,
            chat_scoped: false,
            full_argv: vec![],
        };
        let page = select(f);
        assert!(page.text.len() <= 3584);
        assert!(page.remainder.records.len() > 30);
        assert!(page.text.ends_with('\n'));
    }
}
