#[cfg(test)]
use super::connection::{schema, search_indexes};
use super::replay::session_title;
#[cfg(test)]
use super::text::decode;
use super::text::digest;
use super::{Changes, WriteTimer};
use crate::intake_batch::SourceBatch;
use crate::model::Message;
use anyhow::Result;
#[cfg(test)]
use rusqlite::Connection;
use rusqlite::OptionalExtension;
use rusqlite::{params, Transaction};
use std::collections::{HashMap, HashSet};
use std::fs;

fn reply_metadata(m: &crate::model::Message) -> Result<String> {
    let mut value = serde_json::to_value(&*m.reply_shape)?;
    value["original_reply_chars"] = serde_json::json!(m.reply_chars);
    value["source_parent"] = serde_json::json!(m.parent);
    value["source_side"] = serde_json::json!(m.side);
    Ok(serde_json::to_string(&value)?)
}
pub(super) fn remove_source(
    tx: &Transaction<'_>,
    table: &str,
    key: &str,
    changes: &mut Changes,
) -> Result<()> {
    let ids: Vec<i64> = tx
        .prepare(&format!(
            "SELECT id FROM {table} WHERE source=(SELECT rowid FROM sources WHERE path=?)"
        ))?
        .query_map([key], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    for id in ids {
        erase(tx, table, id, changes)?;
    }
    if table == "tools" {
        if let Some(source) = tx
            .query_row("SELECT rowid FROM sources WHERE path=?", [key], |r| {
                r.get::<_, i64>(0)
            })
            .optional()?
        {
            crate::tool_store::release_source(tx, source)?;
        }
        tx.execute(
            "DELETE FROM parser_graph WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
        tx.execute(
            "DELETE FROM event_rows WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
    } else {
        tx.execute(
            "DELETE FROM session_sources WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
        tx.execute(
            "DELETE FROM reply_shapes WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
    }
    Ok(())
}
pub(super) fn erase(tx: &Transaction<'_>, t: &str, id: i64, changes: &mut Changes) -> Result<()> {
    changes.fts.delete(t, id);
    tx.execute(&format!("DELETE FROM {t} WHERE id=?"), [id])?;
    changes.touch(t, id);
    Ok(())
}
pub(super) struct NewRow<'a> {
    pub(super) agent: &'a str,
    pub(super) session: &'a str,
    pub(super) project: &'a str,
    pub(super) turn: i64,
    pub(super) who: &'a str,
    pub(super) model: &'a str,
    pub(super) model_source: &'a str,
    pub(super) ts: i64,
    pub(super) text: &'a str,
    pub(super) extra: String,
    pub(super) bulk: bool,
}
#[derive(Default)]
struct BulkRows {
    lanes: [Vec<Vec<rusqlite::types::Value>>; 2],
}
impl BulkRows {
    fn flush(&mut self, tx: &Transaction<'_>, lane: usize, changes: &mut Changes) -> Result<()> {
        let rows = &mut self.lanes[lane];
        if rows.is_empty() {
            return Ok(());
        }
        let _timer = WriteTimer::new(false);
        let table = if lane == 0 { "prose" } else { "tools" };
        let sql=format!("INSERT INTO {table}(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES {} ON CONFLICT(source,session,turn,who,extra) DO UPDATE SET project=excluded.project,model=excluded.model,model_source=excluded.model_source,ts=excluded.ts,text=excluded.text,digest=excluded.digest RETURNING id",vec!["(?,?,?,?,?,?,?,?,?,?,?,?,?)";rows.len()].join(","));
        let mut stmt = tx.prepare_cached(&sql)?;
        for id in stmt.query_map(rusqlite::params_from_iter(rows.iter().flatten()), |r| {
            r.get::<_, i64>(0)
        })? {
            let id = id?;
            changes.touch(table, id);
        }
        rows.clear();
        Ok(())
    }
    fn write(
        &mut self,
        tx: &Transaction<'_>,
        table: &str,
        source: i64,
        row: NewRow<'_>,
        changes: &mut Changes,
    ) -> Result<usize> {
        use rusqlite::types::Value;
        if !row.bulk {
            return upsert(tx, table, source, row, changes);
        }
        let lane = usize::from(table == "tools");
        {
            let _timer = WriteTimer::new(false);
            let encoded = if lane == 1 {
                lz4_flex::compress_prepend_size(row.text.as_bytes())
            } else {
                row.text.as_bytes().to_vec()
            };
            self.lanes[lane].push(vec![
                Value::Integer(source),
                row.agent.to_owned().into(),
                row.session.to_owned().into(),
                row.project.to_owned().into(),
                row.turn.into(),
                row.who.to_owned().into(),
                row.model.to_owned().into(),
                row.model_source.to_owned().into(),
                row.ts.into(),
                encoded.into(),
                digest(row.text).into(),
                row.extra.into(),
                row.turn.into(),
            ]);
        }
        if self.lanes[lane].len() >= 64 {
            self.flush(tx, lane, changes)?;
        }
        Ok(1)
    }
}
#[allow(clippy::too_many_arguments)]
fn store_event(
    tx: &Transaction<'_>,
    writer: &mut crate::tool_store::Writer,
    source: i64,
    event: &crate::model::Event,
    call_id: &str,
    turn: i64,
    separator: Option<usize>,
    project: &str,
    text: Option<crate::tool_store::Text<'_>>,
    bulk: bool,
    changes: &mut Changes,
) -> Result<usize> {
    let _timer = WriteTimer::new(false);
    let changed = writer.event(
        tx, source, event, call_id, turn, separator, project, text, bulk,
    )?;
    if changed.text_changed && !bulk {
        if changed.old_visible {
            changes.fts.delete("tools", changed.old_id.unwrap());
        }
        if changed.has_text {
            changes.fts.insert("tools", changed.id);
        }
    }
    if changed.dirty {
        changes.touch("tools", changed.id);
    }
    Ok(usize::from(changed.text_changed))
}
pub(super) fn upsert(
    tx: &Transaction<'_>,
    table: &str,
    source: i64,
    r: NewRow<'_>,
    changes: &mut Changes,
) -> Result<usize> {
    let _timer = WriteTimer::new(false);
    let dg = digest(r.text);
    if r.bulk {
        let encoded = if table == "tools" {
            lz4_flex::compress_prepend_size(r.text.as_bytes())
        } else {
            r.text.as_bytes().to_vec()
        };
        let id=tx.prepare_cached(&format!("INSERT INTO {table}(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?5) ON CONFLICT(source,session,turn,who,extra) DO UPDATE SET project=excluded.project,model=excluded.model,model_source=excluded.model_source,ts=excluded.ts,text=excluded.text,digest=excluded.digest RETURNING id"))?.query_row(params![source,r.agent,r.session,r.project,r.turn,r.who,r.model,r.model_source,r.ts,encoded,dg,r.extra],|row|row.get::<_,i64>(0))?;
        changes.touch(table, id);
        return Ok(1);
    }
    type StoredRow = (i64, String, i64, String, String, String, bool);
    let old:Option<StoredRow>=tx.prepare_cached(&format!("SELECT id,digest,ts,project,model,model_source,visible FROM {table} WHERE source=?1 AND session=?2 AND turn=?3 AND who=?4 AND extra=?5"))?.query_row(params![source,r.session,r.turn,r.who,r.extra],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
    if let Some((id, ref old_dg, old_ts, ref project, ref model, ref model_source, visible)) = old {
        if *old_dg == dg {
            if old_ts == r.ts
                && project == r.project
                && model == r.model
                && model_source == r.model_source
            {
                return Ok(0);
            }
            if old_ts != r.ts {
                changes.touch(table, id);
            }
            tx.execute(
                &format!(
                    "UPDATE {table} SET project=?2,model=?3,model_source=?4,ts=?5 WHERE id=?1"
                ),
                params![id, r.project, r.model, r.model_source, r.ts],
            )?;
            return Ok(0);
        }
        let encoded = if table == "tools" {
            lz4_flex::compress_prepend_size(r.text.as_bytes())
        } else {
            r.text.as_bytes().to_vec()
        };
        if visible {
            changes.fts.delete(table, id);
        }
        tx.prepare_cached(&format!("UPDATE {table} SET project=?2,model=?3,model_source=?4,ts=?5,text=?6,digest=?7 WHERE id=?1"))?.execute(params![id,r.project,r.model,r.model_source,r.ts,encoded,dg])?;
        if visible {
            changes.fts.insert(table, id);
        }
        changes.touch(table, id);
        return Ok(1);
    }
    let encoded = if table == "tools" {
        lz4_flex::compress_prepend_size(r.text.as_bytes())
    } else {
        r.text.as_bytes().to_vec()
    };
    tx.prepare_cached(&format!("INSERT INTO {table}(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?5)"))?.execute(params![source,r.agent,r.session,r.project,r.turn,r.who,r.model,r.model_source,r.ts,encoded,dg,r.extra])?;
    let id = tx.last_insert_rowid();
    changes.fts.insert(table, id);
    changes.touch(table, id);
    Ok(1)
}
/// `prepared` holds the texts of `s.events` by index, where they were prepared off the
/// writer's thread; the rest are formatted here.
pub(super) fn replace(
    tx: &Transaction<'_>,
    s: &SourceBatch,
    prepared: Vec<Option<crate::tool_store::Prepared>>,
    changes: &mut Changes,
    bulk: bool,
) -> Result<usize> {
    tx.execute(
        "INSERT OR IGNORE INTO sources(path,agent,size,mtime_ns,change_token) VALUES(?1,?2,0,0,x'02')",
        params![s.key, s.agent],
    )?;
    let source: i64 = tx.query_row("SELECT rowid FROM sources WHERE path=?", [&s.key], |r| {
        r.get(0)
    })?;
    if !s.append && (s.agent != "pi" || s.reset_nodes) {
        crate::tool_store::release_source(tx, source)?;
    }
    if !s.append {
        tx.execute("DELETE FROM session_sources WHERE source=?", [source])?;
    }
    let members: HashSet<_> = s
        .messages
        .iter()
        .map(|m| (m.agent, m.session.as_ref()))
        .chain(s.events.iter().map(|e| (e.agent, e.session.as_str())))
        .collect();
    let mut link = tx.prepare_cached("INSERT OR IGNORE INTO session_sources VALUES(?1,?2,?3)")?;
    for (agent, session) in members {
        link.execute(params![agent, session, source])?;
    }
    let mut keys: HashSet<(String, i64, String, String)> = HashSet::new();
    let mut count = 0;
    let mut bulk_rows = BulkRows::default();
    let mut event_writer = crate::tool_store::Writer::default();
    let mut meta: HashMap<&str, (&str, i64, bool, &str)> = HashMap::new();
    let initial_bounds = initial_session_bounds(s, bulk);
    let mut prose = ProseRows {
        keys: &mut keys,
        count: &mut count,
        bulk_rows: &mut bulk_rows,
        meta: &mut meta,
    };
    prose.write(tx, s, source, changes, bulk)?;
    let message_keys: HashSet<_> = s
        .messages
        .iter()
        .map(|m| (m.session.to_string(), m.turn as i64))
        .collect();
    let stale_shapes: Vec<(String, i64)> = tx
        .prepare_cached("SELECT session,turn FROM reply_shapes WHERE source=?1 AND NOT ?2")?
        .query_map(params![source, s.append], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (session, turn) in stale_shapes {
        if !s.append && !message_keys.contains(&(session.clone(), turn)) {
            tx.execute(
                "DELETE FROM reply_shapes WHERE source=?1 AND session=?2 AND turn=?3",
                params![source, session, turn],
            )?;
        }
    }
    let mut metadata_keys = HashSet::new();
    let mut event_keys = HashSet::new();
    count += EventRows {
        meta: &meta,
        metadata_keys: &mut metadata_keys,
        event_keys: &mut event_keys,
        event_writer: &mut event_writer,
    }
    .write(tx, s, source, prepared, changes, bulk)?;
    bulk_rows.flush(tx, 0, changes)?;
    bulk_rows.flush(tx, 1, changes)?;
    for (session, (first, last, _, title)) in initial_bounds {
        tx.prepare_cached(
            "UPDATE sessions SET first_ts=?3,last_ts=?4,title=?5 WHERE agent=?1 AND id=?2",
        )?
        .execute(params![s.agent, session, first, last, title])?;
    }
    if bulk {
        // The first build contains no previous source contributions. Besides
        // avoiding pointless diffs, this must precede inactive-row cleanup:
        // its source index is intentionally deferred until bulk loading ends.
        return Ok(count);
    }
    for (t, set) in [("prose", keys), ("tools", event_keys)] {
        let stale: Vec<i64> = tx
            .prepare(&format!(
                "SELECT id,session,turn,who,extra FROM {t} WHERE source=?1 AND NOT ?2"
            ))?
            .query_map(params![source, s.append], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?),
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?
            .into_iter()
            .filter(|(_, k)| !s.append && !set.contains(k))
            .map(|(id, _)| id)
            .collect();
        for id in stale {
            erase(tx, t, id, changes)?;
            count += 1;
        }
    }
    let stale_events: Vec<(String, String)> = tx
        .prepare_cached("SELECT session,call_id FROM events WHERE source=?1 AND NOT ?2")?
        .query_map(params![source, s.append], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (session, call_id) in stale_events {
        if !s.append && !metadata_keys.contains(&(session.clone(), call_id.clone())) {
            tx.execute(
                "UPDATE event_rows SET active=0 WHERE source=?1 AND session_key IN (SELECT id FROM event_sessions WHERE session=?2) AND call_id=?3 AND active=1",
                params![source, session, call_id],
            )?;
        }
    }
    if !s.append && (s.agent != "pi" || s.reset_nodes) {
        tx.execute(
            "DELETE FROM event_rows WHERE source=? AND active=0",
            [source],
        )?;
    }
    Ok(count)
}
fn session(tx: &Transaction<'_>, m: &Message, cwd: &str) -> Result<()> {
    let title = if matches!(m.who.as_ref(), "agent" | "recap") {
        String::new()
    } else {
        session_title(&m.text)
    };
    let family = if m.parent.is_empty() {
        m.session.as_ref()
    } else {
        m.parent.as_ref()
    };
    tx.prepare_cached("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?8) ON CONFLICT(agent,id) DO UPDATE SET project=?3,parent=?4,side=?6,first_ts=CASE WHEN ?8=0 THEN first_ts WHEN first_ts=0 THEN ?8 ELSE MIN(first_ts,?8) END,last_ts=MAX(last_ts,?8),title=CASE WHEN ?9=0 OR title='' THEN ?7 ELSE title END")?.execute(params![m.agent,m.session.as_ref(),m.project.as_ref(),m.parent.as_ref(),family,m.side,title,m.ts.max(0),m.turn])?;
    tx.prepare_cached("UPDATE sessions SET cwd=?3 WHERE agent=?1 AND id=?2 AND ?3!=''")?
        .execute(params![m.agent, m.session.as_ref(), cwd])?;
    Ok(())
}
fn source_cwd(path: &str) -> String {
    use std::io::Read;
    let Ok(file) = fs::File::open(path) else {
        return String::new();
    };
    let mut bytes = Vec::new();
    if path.ends_with(".gz") {
        let _ = flate2::read::GzDecoder::new(file)
            .take(16384)
            .read_to_end(&mut bytes);
    } else {
        let _ = file.take(16384).read_to_end(&mut bytes);
    }
    for line in bytes.split(|b| *b == b'\n').take(8) {
        if let Ok(v) = serde_json::from_slice::<serde_json::Value>(line) {
            for cwd in [v.get("cwd"), v.pointer("/payload/cwd")]
                .into_iter()
                .flatten()
            {
                if let Some(s) = cwd.as_str() {
                    return s.to_string();
                }
            }
        }
    }
    String::new()
}

type RowKey = (String, i64, String, String);
type SessionMeta<'a> = HashMap<&'a str, (&'a str, i64, bool, &'a str)>;
struct ProseRows<'a, 'b> {
    keys: &'b mut HashSet<RowKey>,
    count: &'b mut usize,
    bulk_rows: &'b mut BulkRows,
    meta: &'b mut SessionMeta<'a>,
}
struct EventRows<'a> {
    meta: &'a SessionMeta<'a>,
    metadata_keys: &'a mut HashSet<(String, String)>,
    event_keys: &'a mut HashSet<RowKey>,
    event_writer: &'a mut crate::tool_store::Writer,
}
fn initial_session_bounds(s: &SourceBatch, bulk: bool) -> HashMap<&str, (i64, i64, u32, String)> {
    let mut initial_bounds: HashMap<&str, (i64, i64, u32, String)> = HashMap::new();
    if bulk {
        for m in &s.messages {
            if m.text.is_empty() && m.reply.is_empty() {
                continue;
            }
            let title_turn =
                if !m.text.trim().is_empty() && !matches!(m.who.as_ref(), "agent" | "recap") {
                    m.turn
                } else {
                    u32::MAX
                };
            let entry = initial_bounds.entry(&m.session).or_insert_with(|| {
                (
                    0,
                    0,
                    title_turn,
                    if title_turn == u32::MAX {
                        String::new()
                    } else {
                        session_title(&m.text)
                    },
                )
            });
            if m.ts > 0 {
                entry.0 = if entry.0 > 0 { entry.0.min(m.ts) } else { m.ts };
                entry.1 = entry.1.max(m.ts);
            }
            if title_turn < entry.2 {
                entry.2 = title_turn;
                entry.3 = session_title(&m.text);
            }
        }
    }
    initial_bounds
}

impl<'a> ProseRows<'a, '_> {
    fn write(
        &mut self,
        tx: &Transaction<'_>,
        s: &'a SourceBatch,
        source: i64,
        changes: &mut Changes,
        bulk: bool,
    ) -> Result<()> {
        let Self {
            keys,
            count,
            bulk_rows,
            meta,
        } = self;
        let session_last: HashMap<&str, usize> = s
            .messages
            .iter()
            .enumerate()
            .map(|(i, m)| (m.session.as_ref(), i))
            .collect();
        let source_directory = source_cwd(&s.key);
        for (message_index, m) in s.messages.iter().enumerate() {
            meta.insert(&m.session, (&m.project, m.ts, m.side, &m.parent));
            let cwd = if m.project.starts_with('/') || m.project.contains(":\\") {
                m.project.to_string()
            } else {
                source_directory.clone()
            };
            if !bulk || session_last.get(m.session.as_ref()) == Some(&message_index) {
                session(tx, m, &cwd)?;
            }
            if s.append && !m.reply.is_empty() {
                let prior: Option<String> = tx
                    .query_row(
                        "SELECT body FROM reply_shapes WHERE source=?1 AND session=?2 AND turn=?3",
                        params![source, m.session.as_ref(), m.turn],
                        |r| r.get(0),
                    )
                    .optional()?;
                let old_chars = prior
                    .and_then(|v| serde_json::from_str::<serde_json::Value>(&v).ok())
                    .and_then(|v| v.get("original_reply_chars").and_then(|v| v.as_u64()));
                if old_chars.is_some_and(|n| n < (m.reply_chars as u64)) {
                    tx.execute("UPDATE event_rows SET meta=trim(replace(meta,'after_reply=1','')) WHERE source=?1 AND session_key IN (SELECT id FROM event_sessions WHERE session=?2) AND native_turn=?3 AND meta LIKE '%after_reply=1%'",params![source,m.session.as_ref(),m.turn])?;
                }
            }
            tx.prepare_cached("INSERT INTO reply_shapes VALUES(?1,?2,?3,?4) ON CONFLICT(source,session,turn) DO UPDATE SET body=excluded.body WHERE body!=excluded.body")?.execute(params![source,m.session.as_ref(),m.turn,reply_metadata(m)?])?;
            for (who, text) in [
                (m.who.as_ref(), m.text.as_ref()),
                ("agent", m.reply.as_ref()),
            ] {
                if text.is_empty() {
                    continue;
                }
                let extra = String::new();
                keys.insert((
                    m.session.to_string(),
                    m.turn as i64,
                    who.to_string(),
                    extra.clone(),
                ));
                **count += bulk_rows.write(
                    tx,
                    "prose",
                    source,
                    NewRow {
                        agent: m.agent,
                        session: &m.session,
                        project: &m.project,
                        turn: m.turn as i64,
                        who,
                        model: &m.model,
                        model_source: &m.model_source,
                        ts: m.ts,
                        text,
                        extra,
                        bulk,
                    },
                    changes,
                )?;
            }
        }
        Ok(())
    }
}

impl EventRows<'_> {
    fn write(
        &mut self,
        tx: &Transaction<'_>,
        s: &SourceBatch,
        source: i64,
        mut prepared: Vec<Option<crate::tool_store::Prepared>>,
        changes: &mut Changes,
        bulk: bool,
    ) -> Result<usize> {
        let Self {
            meta,
            metadata_keys,
            event_keys,
            event_writer,
        } = self;
        let mut count = 0;
        let mut event_turn: HashMap<&str, i64> = HashMap::new();
        let mut turns: HashMap<&str, Vec<(i64, i64)>> = HashMap::new();
        for m in &s.messages {
            turns
                .entry(&m.session)
                .or_default()
                .push((m.ts, m.turn as i64));
        }
        for v in turns.values_mut() {
            v.sort_unstable();
            let mut highest = 0;
            for (_, t) in v {
                highest = highest.max(*t);
                *t = highest;
            }
        }

        let mut anonymous: HashMap<String, u32> = HashMap::new();
        for (index, e) in s.events.iter().enumerate() {
            let call_id = if e.call_id.trim().is_empty() {
                let identity = digest(
                    &serde_json::json!([
                        e.agent,
                        e.session,
                        e.ts,
                        e.kind,
                        e.name,
                        e.input,
                        e.output,
                        e.ok,
                        e.child_session,
                        e.meta
                    ])
                    .to_string(),
                );
                let occurrence = anonymous.entry(identity.clone()).or_default();
                let id = format!("agrep-anon-{}-{}", &identity[..16], occurrence);
                *occurrence += 1;
                id
            } else {
                e.call_id.clone()
            };
            if !metadata_keys.insert((e.session.clone(), call_id.clone())) {
                continue;
            }
            let (project, _, _, _) = meta
                .get(e.session.as_str())
                .copied()
                .unwrap_or(("unknown", 0, false, ""));
            let turn = e.turn.map(i64::from).unwrap_or_else(|| {
                turns
                    .get(e.session.as_str())
                    .and_then(|v| {
                        v.partition_point(|(ts, _)| *ts <= e.ts)
                            .checked_sub(1)
                            .map(|i| v[i].1)
                    })
                    .unwrap_or_else(|| *event_turn.get(e.session.as_str()).unwrap_or(&0))
            });
            event_turn.insert(&e.session, turn);
            if e.kind == "control" {
                store_event(
                    tx,
                    event_writer,
                    source,
                    e,
                    &call_id,
                    turn,
                    None,
                    project,
                    None,
                    bulk,
                    changes,
                )?;
                continue;
            }
            let event_call_id = call_id.clone();
            let extra = call_id;
            event_keys.insert((e.session.clone(), turn, "tool".into(), extra.clone()));
            let formatted;
            let (separator, text) = match prepared.get_mut(index).and_then(Option::take) {
                Some(p) => (p.separator, p.text),
                None => {
                    let (text, separator) = crate::tool_text::format(e);
                    formatted = text;
                    (separator, crate::tool_store::Text::Plain(&formatted))
                }
            };
            count += store_event(
                tx,
                event_writer,
                source,
                e,
                &event_call_id,
                turn,
                separator,
                project,
                Some(text),
                bulk,
                changes,
            )?;
            if !bulk || !meta.contains_key(e.session.as_str()) {
                tx.prepare_cached("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES(?1,?2,?3,'',?2,0,'',0,0) ON CONFLICT(agent,id) DO NOTHING")?.execute(params![e.agent,e.session,project])?;
            }
        }
        Ok(count)
    }
}

#[cfg(test)]
mod row_diff_tests {
    #[test]
    fn initial_source_writes_do_not_scan_preceding_sources() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let mut counts = Vec::new();
        for n in [128, 512] {
            let mut c = rusqlite::Connection::open_in_memory().unwrap();
            super::schema(&c).unwrap();
            super::search_indexes(&c, false).unwrap();
            let operations = Arc::new(AtomicUsize::new(0));
            let recorded = operations.clone();
            c.progress_handler(
                100,
                Some(move || {
                    recorded.fetch_add(100, Ordering::Relaxed);
                    false
                }),
            );
            let tx = c.transaction().unwrap();
            let mut changes = super::Changes::default();
            for i in 0..n {
                let source = crate::intake_batch::SourceBatch {
                    key: format!("source-{i}"),
                    agent: "pi".into(),
                    events: vec![crate::model::Event {
                        agent: "pi",
                        session: format!("session-{i}"),
                        ts: 1,
                        turn: Some(0),
                        kind: "tool",
                        name: "echo".into(),
                        input: "hello".into(),
                        output: String::new(),
                        input_chars: 5,
                        output_chars: 0,
                        output_bytes: 0,
                        ok: Some(true),
                        call_id: "call".into(),
                        child_session: String::new(),
                        meta: String::new(),
                        state: String::new(),
                    }],
                    ..Default::default()
                };
                assert_eq!(
                    super::replace(&tx, &source, Vec::new(), &mut changes, true).unwrap(),
                    1
                );
            }
            counts.push(operations.load(Ordering::Relaxed));
        }
        assert!(
            counts[1] < counts[0] * 6,
            "initial writes grew faster than rows: {counts:?}"
        );
    }

    #[test]
    fn unchanged_payload_and_metadata_issue_no_row_update() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let row = |model| super::NewRow {
            agent: "pi",
            session: "s",
            project: "p",
            turn: 0,
            who: "user",
            model,
            model_source: "explicit",
            ts: 1,
            text: "unchanged",
            extra: String::new(),
            bulk: false,
        };
        let mut changes = super::Changes::default();
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("a"), &mut changes).unwrap();
        tx.commit().unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let seen = count.clone();
        c.update_hook(Some(
            move |_: rusqlite::hooks::Action, _: &str, table: &str, _: i64| {
                if table == "prose" {
                    seen.fetch_add(1, Ordering::Relaxed);
                }
            },
        ));
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("a"), &mut changes).unwrap();
        tx.commit().unwrap();
        assert_eq!(count.load(Ordering::Relaxed), 0);
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("b"), &mut changes).unwrap();
        tx.commit().unwrap();
        assert_eq!(count.load(Ordering::Relaxed), 1);
    }
}

#[cfg(test)]
mod event_contract_tests {
    use super::*;
    #[test]
    fn control_timing_and_multiline_tool_separator_are_stored() {
        let mut c = Connection::open_in_memory().unwrap();
        schema(&c).unwrap();
        let event = |kind, name: &str, input: &str, output: &str| crate::model::Event {
            agent: "pi",
            session: "fixture-session".into(),
            ts: 1234,
            turn: Some(3),
            kind,
            name: name.into(),
            input: input.into(),
            output: output.into(),
            input_chars: input.chars().count(),
            output_chars: output.chars().count(),
            output_bytes: output.len(),
            ok: None,
            call_id: name.into(),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        };
        let tx = c.transaction().unwrap();
        let mut changes = Changes::default();
        let source = SourceBatch {
            key: "fixture".into(),
            agent: "pi".into(),
            events: vec![
                event("control", "compaction", "", ""),
                event("tool", "shell", "line one\nline two", "réponse"),
            ],
            ..Default::default()
        };
        replace(&tx, &source, Vec::new(), &mut changes, false).unwrap();
        for name in ["compaction", "shell"] {
            let body: String = tx
                .query_row("SELECT body FROM events WHERE call_id=?", [name], |r| {
                    r.get(0)
                })
                .unwrap();
            let body: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(body["ts"], 1234);
            assert_eq!(body["turn"], 3);
            assert_eq!(body["position"], 3);
            if name == "compaction" {
                assert!(body["input_end"].is_null());
            } else {
                let text: Vec<u8> = tx
                    .query_row("SELECT text FROM tools", [], |r| r.get(0))
                    .unwrap();
                let text = decode("tools", text).unwrap();
                let end = body["input_end"].as_u64().unwrap() as usize;
                let start = body["output_start"].as_u64().unwrap() as usize;
                assert_eq!(text.chars().nth(end), Some('\n'));
                assert_eq!(text.chars().skip(start).collect::<String>(), "réponse");
            }
        }
        assert_eq!(
            tx.query_row("SELECT count(*) FROM tools", [], |r| r.get::<_, usize>(0))
                .unwrap(),
            1
        );
    }

    #[test]
    fn user_side_count_follows_inserts_visibility_and_deletes() {
        let mut c = Connection::open_in_memory().unwrap();
        schema(&c).unwrap();
        let mut changes = Changes::default();
        let tx = c.transaction().unwrap();
        for (turn, who) in [
            (0, "user"),
            (0, "agent"),
            (1, "user"),
            (1, "agent"),
            (2, "recap"),
        ] {
            let row = NewRow {
                agent: "pi",
                session: "s",
                project: "p",
                turn,
                who,
                model: "m",
                model_source: "explicit",
                ts: 1,
                text: "body",
                extra: String::new(),
                bulk: false,
            };
            upsert(&tx, "prose", 1, row, &mut changes).unwrap();
        }
        let counted = |tx: &rusqlite::Transaction| -> (i64, i64) {
            let kept: i64 = tx
                .query_row(
                    "SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_user_count'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            let scanned: i64 = tx
                .query_row(
                    "SELECT count(*) FROM prose WHERE visible=1 AND who!='agent'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            (kept, scanned)
        };
        assert_eq!(counted(&tx), (3, 3));
        tx.execute("UPDATE prose SET visible=0 WHERE turn=1", [])
            .unwrap();
        assert_eq!(counted(&tx), (2, 2));
        tx.execute("UPDATE prose SET visible=1 WHERE who='user'", [])
            .unwrap();
        assert_eq!(counted(&tx), (3, 3));
        tx.execute("DELETE FROM prose WHERE who IN ('recap','agent')", [])
            .unwrap();
        assert_eq!(counted(&tx), (2, 2));
    }
}
