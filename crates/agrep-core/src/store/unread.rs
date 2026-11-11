//! The rows a rebuild keeps of the sources it could not read. An index refresh leaves a
//! source's rows in place when its read fails, so they stay searchable until a read
//! succeeds; a rebuild writes a new database, so it copies them from the index it replaces.
use super::sources::gone;
use super::{Changes, Stamp};
use crate::tool_store::{Text, Writer};
use anyhow::{Context, Result};
use rusqlite::types::Value;
use rusqlite::{params, params_from_iter, Connection, OptionalExtension, Transaction};
use std::collections::{HashMap, HashSet};
use std::path::Path;

/// What a rebuild read: the sources it wrote, the ones adapters retired, every failure by
/// the path it names (a source, or a directory or store above sources), the agents whose
/// collection reported one, and the census.
pub(super) struct Read<'a> {
    pub written: &'a HashSet<String>,
    pub retired: &'a HashSet<String>,
    pub errors: &'a HashMap<String, String>,
    pub troubled: &'a HashSet<&'static str>,
    pub stamps: &'a [Stamp],
}

/// A source of the replaced index, as its `sources` row holds it.
pub(super) struct Source {
    rowid: i64,
    path: String,
    agent: String,
    stamp: [Value; 4],
}

/// The sources of `old` whose rows the rebuild keeps, as an index refresh would: those it did
/// not write because their read, their stamp or their agent's store failed, and those below
/// a directory the census could not list that are not gone. A source the rebuild wrote, one
/// an adapter retired, and one whose store read cleanly without it are not kept.
pub(super) fn sources(old: &Connection, read: &Read<'_>) -> Result<Vec<Source>> {
    let failures: HashSet<&Path> = read
        .errors
        .keys()
        .map(Path::new)
        .chain(
            read.stamps
                .iter()
                .filter(|s| s.error.is_some())
                .map(|s| Path::new(&s.path)),
        )
        .collect();
    let below_failure = |path: &Path| path.ancestors().skip(1).any(|dir| failures.contains(dir));
    let unread = |s: &Source| {
        if read.written.contains(&s.path) || read.retired.contains(&s.path) {
            return false;
        }
        if read.errors.contains_key(&s.path) {
            return true;
        }
        if s.path.starts_with('@') {
            return false;
        }
        if s.path.starts_with("token:") {
            return read.troubled.contains(s.agent.as_str());
        }
        let path = Path::new(&s.path);
        match read
            .stamps
            .binary_search_by(|stamp| stamp.path.as_str().cmp(&s.path))
        {
            Ok(at) => read.stamps[at].error.is_some() || below_failure(path),
            Err(_) => below_failure(path) && !gone(path),
        }
    };
    let mut q = old.prepare(
        "SELECT rowid,path,agent,size,mtime_ns,change_token,last_error FROM sources ORDER BY rowid",
    )?;
    let mut rows = q.query([])?;
    let mut kept = Vec::new();
    while let Some(r) = rows.next()? {
        let source = Source {
            rowid: r.get(0)?,
            path: r.get(1)?,
            agent: r.get(2)?,
            stamp: [r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?],
        };
        if unread(&source) {
            kept.push(source);
        }
    }
    Ok(kept)
}

/// Copies `sources` from `old` into the rebuild in `tx` as a first build writes parsed rows:
/// each row visible and at its own turn, which the rebuild's replay then places among the
/// other copies of its chat. A chat keeps the metadata `old` has for it unless the rebuild
/// wrote it already, and `chats` gains every chat the copies hold. Parser cursors stay
/// behind, so the next read of a source parses it whole.
pub(super) fn copy(
    old: &Connection,
    tx: &Transaction<'_>,
    sources: &[Source],
    changes: &mut Changes,
    chats: &mut HashSet<(String, String)>,
) -> Result<()> {
    let mut events = Events {
        writer: Writer::default(),
        blobs: HashMap::new(),
        next: tx.query_row("SELECT coalesce(max(id),0)+1 FROM event_rows", [], |r| {
            r.get(0)
        })?,
    };
    for s in sources {
        let [size, mtime_ns, change_token, last_error] = &s.stamp;
        tx.execute(
            "INSERT INTO sources(path,agent,size,mtime_ns,change_token,last_error) VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(path) DO NOTHING",
            params![s.path, s.agent, size, mtime_ns, change_token, last_error],
        )?;
        let to: i64 = tx.query_row("SELECT rowid FROM sources WHERE path=?", [&s.path], |r| {
            r.get(0)
        })?;
        let mut held = links(old, tx, s.rowid, to)?;
        prose(old, tx, s.rowid, to, changes, &mut held)?;
        events.copy(old, tx, s.rowid, to, changes, &mut held)?;
        sessions(old, tx, &held)?;
        chats.extend(held);
    }
    Ok(())
}

/// The source's chat links and reply shapes; returns the chats it links.
fn links(
    old: &Connection,
    tx: &Transaction<'_>,
    from: i64,
    to: i64,
) -> Result<HashSet<(String, String)>> {
    let mut held = HashSet::new();
    let mut q = old.prepare_cached("SELECT agent,session FROM session_sources WHERE source=?")?;
    let mut rows = q.query([from])?;
    let mut link = tx.prepare_cached("INSERT OR IGNORE INTO session_sources VALUES(?1,?2,?3)")?;
    while let Some(r) = rows.next()? {
        let chat: (String, String) = (r.get(0)?, r.get(1)?);
        link.execute(params![chat.0, chat.1, to])?;
        held.insert(chat);
    }
    let mut q = old.prepare_cached("SELECT session,turn,body FROM reply_shapes WHERE source=?")?;
    let mut rows = q.query([from])?;
    let mut shape = tx.prepare_cached("INSERT OR IGNORE INTO reply_shapes VALUES(?1,?2,?3,?4)")?;
    while let Some(r) = rows.next()? {
        shape.execute(params![
            to,
            r.get::<_, Value>(0)?,
            r.get::<_, Value>(1)?,
            r.get::<_, Value>(2)?
        ])?;
    }
    Ok(held)
}

/// The source's turns and replies, inserted visible at their own turn as a parse inserts them.
fn prose(
    old: &Connection,
    tx: &Transaction<'_>,
    from: i64,
    to: i64,
    changes: &mut Changes,
    held: &mut HashSet<(String, String)>,
) -> Result<()> {
    const COLUMNS: usize = 11;
    let mut q = old.prepare_cached("SELECT agent,session,project,turn,who,model,model_source,ts,text,digest,extra FROM prose WHERE source=? ORDER BY id")?;
    let mut rows = q.query([from])?;
    let mut insert = tx.prepare_cached("INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?5) RETURNING id")?;
    while let Some(r) = rows.next()? {
        let values = (0..COLUMNS)
            .map(|i| r.get::<_, Value>(i))
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let id: i64 = insert.query_row(
            params_from_iter(std::iter::once(Value::Integer(to)).chain(values)),
            |r| r.get(0),
        )?;
        changes.touch("prose", id);
        held.insert((r.get(0)?, r.get(1)?));
    }
    Ok(())
}

/// The chats' rows of `old`, for those the rebuild has none of. A chat it has already
/// takes a working directory only it knows.
fn sessions(
    old: &Connection,
    tx: &Transaction<'_>,
    held: &HashSet<(String, String)>,
) -> Result<()> {
    let mut q = old.prepare_cached(
        "SELECT project,cwd,parent,family,side,title,first_ts,last_ts FROM sessions WHERE agent=?1 AND id=?2",
    )?;
    let mut insert = tx.prepare_cached("INSERT INTO sessions(agent,id,project,cwd,parent,family,side,title,first_ts,last_ts) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10) ON CONFLICT(agent,id) DO UPDATE SET cwd=excluded.cwd WHERE cwd='' AND excluded.cwd!=''")?;
    for (agent, id) in held {
        let Some(row) = q
            .query_row(params![agent, id], |r| {
                (0..8)
                    .map(|i| r.get::<_, Value>(i))
                    .collect::<rusqlite::Result<Vec<_>>>()
            })
            .optional()?
        else {
            continue;
        };
        insert.execute(params_from_iter(
            [Value::Text(agent.clone()), Value::Text(id.clone())]
                .into_iter()
                .chain(row),
        ))?;
    }
    Ok(())
}

/// Tool and control events, keyed in the rebuild by its own chat, symbol, blob and row ids.
struct Events {
    writer: Writer,
    /// The replaced index's blob ids mapped to the rebuild's.
    blobs: HashMap<i64, i64>,
    next: i64,
}
impl Events {
    /// The source's live events, inserted visible at their native turn as a parse inserts them.
    fn copy(
        &mut self,
        old: &Connection,
        tx: &Transaction<'_>,
        from: i64,
        to: i64,
        changes: &mut Changes,
        held: &mut HashSet<(String, String)>,
    ) -> Result<()> {
        let mut q = old.prepare_cached("SELECT s.agent,s.session,e.call_id,k.value,n.value,e.ts,e.native_turn,e.input_chars,e.output_chars,e.output_bytes,e.ok,e.child_session,e.meta,e.state,e.input_end,e.output_start,p.value,e.blob,m.value,e.model_source FROM event_rows e JOIN event_sessions s ON s.id=e.session_key LEFT JOIN symbols k ON k.id=e.kind_key LEFT JOIN symbols n ON n.id=e.name_key LEFT JOIN symbols p ON p.id=e.project_key LEFT JOIN symbols m ON m.id=e.model_key WHERE e.source=?1 AND e.active=1 ORDER BY e.id")?;
        let mut rows = q.query([from])?;
        while let Some(r) = rows.next()? {
            let chat: (String, String) = (r.get(0)?, r.get(1)?);
            let session = self.writer.session(tx, &chat.0, &chat.1)?;
            let mut symbol = |i: usize| -> Result<i64> {
                let value: Option<String> = r.get(i)?;
                self.writer.symbol(tx, value.as_deref().unwrap_or(""))
            };
            let (kind, name, project, model) = (symbol(3)?, symbol(4)?, symbol(16)?, symbol(18)?);
            let blob = r
                .get::<_, Option<i64>>(17)?
                .map(|blob| self.blob(old, tx, to, blob))
                .transpose()?;
            let id = self.next;
            self.next += 1;
            let value = |i: usize| r.get::<_, Value>(i);
            tx.prepare_cached("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,ok,child_session,meta,state,input_end,output_start,project_key,blob,model_key,model_source) VALUES(?1,?1,?2,?3,?4,?5,?6,?7,?8,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21)")?
                .execute(params![
                    id, to, session, value(2)?, kind, name, value(5)?, value(6)?, value(7)?,
                    value(8)?, value(9)?, value(10)?, value(11)?, value(12)?, value(13)?,
                    value(14)?, value(15)?, project, blob, model, value(19)?
                ])?;
            if blob.is_some() {
                changes.touch("tools", id);
            }
            held.insert(chat);
        }
        Ok(())
    }

    /// The rebuild's id of blob `id` of `old`, which `source` then owns.
    fn blob(
        &mut self,
        old: &Connection,
        tx: &Transaction<'_>,
        source: i64,
        id: i64,
    ) -> Result<i64> {
        if let Some(&blob) = self.blobs.get(&id) {
            self.writer.own_blob(tx, source, blob)?;
            return Ok(blob);
        }
        let (digest, ascii, body): (Vec<u8>, bool, Vec<u8>) = old
            .prepare_cached("SELECT digest,ascii,body FROM tool_blobs WHERE id=?")?
            .query_row([id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
        let digest = digest
            .try_into()
            .ok()
            .context("tool text digest is not 32 bytes")?;
        let blob = self.writer.blob(
            tx,
            source,
            Text::Encoded {
                digest,
                ascii,
                body,
            },
        )?;
        self.blobs.insert(id, blob);
        Ok(blob)
    }
}
