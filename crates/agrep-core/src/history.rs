//! Read-only session navigation shared by browsing, briefings and diagnostics.
use crate::{query::Filters, store};
use anyhow::{bail, Result};
use rusqlite::{params, Connection};
use serde::Serialize;
use std::collections::HashSet;

#[derive(Clone, Debug, Serialize)]
pub struct Session {
    pub agent: String,
    #[serde(rename = "session")]
    pub id: String,
    pub project: String,
    pub cwd: String,
    pub parent: String,
    pub family: String,
    pub side: bool,
    #[serde(rename = "first_text")]
    pub title: String,
    pub first_ts: i64,
    pub last_ts: i64,
}
#[derive(Clone, Debug)]
pub struct Row {
    pub turn: i64,
    pub who: String,
    pub ts: i64,
    pub text: String,
    pub source: i64,
    pub source_turn: i64,
}
pub fn sessions(c: &Connection) -> Result<Vec<Session>> {
    let mut q = c.prepare("SELECT agent,id,project,cwd,parent,family,side,title,first_ts,last_ts FROM sessions ORDER BY last_ts DESC,agent,id")?;
    let mut result: Vec<Session> = q
        .query_map([], |r| {
            Ok(Session {
                agent: r.get(0)?,
                id: r.get(1)?,
                project: r.get(2)?,
                cwd: r.get(3)?,
                parent: r.get(4)?,
                family: r.get(5)?,
                side: r.get(6)?,
                title: r.get(7)?,
                first_ts: r.get(8)?,
                last_ts: r.get(9)?,
            })
        })?
        .collect::<rusqlite::Result<_>>()?;
    let by_id: std::collections::HashMap<_, _> = result
        .iter()
        .enumerate()
        .map(|(index, s)| ((s.agent.as_str(), s.id.as_str()), index))
        .collect();
    let families: Vec<_> = result
        .iter()
        .map(|session| {
            let mut current = session;
            let mut seen = HashSet::new();
            while current.side && !current.parent.is_empty() {
                if !seen.insert(current.id.as_str()) {
                    break;
                }
                let Some(&index) = by_id.get(&(current.agent.as_str(), current.parent.as_str()))
                else {
                    break;
                };
                current = &result[index];
            }
            if current.side {
                session.family.clone()
            } else {
                current.id.clone()
            }
        })
        .collect();
    for (session, family) in result.iter_mut().zip(families) {
        session.family = family;
    }
    Ok(result)
}
pub fn rows(c: &Connection, s: &Session) -> Result<Vec<Row>> {
    let mut q=c.prepare("SELECT position,who,ts,text,source,turn FROM prose WHERE session=?1 AND agent=?2 AND visible=1 ORDER BY position,id")?;
    let raw = q
        .query_map(params![s.id, s.agent], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<(i64, String, i64, Vec<u8>, i64, i64)>>>()?;
    raw.into_iter()
        .map(|(turn, who, ts, text, source, source_turn)| {
            Ok(Row {
                turn,
                who,
                ts,
                text: store::decode("prose", text)?,
                source,
                source_turn,
            })
        })
        .collect()
}
pub fn latest(c: &Connection, s: &Session) -> Result<Option<Row>> {
    let mut q=c.prepare("SELECT position,who,ts,text,source,turn FROM prose WHERE session=?1 AND agent=?2 AND visible=1 AND length(text)>0 ORDER BY position DESC,ts DESC,id DESC LIMIT 1")?;
    let mut r = q.query(params![s.id, s.agent])?;
    Ok(if let Some(r) = r.next()? {
        Some(Row {
            turn: r.get(0)?,
            who: r.get(1)?,
            ts: r.get(2)?,
            text: store::decode("prose", r.get(3)?)?,
            source: r.get(4)?,
            source_turn: r.get(5)?,
        })
    } else {
        None
    })
}
pub fn turn_count(c: &Connection, s: &Session) -> Result<i64> {
    Ok(c.query_row(
        "SELECT count(DISTINCT position) FROM prose WHERE session=?1 AND agent=?2 AND visible=1",
        params![s.id, s.agent],
        |r| r.get(0),
    )?)
}
pub fn project_matches(label: &str, query: &str) -> bool {
    let label = label.replace('\\', "/").to_lowercase();
    let query = query.replace('\\', "/").to_lowercase();
    let leaf = label.rsplit('/').next().unwrap_or(&label);
    if query.contains(['*', '?', '[']) {
        globset::GlobBuilder::new(&query)
            .case_insensitive(true)
            .build()
            .is_ok_and(|g| {
                let g = g.compile_matcher();
                g.is_match(&label) || g.is_match(leaf)
            })
    } else {
        label == query || leaf == query
    }
}
pub fn in_scope(s: &Session, f: &Filters) -> bool {
    f.agent
        .as_ref()
        .is_none_or(|a| a.split(',').any(|a| normalize_agent(a) == s.agent))
        && f.project
            .as_ref()
            .is_none_or(|p| project_matches(&s.project, p))
        && f.exclude_project
            .as_ref()
            .is_none_or(|p| !project_matches(&s.project, p))
        && f.since.is_none_or(|t| s.last_ts >= t)
        && f.until.is_none_or(|t| s.first_ts < t)
        && (!f.no_side || !s.side)
}
pub fn normalize_agent(a: &str) -> &str {
    match a {
        "agy" => "antigravity",
        "omp" | "oh-my-pi" => "pi",
        _ => a,
    }
}
pub fn target(id: &str) -> String {
    format!("@{}", crate::handles::full_target(id))
}
pub fn handle(id: &str, row: &Row) -> String {
    crate::handles::mint_full(id, row.turn, &row.text)
}
pub fn identity(reference: &str) -> Result<String> {
    if crate::handles::is_handle(reference) {
        return Ok(crate::handles::parse(reference)?.session);
    }
    crate::handles::normalize(reference)
}
pub fn resolve<'a>(all: &'a [Session], reference: &str) -> Result<(Vec<&'a Session>, bool)> {
    let id = identity(reference)?;
    let exact: Vec<_> = all.iter().filter(|s| s.id == id).collect();
    if !exact.is_empty() {
        return Ok((exact, false));
    }
    let prefix: Vec<_> = all.iter().filter(|s| s.id.starts_with(&id)).collect();
    if !prefix.is_empty() {
        return Ok((prefix, false));
    }
    if id.len() >= 6 && id.bytes().all(|b| b.is_ascii_hexdigit()) {
        let v: Vec<_> = all.iter().filter(|s| s.id.contains(&id)).collect();
        if !v.is_empty() {
            return Ok((v, false));
        }
    }
    let roots: Vec<_> = all.iter().filter(|s| !s.side).collect();
    let project: Vec<_> = roots
        .iter()
        .copied()
        .filter(|s| project_matches(&s.project, reference))
        .collect();
    if !project.is_empty() {
        return Ok((project, true));
    }
    let needle = reference.to_lowercase();
    Ok((
        roots
            .into_iter()
            .filter(|s| s.title.to_lowercase().contains(&needle))
            .collect(),
        true,
    ))
}
pub fn root<'a>(all: &'a [Session], mut s: &'a Session) -> Result<&'a Session> {
    let mut seen = HashSet::new();
    while s.side {
        if !seen.insert((&s.agent, &s.id)) {
            bail!("side chat parent chain contains a cycle")
        };
        s = all
            .iter()
            .find(|p| p.agent == s.agent && p.id == s.parent)
            .ok_or_else(|| anyhow::anyhow!("side chat parent is not indexed: {}", s.parent))?;
    }
    Ok(s)
}
pub fn one_line(s: &str, max: usize) -> String {
    s.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(max)
        .collect()
}
pub fn age(ts: i64) -> String {
    let seconds = (chrono::Utc::now().timestamp_millis() - ts).max(0) / 1000;
    match seconds {
        0..60 => "now".into(),
        60..3600 => format!("{}m", seconds / 60),
        3600..86400 => format!("{}h", seconds / 3600),
        _ => format!("{}d", seconds / 86400),
    }
}

pub fn project_leaf(project: &str) -> String {
    project
        .replace('\\', "/")
        .trim_end_matches('/')
        .rsplit('/')
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or("-")
        .to_string()
}
pub fn validate_filters(filters: &Filters) -> Result<()> {
    for (name, value) in [
        ("agent", &filters.agent),
        ("project", &filters.project),
        ("exclude-project", &filters.exclude_project),
    ] {
        if value.as_ref().is_some_and(|s| s.trim().is_empty()) {
            bail!("--{name} must not be empty")
        }
    }
    if let Some(agent) = &filters.agent {
        for a in agent.split(',') {
            if !crate::ingest::registry::ADAPTERS
                .iter()
                .any(|candidate| candidate.name() == normalize_agent(a))
            {
                bail!("unknown agent: {a}")
            }
        }
    }
    Ok(())
}

pub fn resolve_indexed<'a>(
    c: &Connection,
    all: &'a [Session],
    reference: &str,
) -> Result<(Vec<&'a Session>, bool)> {
    let matched = resolve(all, reference)?;
    if !matched.1 {
        return Ok(matched);
    }
    let identity = identity(reference)?;
    let mut q = c.prepare("SELECT src.path,src.agent,e.session FROM sources src JOIN (SELECT source,session FROM reply_shapes GROUP BY source,session UNION SELECT source,session FROM events GROUP BY source,session) e ON e.source=src.rowid WHERE src.path NOT LIKE '@%' AND src.path NOT LIKE 'token:%'")?;
    let mut aliases = HashSet::new();
    for row in q.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
        ))
    })? {
        let (path, agent, session) = row?;
        if crate::ingest::registry::path_eq(
            std::path::Path::new(&path),
            std::path::Path::new(reference),
        ) || crate::ingest::registry::session_alias(
            &agent,
            std::path::Path::new(&path),
            &session,
        )
        .as_deref()
            == Some(identity.as_str())
        {
            aliases.insert((agent, session));
        }
    }
    let found: Vec<_> = all
        .iter()
        .filter(|s| aliases.contains(&(s.agent.clone(), s.id.clone())))
        .collect();
    if found.is_empty() {
        Ok(matched)
    } else {
        Ok((found, false))
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn project_filters_accept_both_separator_spellings() {
        for label in [r"C:\Work\Project", "C:/Work/Project"] {
            for query in [
                r"c:\work\project",
                "c:/work/project",
                r"C:\Work\*",
                "project",
            ] {
                assert!(super::project_matches(label, query), "{label}: {query}");
            }
            assert!(!super::project_matches(label, r"C:\Other\Project"));
        }
    }
}
