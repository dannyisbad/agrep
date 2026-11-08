//! One composer's bubbles into user turns, replies and tool events.

use std::collections::HashMap;

use rusqlite::Connection;
use serde_json::Value;

use super::kv_text_checked;
use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::intake::{Skip, Tally};
use crate::model::{Event, Message, RawMessage};

/// One tool call bubble -> a tool Event. `params` is the resolved argument object when the
/// call completed; `rawArgs` is the model's raw string (both JSON-encoded strings).
fn tool_event(session: &str, bubble_id: &str, ts: i64, tf: &Value) -> Event {
    let args = tf
        .get("params")
        .and_then(|p| p.as_str())
        .filter(|s| !s.trim().is_empty())
        .or_else(|| tf.get("rawArgs").and_then(|p| p.as_str()));
    let name = tf
        .get("name")
        .and_then(|n| n.as_str())
        .unwrap_or("?")
        .to_string();
    let mut state = String::new();
    let (input, input_chars) = args
        .map(|s| {
            serde_json::from_str::<Value>(s)
                .map(|v| {
                    state = crate::ingest::task_state(&name, Some(&v));
                    summarize_tool_input_with_chars(&v)
                })
                .unwrap_or_else(|_| cap_str_with_chars(s, EVENT_CAP))
        })
        .unwrap_or_default();
    let (output, output_chars, output_bytes) =
        cap_event_output(tf.get("result").and_then(|r| r.as_str()).unwrap_or(""));
    let ok = match tf.get("status").and_then(|s| s.as_str()) {
        Some("completed") => Some(true),
        Some("error") | Some("errored") | Some("failed") | Some("cancelled") | Some("canceled")
        | Some("rejected") | Some("aborted") => Some(false),
        _ => None,
    };
    Event {
        turn: None,
        agent: "cursor",
        session: session.to_string(),
        ts,
        kind: "tool",
        name,
        input,
        output,
        input_chars,
        output_chars,
        output_bytes,
        ok,
        call_id: tf
            .get("toolCallId")
            .and_then(|i| i.as_str())
            .filter(|id| !id.trim().is_empty())
            .map(str::to_string)
            // bubbleId is Cursor's durable row identity when toolFormerData omits its correlation id.
            .unwrap_or_else(|| format!("cursor:{bubble_id}")),
        child_session: String::new(),
        meta: String::new(),
        state,
    }
}

/// The composer's `composerData` record; None (counted) when it is missing or unreadable.
fn composer_data(conn: &Connection, cid: &str, tally: &Tally) -> Option<Value> {
    let raw = match kv_text_checked(conn, "cursorDiskKV", &format!("composerData:{cid}")) {
        Ok(Some(raw)) => raw,
        Ok(None) => {
            tally.error("missing composerData row");
            return None;
        }
        Err(error) => {
            tally.error(&format!("composerData query: {error}"));
            return None;
        }
    };
    let Ok(cd) = serde_json::from_str::<Value>(&raw) else {
        tally.error(&format!(
            "bad composerData: {}",
            crate::intake::clip(&raw, 80)
        ));
        return None;
    };
    Some(cd)
}

/// Parse one composer into (user turns + replies, tool events), walking the header list in
/// conversation order and fetching each bubble row. Missing or NULL bubbles are skipped.
pub(super) fn parse_session(
    conn: &Connection,
    projects: &HashMap<String, String>,
    cid: &str,
) -> (Vec<Message>, Vec<Event>, bool) {
    // seen = bubble rows iterated within this conversation
    let tally = crate::intake::tally();
    let Some(cd) = composer_data(conn, cid, &tally) else {
        return (Vec::new(), Vec::new(), false);
    };
    let created = cd
        .get("createdAt")
        .and_then(|t| t.as_f64())
        .map(crate::ingest::parse_timestamp::epoch_number)
        .unwrap_or(0);
    let model = cd
        .pointer("/modelConfig/modelName")
        .and_then(|m| m.as_str())
        .unwrap_or("")
        .to_string();
    let project = projects
        .get(cid)
        .cloned()
        .unwrap_or_else(|| "cursor".to_string());
    let headers = cd
        .get("fullConversationHeadersOnly")
        .and_then(|h| h.as_array())
        .cloned()
        .unwrap_or_default();
    let mut parse = ComposerParse {
        conn,
        tally: &tally,
        cid,
        created,
        model,
        project,
        out: Vec::new(),
        events: Vec::new(),
        healthy: true,
        turn: 0,
        worked: false,
    };
    for h in &headers {
        parse.header(h);
    }
    (
        parse.out.into_iter().map(RawMessage::freeze).collect(),
        parse.events,
        parse.healthy,
    )
}

/// One composer's bubble walk: user bubbles open turns, visible AI prose joins the last turn
/// as its reply, and tool bubbles become events.
struct ComposerParse<'a> {
    conn: &'a Connection,
    tally: &'a Tally,
    cid: &'a str,
    created: i64,
    model: String,
    project: String,
    out: Vec<RawMessage>,
    events: Vec<Event>,
    healthy: bool,
    turn: u32,
    // bubbles are in order: a tool bubble sets the flag the next prose bubble consumes
    worked: bool,
}

impl ComposerParse<'_> {
    fn header(&mut self, h: &Value) {
        let Some(bid) = h.get("bubbleId").and_then(|b| b.as_str()) else {
            return;
        };
        let cid = self.cid;
        let raw = match kv_text_checked(self.conn, "cursorDiskKV", &format!("bubbleId:{cid}:{bid}"))
        {
            Ok(Some(raw)) => raw,
            Ok(None) => return,
            Err(error) => {
                self.tally.error(&format!("bubble query: {error}"));
                self.healthy = false;
                return;
            }
        };
        self.tally.seen();
        let b: Value = match serde_json::from_str(&raw) {
            Ok(b) => b,
            Err(e) => {
                self.tally
                    .error(&format!("{e}: {}", crate::intake::clip(&raw, 80)));
                self.healthy = false;
                return;
            }
        };
        let ts = match b.get("createdAt").and_then(|t| t.as_str()) {
            Some(s) => crate::ingest::parse_timestamp::rfc3339(Some(s)),
            None => self.created,
        };
        let text = b.get("text").and_then(|t| t.as_str()).unwrap_or("");
        match b.get("type").and_then(|t| t.as_i64()) {
            Some(1) => self.user_bubble(text, ts),
            Some(2) => self.ai_bubble(&b, text, bid, ts),
            _ => self.tally.skip(Skip::NonHuman),
        }
    }

    fn user_bubble(&mut self, text: &str, ts: i64) {
        if text.trim().is_empty() {
            self.tally.skip(Skip::EmptyText);
            return;
        }
        if is_wrapper(text) {
            self.tally.skip(Skip::Wrapper);
            return;
        }
        self.tally.row();
        self.worked = false;
        self.out.push(RawMessage {
            agent: "cursor",
            project: self.project.clone(),
            session: self.cid.to_string(),
            ts: if ts > 0 { ts } else { self.created },
            turn: self.turn,
            text: text.to_string(),
            model: self.model.clone(),
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }

    fn ai_bubble(&mut self, b: &Value, text: &str, bid: &str, ts: i64) {
        // isThought bubbles are hidden reasoning: indexing them would let search
        // hit chain-of-thought that never appears in chat.
        let visible_text = !b
            .get("isThought")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
            && !text.trim().is_empty();
        let mut consumed = visible_text;
        // a bubble's prose precedes its own call: the call joins it to the next prose
        if visible_text {
            if let Some(last) = self.out.last_mut() {
                crate::ingest::append_reply(last, text);
                crate::ingest::ReplyOrder::joined_known(&mut last.reply_shape, self.worked);
                self.worked = false;
            }
        }
        if let Some(tf) = b.get("toolFormerData").filter(|tf| tf.is_object()) {
            consumed = true;
            self.tally.event();
            let event = tool_event(self.cid, bid, ts, tf);
            self.worked |= crate::ingest::is_work_event(&event);
            self.events.push(event);
        }
        if consumed {
            self.tally.agent_row();
        } else {
            self.tally.skip(Skip::NonHuman);
        }
    }
}
