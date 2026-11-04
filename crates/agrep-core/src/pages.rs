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
    /// Whether `-c` can count a floor total exactly: not for a `--no-meta` page that
    /// dropped rows.
    #[serde(default)]
    pub exhaustible: bool,
}
#[derive(Debug)]
pub struct Page {
    pub text: String,
    pub remainder: Frozen,
}
/// 0.4.9's compact page budget (`compact.DEFAULT_BYTE_BUDGET`), and the rows a page shows
/// before that budget can end it (`MIN_PAGE_HITS`).
const BUDGET: usize = 3584;
const MIN_ROWS: usize = 4;
pub fn select(mut frozen: Frozen) -> Page {
    let mut selected = Vec::new();
    let mut used = 0;
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
        if selected.len() >= MIN_ROWS && used + r.line.len() + 1 > BUDGET {
            break;
        }
        if frozen.requested.is_none() && selected.len() >= MIN_ROWS && r.score < first_score * 0.35
        {
            continue;
        }
        used += r.line.len() + 1;
        selected.push(i);
        *count += 1;
        tools += usize::from(r.tool);
        identities.insert(r.identity.clone());
    }
    let lines: Vec<&str> = selected
        .iter()
        .map(|&i| frozen.records[i].line.as_str())
        .collect();
    let mut text = String::new();
    for line in fit(&lines, BUDGET) {
        text += &line;
        text.push('\n');
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
/// 0.4.9's `_fit_page_records`: rows that overrun the budget share it. A row no longer
/// than an even share keeps its length; the others split what is left and are cut at a
/// character boundary, ending in `…`.
fn fit(lines: &[&str], budget: usize) -> Vec<String> {
    let sizes: Vec<usize> = lines.iter().map(|line| line.len()).collect();
    if sizes.iter().sum::<usize>() + lines.len() <= budget {
        return lines.iter().map(|line| (*line).to_owned()).collect();
    }
    let mut available = budget.saturating_sub(lines.len());
    let mut caps = vec![0; lines.len()];
    let mut pending: std::collections::BTreeSet<usize> = (0..lines.len()).collect();
    while !pending.is_empty() {
        let (share, extra) = (available / pending.len(), available % pending.len());
        let small: Vec<usize> = pending
            .iter()
            .copied()
            .filter(|&i| sizes[i] <= share)
            .collect();
        if small.is_empty() {
            for (offset, &i) in pending.iter().enumerate() {
                caps[i] = share + usize::from(offset < extra);
            }
            break;
        }
        for i in small {
            caps[i] = sizes[i];
            available -= sizes[i];
            pending.remove(&i);
        }
    }
    lines
        .iter()
        .zip(caps)
        .map(|(line, cap)| cut(line, cap))
        .collect()
}
/// 0.4.9's `_truncate_visible_line`.
fn cut(line: &str, cap: usize) -> String {
    if line.len() <= cap {
        return line.to_owned();
    }
    let marker = if cap >= 3 {
        "…".to_owned()
    } else {
        ".".repeat(cap)
    };
    let room = cap - marker.len();
    let end = line
        .char_indices()
        .map(|(at, c)| at + c.len_utf8())
        .take_while(|&end| end <= room)
        .last()
        .unwrap_or(0);
    format!("{}{marker}", line[..end].trim_end())
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
        let lever = if f.exhaustible { "; -c exact" } else { "" };
        format!("{total}+ matches (floor{lever})")
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
    fn overrunning_rows_share_the_budget_and_short_rows_stay_whole() {
        let short = "@short:1 kept whole".to_owned();
        let long = format!("@long:2 {}", "é".repeat(2000));
        let lines = fit(&[&short, &long, &long], 3584);
        assert_eq!(lines[0], short);
        // The long rows split what the short one leaves, cut between characters.
        for line in &lines[1..] {
            assert_eq!(line.len(), 1781);
            assert!(line.ends_with("é…"));
        }
        assert!(lines.iter().map(|line| line.len() + 1).sum::<usize>() <= 3584);
        assert_eq!(fit(&[&short], 3584), [short]);
    }
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
            exhaustible: true,
        };
        let page = select(f);
        assert!(page.text.len() <= 3584);
        assert!(page.remainder.records.len() > 30);
        assert!(page.text.ends_with('\n'));
    }
}
