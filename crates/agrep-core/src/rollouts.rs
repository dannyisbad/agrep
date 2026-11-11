//! Codex rollouts a reference names by the id ending their file name. That id is what a user
//! copies, but a rollout's rows can live under another chat (a file Codex started when it
//! resumed a chat, a fork child holding only its parent's history) or nowhere at all.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::Result;
use rusqlite::Connection;

use crate::ingest::codex;
use crate::query::Inherited;

/// A rollout a reference names, and where its rows live.
#[derive(Clone, Debug, PartialEq)]
pub struct Rollout {
    /// The id its file name ends with.
    pub id: String,
    /// Its files: a plain and a compressed copy can exist at once.
    pub paths: Vec<String>,
    pub held: Held,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Held {
    /// Its rows are indexed under these chats.
    Chats(Vec<String>),
    /// It holds only history it copied from its parent: the parent's rows up to the fork,
    /// and theirs in turn, nearest first.
    Copied(Vec<Inherited>),
    /// A review it started was interrupted before its result; the review's own work is in
    /// these indexed review threads.
    Interrupted(Vec<String>),
    /// Nothing in it is searchable, for this reason.
    Empty(String),
}

/// The Codex rollouts whose file-name id is or starts with `reference`'s id, one per id,
/// when no indexed chat's id does.
pub fn named(c: &Connection, reference: &str) -> Result<Vec<Rollout>> {
    let Ok(id) = crate::history::identity(reference) else {
        return Ok(Vec::new());
    };
    let chat: bool = c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sessions WHERE substr(id,1,length(?1))=?1)",
        [&id],
        |r| r.get(0),
    )?;
    if id.is_empty() || chat {
        return Ok(Vec::new());
    }
    let mut files: BTreeMap<String, Vec<(i64, String)>> = BTreeMap::new();
    let mut sources = c.prepare("SELECT rowid,path FROM sources WHERE agent='codex'")?;
    for row in sources.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))? {
        let (source, path) = row?;
        if let Some(file) = codex::file_id(Path::new(&path)).filter(|file| file.starts_with(&id)) {
            files
                .entry(file.to_owned())
                .or_default()
                .push((source, path));
        }
    }
    files
        .into_iter()
        .map(|(id, sources)| {
            let held = held(c, &id, &sources)?;
            let paths = sources.into_iter().map(|(_, path)| path).collect();
            Ok(Rollout { id, paths, held })
        })
        .collect()
}

fn held(c: &Connection, id: &str, sources: &[(i64, String)]) -> Result<Held> {
    // The events view's inner joins: every key names a stored session.
    let mut rows = c.prepare_cached("SELECT session FROM prose WHERE source=?1 UNION SELECT s.session FROM event_rows e INDEXED BY event_active_key JOIN event_sessions s ON s.id=e.session_key WHERE e.active=1 AND e.source=?1")?;
    let mut chats = BTreeSet::new();
    for (source, _) in sources {
        for session in rows.query_map([source], |r| r.get::<_, String>(0))? {
            chats.insert(session?);
        }
    }
    if !chats.is_empty() {
        return Ok(Held::Chats(chats.into_iter().collect()));
    }
    let Some(path) = sources
        .iter()
        .map(|(_, path)| Path::new(path))
        .find(|path| path.is_file())
    else {
        return Ok(Held::Empty("its file is gone".into()));
    };
    if let Some(chain) = copied(c, path)? {
        return Ok(Held::Copied(chain));
    }
    let Some(holdings) = codex::holdings(path) else {
        return Ok(Held::Empty("its file cannot be read".into()));
    };
    if holdings.review && holdings.replies == 0 {
        let threads = c
            .prepare_cached(
                "SELECT id FROM sessions WHERE agent='codex' AND parent=?1 ORDER BY id",
            )?
            .query_map([id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        return Ok(Held::Interrupted(threads));
    }
    Ok(Held::Empty(if holdings.replies + holdings.calls == 0 {
        "nothing in it was typed, answered or run".into()
    } else {
        format!(
            "its {} replies and {} tool calls are not in the index - agrep audit --full checks it",
            holdings.replies, holdings.calls
        )
    }))
}

/// For a fork child whose parent is indexed: the history it copied, the parent's rows up to
/// the fork and the history the parent inherited in turn.
fn copied(c: &Connection, path: &Path) -> Result<Option<Vec<Inherited>>> {
    let Some((parent, forked)) = codex::fork_origin(path) else {
        return Ok(None);
    };
    let indexed: bool = c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sessions WHERE agent='codex' AND id=?1)",
        [&parent],
        |r| r.get(0),
    )?;
    if !indexed {
        return Ok(None);
    }
    let inherited = crate::forks::inherited(c, &parent)?;
    let mut chain = vec![Inherited {
        session: parent,
        until: forked,
    }];
    chain.extend(inherited.into_iter().map(|link| Inherited {
        until: link.until.min(forked),
        ..link
    }));
    Ok(Some(chain))
}
