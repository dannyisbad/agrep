//! One conversation's message rows into user turns, replies and tool events.

use std::collections::HashMap;

use rusqlite::Connection;
use serde_json::Value;

use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::intake::{Skip, Tally};
use crate::model::{Event, Message, RawMessage};

pub(super) const PARENT_QUERY: &str =
    "SELECT COALESCE(parent_session_id,'') FROM sessions WHERE id = ?1";
pub(super) const MESSAGES_QUERY: &str = "SELECT role, parts, model, created_at FROM messages \
     WHERE session_id = ? ORDER BY created_at, id";

/// (role, parts JSON, model, created_at) as `MESSAGES_QUERY` selects them.
type MessageRow = (String, String, Option<String>, i64);

/// A text part's `data.text`.
fn text_data(part: &Value) -> Option<&str> {
    part.get("data")
        .and_then(|d| d.get("text"))
        .and_then(|t| t.as_str())
}

/// A part wrapper's text, if it is a text part.
fn part_text(part: &Value) -> Option<&str> {
    if part.get("type").and_then(|t| t.as_str()) == Some("text") {
        text_data(part)
    } else {
        None
    }
}

/// A digest of one conversation's stored message rows, None when they cannot all be read.
pub(super) fn conversation_digest(conn: &Connection, session: &str) -> Option<String> {
    let mut stmt = conn
        .prepare("SELECT id, role, parts, model, created_at FROM messages WHERE session_id = ?1 ORDER BY id")
        .ok()?;
    let mut rows = stmt.query([session]).ok()?;
    let mut hash = 0xcbf29ce484222325_u64;
    while let Some(row) = rows.next().ok()? {
        for column in 0..5 {
            let value: rusqlite::types::Value = row.get(column).ok()?;
            let text = format!("{value:?}\0");
            for byte in text.bytes() {
                hash ^= u64::from(byte);
                hash = hash.wrapping_mul(0x100000001b3);
            }
        }
    }
    Some(format!("rows:{hash:016x}"))
}

/// Parse one session's messages into (user turns + replies, events). Tool results pair to
/// their tool_call by id (they may share a message or arrive in a later one).
pub(super) fn parse_session(
    conn: &Connection,
    project: &str,
    session: &str,
) -> (Vec<Message>, Vec<Event>, bool) {
    // seen = message rows in this conversation
    let tally = crate::intake::tally();
    let mut stmt = match conn.prepare(MESSAGES_QUERY) {
        Ok(s) => s,
        Err(e) => {
            tally.error(&format!("message query: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };
    let rows = stmt.query_map([session], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, Option<String>>(2)?,
            r.get::<_, i64>(3)?,
        ))
    });
    let rows = match rows {
        Ok(r) => r,
        Err(e) => {
            tally.error(&format!("message rows: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };
    let mut parse = SessionParse::new(conn, &tally, project, session);
    for (row_ordinal, row) in rows.enumerate() {
        parse.row(row_ordinal, row);
    }
    (
        parse.out.into_iter().map(RawMessage::freeze).collect(),
        parse.events,
        parse.healthy,
    )
}

/// One conversation's row walk: user rows open turns, assistant text joins the last turn as
/// its reply, tool_call parts become events completed by their tool_result.
struct SessionParse<'a> {
    tally: &'a Tally,
    project: &'a str,
    session: &'a str,
    parent: String,
    out: Vec<RawMessage>,
    events: Vec<Event>,
    pending: HashMap<String, usize>,
    turn: u32,
    // parts are in order: a tool_call sets the flag the next text consumes
    worked: bool,
    healthy: bool,
}

impl<'a> SessionParse<'a> {
    fn new(conn: &Connection, tally: &'a Tally, project: &'a str, session: &'a str) -> Self {
        let mut healthy = true;
        let parent = match conn.query_row(PARENT_QUERY, [session], |r| r.get::<_, String>(0)) {
            Ok(parent) => parent,
            Err(error) => {
                tally.error(&format!("session parent: {error}"));
                healthy = false;
                String::new()
            }
        };
        Self {
            tally,
            project,
            session,
            parent,
            out: Vec::new(),
            events: Vec::new(),
            pending: HashMap::new(),
            turn: 0,
            worked: false,
            healthy,
        }
    }

    fn row(&mut self, row_ordinal: usize, row: rusqlite::Result<MessageRow>) {
        self.tally.seen();
        let (role, parts_json, model, created_at) = match row {
            Ok(row) => row,
            Err(e) => {
                self.tally.error(&format!("message row: {e}"));
                self.healthy = false;
                return;
            }
        };
        let parts: Vec<Value> = match serde_json::from_str(&parts_json) {
            Ok(parts) => parts,
            Err(e) => {
                self.tally
                    .error(&format!("{e}: {}", crate::intake::clip(&parts_json, 80)));
                self.healthy = false;
                return;
            }
        };
        if role == "user" {
            self.user(&parts, created_at);
        } else if role == "assistant" {
            self.assistant(&parts, model.as_deref(), created_at, row_ordinal);
        } else {
            self.tally.skip(Skip::NonHuman);
        }
    }

    fn user(&mut self, parts: &[Value], created_at: i64) {
        let mut text = String::new();
        for p in parts {
            if let Some(t) = part_text(p) {
                if !text.is_empty() {
                    text.push('\n');
                }
                text.push_str(t);
            }
        }
        if text.trim().is_empty() {
            self.tally.skip(Skip::EmptyText);
            return;
        }
        if is_wrapper(&text) {
            self.tally.skip(Skip::Wrapper);
            return;
        }
        self.tally.row();
        self.worked = false;
        self.out.push(RawMessage {
            agent: "crush",
            project: self.project.to_string(),
            session: self.session.to_string(),
            ts: created_at,
            turn: self.turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: !self.parent.is_empty(),
            parent: self.parent.clone(),
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }

    fn assistant(
        &mut self,
        parts: &[Value],
        model: Option<&str>,
        created_at: i64,
        row_ordinal: usize,
    ) {
        let mut reply = String::new();
        // the row's text parts make one reply block: its join is what came before the
        // first of them, calls after that join it to the next row
        let mut worked_before = None;
        for (part_ordinal, p) in parts.iter().enumerate() {
            match p.get("type").and_then(|t| t.as_str()) {
                Some("text") => {
                    if let Some(t) = text_data(p) {
                        if worked_before.is_none() {
                            worked_before = Some(std::mem::take(&mut self.worked));
                        }
                        if !reply.is_empty() {
                            reply.push('\n');
                        }
                        reply.push_str(t);
                    }
                }
                Some("tool_call") => self.tool_call(p, created_at, row_ordinal, part_ordinal),
                Some("tool_result") => self.tool_result(p),
                _ => {} // reasoning / finish / image / binary: not the reply text
            }
        }
        if !reply.trim().is_empty() {
            if let Some(last) = self.out.last_mut() {
                crate::ingest::append_reply(last, &reply);
                crate::ingest::ReplyOrder::joined_known(
                    &mut last.reply_shape,
                    worked_before.unwrap_or(false),
                );

                if last.model.is_empty() {
                    if let Some(m) = model {
                        if !m.is_empty() {
                            last.model = m.to_string();
                        }
                    }
                }
            }
        }
        self.tally.agent_row();
    }

    fn tool_call(&mut self, p: &Value, created_at: i64, row_ordinal: usize, part_ordinal: usize) {
        let d = p.get("data").cloned().unwrap_or(Value::Null);
        let name = d
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("?")
            .to_string();
        self.worked |= !crate::ingest::is_bookkeeping_tool(&name);
        let session = self.session;
        let call_id = d
            .get("id")
            .and_then(|i| i.as_str())
            .filter(|id| !id.trim().is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| format!("crush:{session}:{row_ordinal}:{part_ordinal}"));
        // input is a JSON-encoded string; summarize the parsed object
        let (input, input_chars) = d
            .get("input")
            .and_then(|i| i.as_str())
            .map(|s| {
                serde_json::from_str::<Value>(s)
                    .map(|v| summarize_tool_input_with_chars(&v))
                    .unwrap_or_else(|_| cap_str_with_chars(s, EVENT_CAP))
            })
            .unwrap_or_default();
        if d.get("id")
            .and_then(|i| i.as_str())
            .is_some_and(|id| !id.is_empty())
        {
            self.pending.insert(call_id.clone(), self.events.len());
        }
        self.tally.event();
        self.events.push(Event {
            turn: None,
            agent: "crush",
            session: session.to_string(),
            ts: created_at,
            kind: "tool",
            name,
            input,
            output: String::new(),
            input_chars,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id,
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        });
    }

    fn tool_result(&mut self, p: &Value) {
        let d = p.get("data").cloned().unwrap_or(Value::Null);
        let id = d.get("tool_call_id").and_then(|i| i.as_str()).unwrap_or("");
        let Some(&i) = self.pending.get(id) else {
            return;
        };
        let content = d.get("content").and_then(|c| c.as_str()).unwrap_or("");
        let event = &mut self.events[i];
        (event.output, event.output_chars, event.output_bytes) = cap_event_output(content);
        event.ok = d.get("is_error").and_then(|e| e.as_bool()).map(|e| !e);
        self.pending.remove(id);
    }
}
