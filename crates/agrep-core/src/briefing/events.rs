//! Lazy event payloads and snapshot-ordered event reads.
use crate::history::Session;
use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use std::{cell::OnceCell, collections::HashSet};

/// An indexed event as briefings read it. Classification reads the columns of every event of
/// a chat but the text of only a few, so the stored text is decoded on first use.
pub struct Event<'a> {
    pub ts: i64,
    pub kind: &'a str,
    pub name: &'a str,
    pub ok: Option<bool>,
    pub child_session: String,
    pub meta: String,
    pub state: String,
    pub(super) input_chars: i64,
    pub(super) input_end: Option<usize>,
    pub(super) output_start: Option<usize>,
    pub(super) blob: Option<i64>,
    pub(super) text: OnceCell<(String, String)>,
}
impl Event<'_> {
    /// The input and output `evidence::load_events` gives the event.
    fn text(&self, c: &Connection) -> Result<&(String, String)> {
        if let Some(text) = self.text.get() {
            return Ok(text);
        }
        let text = if self.kind == "control" {
            (self.meta.clone(), String::new())
        } else {
            let stored = match self.blob {
                Some(blob) => c
                    .prepare_cached("SELECT body FROM tool_blobs WHERE id=?1")?
                    .query_row([blob], |r| r.get::<_, Vec<u8>>(0))
                    .optional()?,
                None => None,
            };
            let text = stored
                .map(|bytes| crate::store::decode("tools", bytes))
                .transpose()?
                .unwrap_or_default();
            crate::tool_text::split(
                &text,
                self.kind,
                self.name,
                self.ok,
                self.input_end,
                self.output_start,
            )
        };
        Ok(self.text.get_or_init(|| text))
    }
    pub fn input(&self, c: &Connection) -> Result<&str> {
        Ok(&self.text(c)?.0)
    }
    pub fn output(&self, c: &Connection) -> Result<&str> {
        Ok(&self.text(c)?.1)
    }
}
/// The kind and tool names `event_rows` refers to by id.
pub struct Symbols(Vec<(i64, String)>);
impl Symbols {
    pub fn load(c: &Connection) -> Result<Self> {
        let mut q = c.prepare("SELECT id,value FROM symbols ORDER BY id")?;
        let names = q.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
        Ok(Self(names.collect::<rusqlite::Result<_>>()?))
    }
    fn get(&self, id: i64) -> Option<&str> {
        let at = self.0.binary_search_by_key(&id, |(key, _)| *key).ok()?;
        Some(&self.0[at].1)
    }
}
/// The events `evidence::load_events` loads for the chat, in its order and with its replay
/// de-duplication, their text left stored.
pub fn events<'a>(c: &Connection, symbols: &'a Symbols, s: &Session) -> Result<Vec<Event<'a>>> {
    // That query's plan feeds SQLite's sorter the rows in this index order to sort by
    // (position, ts, call_id). The sorter is stable, so a stable sort of the same rows here
    // gives its order without copying every row through the sorter.
    let mut q = c.prepare_cached("SELECT e.position,e.call_id,e.ts,e.kind_key,e.name_key,CASE WHEN e.ok IS NULL THEN NULL WHEN e.ok THEN 1 ELSE 0 END,e.child_session,e.meta,e.state,e.input_chars,e.input_end,e.output_start,e.blob FROM event_rows e JOIN event_sessions s ON s.id=e.session_key WHERE e.active=1 AND e.visible=1 AND s.session=?1 AND e.position BETWEEN ?2 AND ?3 ORDER BY s.id,e.position,e.ts,e.id")?;
    let mut rows = q.query(params![s.id, i64::MIN, i64::MAX])?;
    let offset = |v: Option<i64>| v.and_then(|v| usize::try_from(v).ok());
    let mut found: Vec<(i64, String, Event<'a>)> = vec![];
    while let Some(r) = rows.next()? {
        let (Some(kind), Some(name)) = (symbols.get(r.get(3)?), symbols.get(r.get(4)?)) else {
            continue;
        };
        found.push((
            r.get(0)?,
            r.get(1)?,
            Event {
                ts: r.get(2)?,
                kind,
                name,
                ok: r.get::<_, Option<i64>>(5)?.map(|ok| ok != 0),
                child_session: r.get(6)?,
                meta: r.get(7)?,
                state: r.get(8)?,
                input_chars: r.get(9)?,
                input_end: offset(r.get(10)?),
                output_start: offset(r.get(11)?),
                blob: r.get(12)?,
                text: OnceCell::new(),
            },
        ));
    }
    found.sort_by(|(p1, call1, e1), (p2, call2, e2)| (p1, e1.ts, call1).cmp(&(p2, e2.ts, call2)));
    // Replay copies of one call share its id; separate identical calls stay separate.
    let mut seen = HashSet::new();
    Ok(found
        .into_iter()
        .filter_map(|(_, call, event)| seen.insert(call).then_some(event))
        .collect())
}
