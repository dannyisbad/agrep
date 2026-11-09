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
    let mut q=c.prepare_cached("SELECT position,who,ts,text,source,turn FROM prose WHERE session=?1 AND agent=?2 AND visible=1 ORDER BY position,id")?;
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
/// The row a chat's latest handle claims, as 0.4.9's `_chat_latest_claims` picked it
/// (py/search.py:8624-8662): the last nonempty row by turn, then time, then insertion,
/// where a searched tool row followed every row of its turn, so a call timed after the
/// reply is the claim.
pub fn latest_claim(c: &Connection, s: &Session, tools: bool) -> Result<Option<Row>> {
    let prose = latest(c, s)?;
    if !tools {
        return Ok(prose);
    }
    let mut q = c.prepare_cached("SELECT position,ts,text,source,turn FROM tools WHERE session=?1 AND agent=?2 AND visible=1 AND length(text)>0 ORDER BY position DESC,ts DESC,id DESC LIMIT 1")?;
    let tool = q
        .query_map(params![s.id, s.agent], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get::<_, Vec<u8>>(2)?,
                r.get(3)?,
                r.get(4)?,
            ))
        })?
        .next()
        .transpose()?
        .map(|(turn, ts, text, source, source_turn)| {
            Ok::<_, anyhow::Error>(Row {
                turn,
                who: "tool".into(),
                ts,
                text: store::decode("tools", text)?,
                source,
                source_turn,
            })
        })
        .transpose()?;
    Ok(match (prose, tool) {
        (Some(p), Some(t)) if (p.turn, p.ts) > (t.turn, t.ts) => Some(p),
        (prose, tool) => tool.or(prose),
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
/// 0.4.9's `capability_error` (py/hookless/registry.py:405-425): an unknown name lists the
/// agents with `capability`, and a known agent without it gives its reason.
pub fn capability_error(
    capability: &str,
    name: &str,
    supported: &[&str],
    reasons: &[(&str, &str)],
) -> String {
    match reasons.iter().find(|(agent, _)| *agent == name) {
        Some((_, reason)) => format!("{name} has no {capability} support: {reason}"),
        None => {
            // Python's repr of the name, as 0.4.9 printed it.
            let escaped = name.replace('\\', "\\\\");
            let quoted = if name.contains('\'') && !name.contains('"') {
                format!("\"{escaped}\"")
            } else {
                format!("'{}'", escaped.replace('\'', "\\'"))
            };
            format!(
                "unknown agent {quoted}; supported for {capability}: {}",
                supported.join(", ")
            )
        }
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
pub fn resolve<'a, I>(sessions: I, reference: &str) -> Result<(Vec<&'a Session>, bool)>
where
    I: IntoIterator<Item = &'a Session>,
    I::IntoIter: Clone,
{
    let all = sessions.into_iter();
    let id = identity(reference)?;
    let exact: Vec<_> = all.clone().filter(|s| s.id == id).collect();
    if !exact.is_empty() {
        return Ok((exact, false));
    }
    let prefix: Vec<_> = all.clone().filter(|s| s.id.starts_with(&id)).collect();
    if !prefix.is_empty() {
        return Ok((prefix, false));
    }
    if id.len() >= 6 && id.bytes().all(|b| b.is_ascii_hexdigit()) {
        let v: Vec<_> = all.clone().filter(|s| s.id.contains(&id)).collect();
        if !v.is_empty() {
            return Ok((v, false));
        }
    }
    let roots: Vec<_> = all.filter(|s| !s.side).collect();
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
    let seconds = (crate::clock::now_ms() - ts).max(0) / 1000;
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
    let aliases = file_sessions(c, reference, &identity)?;
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
/// The sessions an indexed file names for `reference`: every session the file at that
/// path holds reply shapes or active tool events of, and each one whose file's own name
/// gives `identity` (`registry::session_alias`).
fn file_sessions(
    c: &Connection,
    reference: &str,
    identity: &str,
) -> Result<HashSet<(String, String)>> {
    let mut found = HashSet::new();
    let mut sources = c.prepare(
        "SELECT rowid,path,agent FROM sources WHERE path NOT LIKE '@%' AND path NOT LIKE 'token:%'",
    )?;
    // The events view's inner joins: every key names a stored session and symbol.
    let mut sessions = c.prepare("SELECT session FROM reply_shapes WHERE source=?1 UNION SELECT s.session FROM event_rows e INDEXED BY event_active_key JOIN event_sessions s ON s.id=e.session_key WHERE e.active=1 AND e.source=?1")?;
    let mut rows = sources.query([])?;
    while let Some(row) = rows.next()? {
        let path = std::path::Path::new(row.get_ref(1)?.as_str()?);
        let agent = row.get_ref(2)?.as_str()?;
        let named = crate::ingest::registry::path_eq(path, std::path::Path::new(reference));
        // An alias is the file's own id whenever that differs from the session's, so a
        // file whose name gives another id aliases none of its sessions to `identity`.
        if !named
            && crate::ingest::registry::session_alias(agent, path, "").as_deref() != Some(identity)
        {
            continue;
        }
        for session in sessions.query_map([row.get::<_, i64>(0)?], |r| r.get::<_, String>(0))? {
            let session = session?;
            if named
                || crate::ingest::registry::session_alias(agent, path, &session).as_deref()
                    == Some(identity)
            {
                found.insert((agent.to_owned(), session));
            }
        }
    }
    Ok(found)
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

    // The files a reference names, as the grouping scan over every event row found them,
    // read through the sources the reference can name.
    #[test]
    fn file_references_read_only_the_files_they_name() {
        let c = rusqlite::Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let named = crate::ingest::home()
            .join(".omp/agent/sessions/slug/2026-01-01T00-00-00-000Z_fileid.jsonl")
            .to_string_lossy()
            .into_owned();
        for (path, agent) in [
            ("/a.jsonl", "pi"),
            ("@live", "codex"),
            ("token:x", "pi"),
            ("/b.jsonl", "codex"),
            (named.as_str(), "pi"),
            ("/d", "codex"),
        ] {
            c.execute(
                "INSERT INTO sources VALUES(?1,?2,0,0,x'',NULL)",
                rusqlite::params![path, agent],
            )
            .unwrap();
        }
        c.execute_batch("INSERT INTO event_sessions VALUES(1,'pi','s1'),(2,'codex','s1'),(3,'pi','s2'),(4,'pi','fileid'),(5,'pi','gone');").unwrap();
        for (id, (source, key, active)) in [
            (1, 1, 1),
            (1, 1, 1),
            (1, 3, 1),
            (2, 2, 1),
            (3, 1, 1),
            (4, 2, 1),
            (5, 4, 1),
            (5, 5, 0),
            (5, 3, 1),
            (6, 5, 1),
        ]
        .into_iter()
        .enumerate()
        {
            c.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key,active) VALUES(?1,?1,?2,?3,?1,0,0,0,0,0,0,0,0,'','','',0,?4)", rusqlite::params![id, source, key, active]).unwrap();
        }
        for (source, session, turn) in [
            (1, "s1", 0),
            (1, "s1", 1),
            (4, "s9", 0),
            (5, "hdr", 0),
            (6, "s2", 0),
            (2, "s7", 0),
        ] {
            c.execute(
                "INSERT INTO reply_shapes VALUES(?1,?2,?3,'')",
                rusqlite::params![source, session, turn],
            )
            .unwrap();
        }
        let pairs: Vec<(String, String, String)> = c
            .prepare("SELECT src.path,src.agent,e.session FROM sources src JOIN (SELECT source,session FROM reply_shapes GROUP BY source,session UNION SELECT source,session FROM events GROUP BY source,session) e ON e.source=src.rowid WHERE src.path NOT LIKE '@%' AND src.path NOT LIKE 'token:%'")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let mut hits = 0;
        for reference in [
            "/a.jsonl", "/b.jsonl", "/d", &named, "@live", "token:x", "fileid", "hdr", "s1", "s2",
            "gone", "zzzz",
        ] {
            let identity = super::identity(reference).unwrap();
            let scanned: std::collections::HashSet<_> = pairs
                .iter()
                .filter(|(path, agent, session)| {
                    let path = std::path::Path::new(path);
                    crate::ingest::registry::path_eq(path, std::path::Path::new(reference))
                        || crate::ingest::registry::session_alias(agent, path, session).as_deref()
                            == Some(identity.as_str())
                })
                .map(|(_, agent, session)| (agent.clone(), session.clone()))
                .collect();
            hits += scanned.len();
            assert_eq!(
                super::file_sessions(&c, reference, &identity).unwrap(),
                scanned,
                "{reference}"
            );
        }
        assert_eq!(hits, 11);
        let plan: Vec<String> = c
            .prepare("EXPLAIN QUERY PLAN SELECT session FROM reply_shapes WHERE source=?1 UNION SELECT s.session FROM event_rows e INDEXED BY event_active_key JOIN event_sessions s ON s.id=e.session_key WHERE e.active=1 AND e.source=?1")
            .unwrap()
            .query_map([1], |r| r.get(3))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        assert!(
            !plan.iter().any(|step| step.starts_with("SCAN")),
            "{plan:?}"
        );
    }
}
