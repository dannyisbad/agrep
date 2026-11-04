//! Integer-keyed events and shared immutable tool text.
//! The two read views retain the public row columns used by command readers.
use crate::model::Event;
use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};

// Candidate streams already carry IDs or a block range. Keep that access path
// ahead of broad agent/session predicates, and do not join stored text here.
pub(crate) const CANDIDATES_BY_ID: &str = "(SELECT e.tool_id AS id,s.agent,s.session,p.value AS project,'tool' AS who,m.value AS model,e.ts,e.position,e.visible,e.blob FROM event_rows e INDEXED BY event_tool_id CROSS JOIN event_sessions s ON s.id=e.session_key LEFT JOIN symbols p ON p.id=e.project_key LEFT JOIN symbols m ON m.id=e.model_key WHERE e.active=1 AND e.blob IS NOT NULL)";

pub(crate) fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS event_sessions(id INTEGER PRIMARY KEY,agent TEXT NOT NULL,session TEXT NOT NULL,UNIQUE(agent,session));
CREATE INDEX IF NOT EXISTS event_sessions_session ON event_sessions(session);
CREATE TABLE IF NOT EXISTS symbols(id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE);
INSERT OR IGNORE INTO symbols VALUES(0,'');
CREATE TABLE IF NOT EXISTS tool_blobs(id INTEGER PRIMARY KEY,digest BLOB NOT NULL UNIQUE,ascii INTEGER NOT NULL DEFAULT 0,body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS tool_blob_sources(source INTEGER NOT NULL,blob INTEGER NOT NULL,PRIMARY KEY(source,blob)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tool_blob_sources_blob ON tool_blob_sources(blob);
CREATE TABLE IF NOT EXISTS blob_gc(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS event_rows(id INTEGER PRIMARY KEY,tool_id INTEGER NOT NULL,source INTEGER NOT NULL,session_key INTEGER NOT NULL,call_id TEXT NOT NULL,kind_key INTEGER NOT NULL,name_key INTEGER NOT NULL,ts INTEGER NOT NULL,native_turn INTEGER NOT NULL,position INTEGER NOT NULL,input_chars INTEGER NOT NULL,output_chars INTEGER NOT NULL,output_bytes INTEGER NOT NULL,ok INTEGER,child_session TEXT NOT NULL,meta TEXT NOT NULL,state TEXT NOT NULL,input_end INTEGER,output_start INTEGER,project_key INTEGER NOT NULL,blob INTEGER,model_key INTEGER NOT NULL DEFAULT 0,model_source TEXT NOT NULL DEFAULT 'unknown',visible INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS event_active_key ON event_rows(source,session_key,call_id) WHERE active=1;
CREATE UNIQUE INDEX IF NOT EXISTS event_tool_id ON event_rows(tool_id) WHERE active=1;
CREATE VIEW IF NOT EXISTS tools AS SELECT e.tool_id AS id,e.source,s.agent,s.session,p.value AS project,e.native_turn AS turn,'tool' AS who,m.value AS model,e.model_source,e.ts,b.body AS text,lower(hex(b.digest)) AS digest,e.call_id AS extra,e.position,e.visible FROM event_rows e JOIN event_sessions s ON s.id=e.session_key LEFT JOIN symbols p ON p.id=e.project_key LEFT JOIN symbols m ON m.id=e.model_key LEFT JOIN tool_blobs b ON b.id=e.blob WHERE e.blob IS NOT NULL AND e.active=1;
CREATE VIEW IF NOT EXISTS events AS SELECT e.source,s.session,e.call_id,json_object('kind',k.value,'name',n.value,'input_chars',e.input_chars,'output_chars',e.output_chars,'output_bytes',e.output_bytes,'ok',CASE WHEN e.ok IS NULL THEN NULL ELSE json(CASE WHEN e.ok THEN 'true' ELSE 'false' END) END,'call_id',e.call_id,'child_session',e.child_session,'meta',e.meta,'state',e.state,'ts',e.ts,'native_turn',e.native_turn,'visible',e.visible,'turn',e.position,'position',e.position,'input_end',e.input_end,'output_start',e.output_start) AS body,e.id,e.ts,e.position,e.visible,b.body AS text FROM event_rows e JOIN event_sessions s ON s.id=e.session_key JOIN symbols k ON k.id=e.kind_key JOIN symbols n ON n.id=e.name_key LEFT JOIN tool_blobs b ON b.id=e.blob WHERE e.active=1;
CREATE TRIGGER IF NOT EXISTS tools_delete INSTEAD OF DELETE ON tools BEGIN UPDATE event_rows SET active=0 WHERE tool_id=old.id AND active=1; END;
CREATE TRIGGER IF NOT EXISTS tools_visibility INSTEAD OF UPDATE OF visible,position ON tools BEGIN UPDATE event_rows SET visible=new.visible,position=new.position WHERE tool_id=old.id AND active=1; END;
CREATE TRIGGER IF NOT EXISTS events_delete INSTEAD OF DELETE ON events BEGIN UPDATE event_rows SET active=0 WHERE source=old.source AND session_key IN (SELECT id FROM event_sessions WHERE session=old.session) AND call_id=old.call_id AND active=1; END;
CREATE TRIGGER IF NOT EXISTS events_update INSTEAD OF UPDATE OF body ON events BEGIN UPDATE event_rows SET position=json_extract(new.body,'$.position'),meta=coalesce(json_extract(new.body,'$.meta'),'') WHERE source=old.source AND session_key IN (SELECT id FROM event_sessions WHERE session=old.session) AND call_id=old.call_id AND active=1; END;
CREATE TRIGGER IF NOT EXISTS tools_count_insert AFTER INSERT ON event_rows WHEN new.blob IS NOT NULL AND new.visible=1 AND new.active=1 BEGIN INSERT INTO state(key,value) VALUES('tools_count',1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; END;
CREATE TRIGGER IF NOT EXISTS tools_count_delete AFTER DELETE ON event_rows WHEN old.blob IS NOT NULL AND old.visible=1 AND old.active=1 BEGIN UPDATE state SET value=CAST(value AS INTEGER)-1 WHERE key='tools_count'; END;
CREATE TRIGGER IF NOT EXISTS tools_count_visibility AFTER UPDATE OF blob,visible,active ON event_rows WHEN (old.blob IS NOT NULL AND old.visible=1 AND old.active=1)!=(new.blob IS NOT NULL AND new.visible=1 AND new.active=1) BEGIN UPDATE state SET value=CAST(value AS INTEGER)+(new.blob IS NOT NULL AND new.visible=1 AND new.active=1)-(old.blob IS NOT NULL AND old.visible=1 AND old.active=1) WHERE key='tools_count'; END;
")?;
    indexes(c, true)
}

pub(crate) fn release_source(tx: &Transaction<'_>, source: i64) -> Result<()> {
    tx.execute(
        "INSERT OR IGNORE INTO blob_gc SELECT blob FROM tool_blob_sources WHERE source=?",
        [source],
    )?;
    tx.execute("DELETE FROM tool_blob_sources WHERE source=?", [source])?;
    Ok(())
}
pub(crate) fn collect(tx: &Transaction<'_>) -> Result<()> {
    tx.execute("DELETE FROM tool_blobs WHERE id IN (SELECT id FROM blob_gc) AND NOT EXISTS(SELECT 1 FROM tool_blob_sources WHERE blob=tool_blobs.id) AND NOT EXISTS(SELECT 1 FROM event_rows WHERE blob=tool_blobs.id)", [])?;
    tx.execute("DELETE FROM blob_gc", [])?;
    Ok(())
}

pub(crate) fn indexes(c: &Connection, create: bool) -> Result<()> {
    for (name, columns, predicate) in [
        ("event_session", "session_key,native_turn", ""),
        ("event_position", "session_key,position,ts", ""),
        ("event_time", "ts DESC", ""),
        ("event_project", "project_key,ts DESC", ""),
        ("event_native_turn", "source,session_key,native_turn", ""),
        ("event_blob", "blob", " WHERE blob IS NOT NULL"),
    ] {
        c.execute_batch(&if create {
            format!("CREATE INDEX IF NOT EXISTS {name} ON event_rows({columns}){predicate};")
        } else {
            format!("DROP INDEX IF EXISTS {name};")
        })?;
    }
    Ok(())
}

pub(crate) fn encode(text: &str) -> Result<Vec<u8>> {
    let mut fast = vec![0];
    fast.extend(lz4_flex::compress_prepend_size(text.as_bytes()));
    if text.len() < 128 {
        return Ok(fast);
    }
    let dense = crate::compression::deflate(&[1], text.as_bytes())?;
    Ok(if dense.len() + 16 < fast.len() {
        dense
    } else {
        fast
    })
}

pub(crate) fn decode(bytes: &[u8]) -> Result<String> {
    let (&codec, body) = bytes.split_first().context("missing tool text codec")?;
    let out = match codec {
        0 => lz4_flex::decompress_size_prepended(body)?,
        1 => crate::compression::inflate(body)?,
        _ => anyhow::bail!("unknown tool text codec"),
    };
    Ok(String::from_utf8(out)?)
}

#[derive(Default)]
pub(crate) struct Writer {
    symbols: HashMap<String, i64>,
    sessions: HashMap<(String, String), i64>,
    blobs: HashMap<[u8; 32], i64>,
    owners: HashSet<(i64, i64)>,
    next_event: Option<i64>,
}
pub(crate) struct Changed {
    pub id: i64,
    pub old_id: Option<i64>,
    pub text_changed: bool,
    pub old_visible: bool,
    pub has_text: bool,
    pub dirty: bool,
}
impl Writer {
    pub(crate) fn own_blob(&mut self, tx: &Transaction<'_>, source: i64, id: i64) -> Result<()> {
        if self.owners.insert((source, id)) {
            tx.prepare_cached("INSERT OR IGNORE INTO tool_blob_sources VALUES(?1,?2)")?
                .execute(params![source, id])?;
        }
        Ok(())
    }
    fn symbol(&mut self, tx: &Transaction<'_>, value: &str) -> Result<i64> {
        if value.is_empty() {
            return Ok(0);
        }
        if let Some(id) = self.symbols.get(value) {
            return Ok(*id);
        }
        let id = tx
            .prepare_cached(
                "INSERT INTO symbols(value) VALUES(?) ON CONFLICT(value) DO NOTHING RETURNING id",
            )?
            .query_row([value], |r| r.get(0))
            .optional()?
            .map_or_else(
                || {
                    tx.query_row("SELECT id FROM symbols WHERE value=?", [value], |r| {
                        r.get(0)
                    })
                },
                Ok,
            )?;
        self.symbols.insert(value.into(), id);
        Ok(id)
    }
    fn session(&mut self, tx: &Transaction<'_>, agent: &str, session: &str) -> Result<i64> {
        let key = (agent.to_string(), session.to_string());
        if let Some(id) = self.sessions.get(&key) {
            return Ok(*id);
        }
        let id = tx.prepare_cached("INSERT INTO event_sessions(agent,session) VALUES(?1,?2) ON CONFLICT(agent,session) DO NOTHING RETURNING id")?.query_row(params![agent,session], |r| r.get(0)).optional()?.map_or_else(||tx.query_row("SELECT id FROM event_sessions WHERE agent=?1 AND session=?2", params![agent,session], |r|r.get(0)), Ok)?;
        self.sessions.insert(key, id);
        Ok(id)
    }
    pub(crate) fn blob(&mut self, tx: &Transaction<'_>, source: i64, text: &str) -> Result<i64> {
        let key: [u8; 32] = Sha256::digest(text.as_bytes()).into();
        let id = if let Some(id) = self.blobs.get(&key) {
            *id
        } else {
            let existing = tx
                .prepare_cached("SELECT id FROM tool_blobs WHERE digest=?")?
                .query_row([key.as_slice()], |r| r.get(0))
                .optional()?;
            let id = if let Some(id) = existing {
                id
            } else {
                let encoded = encode(text)?;
                tx.prepare_cached(
                    "INSERT INTO tool_blobs(digest,ascii,body) VALUES(?1,?2,?3) RETURNING id",
                )?
                .query_row(params![key.as_slice(), text.is_ascii(), encoded], |r| {
                    r.get(0)
                })?
            };
            self.blobs.insert(key, id);
            id
        };
        self.own_blob(tx, source, id)?;
        Ok(id)
    }
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn event(
        &mut self,
        tx: &Transaction<'_>,
        source: i64,
        e: &Event,
        call_id: &str,
        turn: i64,
        separator: Option<usize>,
        project: &str,
        text: Option<&str>,
        bulk: bool,
    ) -> Result<Changed> {
        let session = self.session(tx, e.agent, &e.session)?;
        let kind = self.symbol(tx, e.kind)?;
        let name = self.symbol(tx, &e.name)?;
        let project = self.symbol(tx, project)?;
        let blob = text.map(|t| self.blob(tx, source, t)).transpose()?;
        struct Prior {
            id: i64,
            tool_id: i64,
            blob: Option<i64>,
            visible: bool,
            ts: i64,
            project: i64,
            turn: i64,
            kind: i64,
            name: i64,
            input: i64,
            output: i64,
            bytes: i64,
            ok: Option<bool>,
            child: String,
            state: String,
        }
        let old = if bulk {
            None
        } else {
            tx.prepare_cached("SELECT id,blob,visible,ts,project_key,native_turn,kind_key,name_key,input_chars,output_chars,output_bytes,ok,child_session,state,tool_id FROM event_rows WHERE source=?1 AND session_key=?2 AND call_id=?3 AND active=1")?.query_row(params![source,session,call_id], |r|Ok(Prior{id:r.get(0)?,blob:r.get(1)?,visible:r.get(2)?,ts:r.get(3)?,project:r.get(4)?,turn:r.get(5)?,kind:r.get(6)?,name:r.get(7)?,input:r.get(8)?,output:r.get(9)?,bytes:r.get(10)?,ok:r.get(11)?,child:r.get(12)?,state:r.get(13)?,tool_id:r.get(14)?})).optional()?
        };
        // Graph references name the immutable native event revision. A later
        // result for the same call can update the public row without changing
        // what an older branch will restore.
        let revision = e.agent == "pi"
            && old.as_ref().is_some_and(|p| {
                p.blob != blob
                    || p.kind != kind
                    || p.name != name
                    || p.input != e.input_chars as i64
                    || p.output != e.output_chars as i64
                    || p.bytes != e.output_bytes as i64
                    || p.ok != e.ok
                    || p.child != e.child_session
                    || p.state != e.state
            });
        if let Some(prior) = old.as_ref().filter(|_| revision) {
            tx.execute("UPDATE event_rows SET active=0 WHERE id=?", [prior.id])?;
        }
        let physical_id = if let Some(prior) = old.as_ref().filter(|_| !revision) {
            prior.id
        } else {
            let id = if let Some(id) = self.next_event {
                id
            } else {
                tx.query_row("SELECT coalesce(max(id),0)+1 FROM event_rows", [], |r| {
                    r.get(0)
                })?
            };
            self.next_event = Some(id + 1);
            id
        };
        let tool_id = old.as_ref().map_or(physical_id, |v| v.tool_id);
        let end = separator.map(|n| n as i64);
        let start = separator.map(|n| if n == 0 { 0 } else { n as i64 + 1 });
        // A control event has no searchable text; its input (an API error's message, a
        // notification's fields) is kept in `meta`, which no control event otherwise uses.
        let meta = if e.kind == "control" {
            &e.input
        } else {
            &e.meta
        };
        tx.prepare_cached("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,ok,child_session,meta,state,input_end,output_start,project_key,blob) VALUES(?19,?20,?1,?2,?3,?4,?5,?6,?7,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18) ON CONFLICT(source,session_key,call_id) WHERE active=1 DO UPDATE SET kind_key=excluded.kind_key,name_key=excluded.name_key,ts=excluded.ts,native_turn=excluded.native_turn,input_chars=excluded.input_chars,output_chars=excluded.output_chars,output_bytes=excluded.output_bytes,ok=excluded.ok,child_session=excluded.child_session,meta=excluded.meta,state=excluded.state,input_end=excluded.input_end,output_start=excluded.output_start,project_key=excluded.project_key,blob=excluded.blob")?.execute(params![source,session,call_id,kind,name,e.ts,turn,e.input_chars as i64,e.output_chars as i64,e.output_bytes as i64,e.ok,e.child_session,meta,e.state,end,start,project,blob,physical_id,tool_id])?;
        let text_changed = old.as_ref().map_or(blob.is_some(), |v| v.blob != blob);
        let dirty = text_changed
            || old
                .as_ref()
                .is_some_and(|v| v.ts != e.ts || v.project != project || v.turn != turn);
        Ok(Changed {
            id: tool_id,
            old_id: old.as_ref().map(|v| v.tool_id),
            text_changed,
            old_visible: old.is_some_and(|v| v.visible && v.blob.is_some()),
            has_text: blob.is_some(),
            dirty,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };

    #[test]
    fn bounded_tool_ids_do_not_rescan_an_unselective_agent() {
        let mut work = Vec::new();
        for count in [2048, 8192] {
            let mut c = Connection::open_in_memory().unwrap();
            c.execute_batch("CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);")
                .unwrap();
            schema(&c).unwrap();
            let tx = c.transaction().unwrap();
            tx.execute("INSERT INTO event_sessions VALUES(1,'pi','fixture')", [])
                .unwrap();
            for id in 1..=count {
                tx.prepare_cached("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key,blob) VALUES(?1,?1,1,1,?1,0,0,0,0,0,0,0,0,'','','',0,1)").unwrap().execute([id]).unwrap();
            }
            tx.commit().unwrap();
            let steps = Arc::new(AtomicUsize::new(0));
            let capture = steps.clone();
            c.progress_handler(
                100,
                Some(move || {
                    capture.fetch_add(100, Ordering::Relaxed);
                    false
                }),
            );
            let ids: Vec<i64> = c
                .prepare(&format!("SELECT p.id FROM {CANDIDATES_BY_ID} p WHERE p.visible=1 AND p.agent='pi' AND p.id>=1 AND p.id<129"))
                .unwrap()
                .query_map([], |r| r.get(0))
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap();
            assert_eq!(ids.len(), 128);
            work.push(steps.load(Ordering::Relaxed));
        }
        assert!(work[1] < work[0] * 2, "{work:?}");
    }
}
