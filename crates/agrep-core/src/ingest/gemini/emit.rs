//! A session's transcript as rows and tool events.

use serde_json::Value;

use super::content::{
    carries_function_response, is_injected_context, is_state_snapshot, part_text, prompt_text,
    visible_text, COMPRESSION_ACK, INTERRUPTED_PLACEHOLDER,
};
use super::discover::Place;
use crate::ingest::{
    cap_event_output, is_wrapper, parse_timestamp, summarize_tool_input_with_chars,
};
use crate::intake::{Skip, Tally};
use crate::model::{Event, Message, RawMessage};

/// One session's messages as rows and events; a message flagged `true` only repeats an earlier
/// one and a message flagged `true` second carries a history copy's parts (see `is_history_copy`).
/// Callers have already counted every message as seen; each one here lands in exactly one row,
/// agent row or named skip.
pub(super) fn emit_messages<'a>(
    session: &str,
    place: &Place,
    messages: impl IntoIterator<Item = (&'a Value, bool, bool)>,
    tally: &Tally,
) -> (Vec<Message>, Vec<Event>) {
    let mut emission = Emission {
        session,
        place,
        tally,
        out: Vec::new(),
        events: Vec::new(),
        recap_turns: Vec::new(),
        turn: 0,
        worked: false,
    };
    for (message_ordinal, (m, replay, copied)) in messages.into_iter().enumerate() {
        if replay {
            tally.skip(Skip::Replay);
            continue;
        }
        let ty = m.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let ts = parse_timestamp::rfc3339(m.get("timestamp").and_then(|t| t.as_str()));
        match ty {
            "user" => emission.user(m, ts),
            "gemini" => emission.model(m, ts, copied, message_ordinal),
            "info" => tally.skip(Skip::Meta),
            _ => tally.skip(Skip::NonHuman),
        }
    }
    emission.finish()
}

struct Emission<'a> {
    session: &'a str,
    place: &'a Place,
    tally: &'a Tally,
    out: Vec<RawMessage>,
    events: Vec<Event>,
    recap_turns: Vec<u32>,
    turn: u32,
    /// A message's prose precedes its calls: the calls join it to the next block.
    worked: bool,
}

impl Emission<'_> {
    /// A prompt, or a compression's snapshot (a recap row); tool results and injected context
    /// are nobody's words.
    fn user(&mut self, m: &Value, ts: i64) {
        let content = m.get("content").unwrap_or(&Value::Null);
        if carries_function_response(content) {
            self.tally.skip(Skip::NonHuman);
            return;
        }
        let text = prompt_text(m, content);
        if text.trim().is_empty() {
            self.tally.skip(Skip::EmptyText);
            return;
        }
        if is_wrapper(&text) || is_injected_context(&text) {
            self.tally.skip(Skip::Wrapper);
            return;
        }
        self.tally.row();
        if is_state_snapshot(&text) {
            self.recap_turns.push(self.turn);
        } else {
            self.worked = false;
        }
        self.out.push(RawMessage {
            agent: "gemini",
            project: self.place.project.clone(),
            session: self.session.to_string(),
            ts,
            turn: self.turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: !self.place.parent.is_empty(),
            parent: self.place.parent.clone(),
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }

    /// The model's turn: its prose reply joins the user turn it answers (thoughts are reasoning,
    /// excluded) and its calls become tool events.
    fn model(&mut self, m: &Value, ts: i64, copied: bool, message_ordinal: usize) {
        // a history copy's raw stream reads as upstream shows it, a recorded reply as saved
        let txt = m.get("content").map(part_text).unwrap_or_default();
        let txt = if copied { visible_text(&txt) } else { txt };
        // canned CLI text: a copy may land away from the snapshot it acknowledged
        let canned = m.get("toolCalls").is_none()
            && matches!(txt.trim(), COMPRESSION_ACK | INTERRUPTED_PLACEHOLDER);
        if canned {
            self.tally.skip(Skip::Meta);
            return;
        }
        self.attach_reply(&txt);
        self.attach_model(m);
        if let Some(calls) = m.get("toolCalls").and_then(|c| c.as_array()) {
            for (call_ordinal, tc) in calls.iter().enumerate() {
                self.tool_call(tc, ts, message_ordinal, call_ordinal);
            }
        }
        self.tally.agent_row();
    }

    fn attach_reply(&mut self, txt: &str) {
        if txt.trim().is_empty() {
            return;
        }
        if let Some(last) = self.out.last_mut() {
            crate::ingest::append_reply(last, txt);
            crate::ingest::ReplyOrder::joined_known(&mut last.reply_shape, self.worked);
            self.worked = false;
        }
    }

    /// The first model that answers a row names it; a recap row has none.
    fn attach_model(&mut self, m: &Value) {
        let Some(last) = self.out.last_mut() else {
            return;
        };
        let recap = self.recap_turns.last() == Some(&last.turn);
        if !last.model.is_empty() || recap {
            return;
        }
        if let Some(md) = m.get("model").and_then(|v| v.as_str()) {
            if !md.is_empty() {
                last.model = md.to_string();
            }
        }
    }

    fn tool_call(&mut self, tc: &Value, ts: i64, message_ordinal: usize, call_ordinal: usize) {
        let name = tc.get("name").and_then(|n| n.as_str()).unwrap_or("?");
        self.worked |= !crate::ingest::is_bookkeeping_tool(name);
        let (input, input_chars) = tc
            .get("args")
            .map(summarize_tool_input_with_chars)
            .unwrap_or_default();
        let (output, output_chars, output_bytes, ok) = tool_result(tc);
        let child = delegated_session(tc);
        let meta = delegation_meta(tc, &child);
        self.tally.event();
        self.events.push(Event {
            turn: None,
            agent: "gemini",
            session: self.session.to_string(),
            ts,
            kind: "tool",
            name: name.to_string(),
            input,
            output,
            input_chars,
            output_chars,
            output_bytes,
            ok,
            call_id: tc
                .get("id")
                .and_then(|i| i.as_str())
                .filter(|id| !id.trim().is_empty())
                .map(str::to_string)
                .unwrap_or_else(|| format!("gemini:{message_ordinal}:{call_ordinal}")),
            child_session: child,
            meta,
            state: crate::ingest::task_state(name, tc.get("args")),
        });
    }

    fn finish(self) -> (Vec<Message>, Vec<Event>) {
        let recap_turns = self.recap_turns;
        let messages = self
            .out
            .into_iter()
            .map(|raw| {
                let recap = recap_turns.binary_search(&raw.turn).is_ok();
                let mut message = raw.freeze();
                if recap {
                    message.who = "recap".into();
                    message.model_source = "recap".into();
                }
                message
            })
            .collect();
        (messages, self.events)
    }
}

/// An invoke_agent record names the subagent it ran (geminiChat.ts recordCompletedToolCalls at
/// fb972b2): that agent's own session.
fn delegated_session(tc: &Value) -> String {
    tc.get("agentId")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|id| !id.is_empty())
        .map(str::to_string)
        .unwrap_or_default()
}

/// The call completes only once the subagent's run ended, so its status is the run's.
fn delegation_meta(tc: &Value, child: &str) -> String {
    if child.is_empty() {
        return String::new();
    }
    let status = match tc.get("status").and_then(Value::as_str) {
        Some("success") => "completed".to_string(),
        Some(status) => status.to_string(),
        None => "unknown".to_string(),
    };
    format!("delegation={status}")
}

/// `result[*].functionResponse.response.output` of one result part.
fn function_output(part: &Value) -> Option<&str> {
    part.get("functionResponse")?
        .get("response")?
        .get("output")?
        .as_str()
}

/// A toolCall's captured output + outcome: `result[*].functionResponse.response.output`,
/// with `status` giving the ok signal when the store recorded one. `result` is a
/// PartListUnion, so a bare string or a single part is accepted too.
fn tool_result(tc: &Value) -> (String, usize, usize, Option<bool>) {
    let outputs: Vec<&str> = match tc.get("result") {
        Some(Value::String(text)) => vec![text.as_str()],
        Some(Value::Array(parts)) => parts.iter().filter_map(function_output).collect(),
        Some(part @ Value::Object(_)) => function_output(part).into_iter().collect(),
        _ => Vec::new(),
    };
    let ok = match tc.get("status").and_then(|s| s.as_str()) {
        Some("success") => Some(true),
        Some("error") | Some("failed") => Some(false),
        _ => None,
    };
    let (output, output_chars, output_bytes) = cap_event_output(&outputs.join("\n"));
    (output, output_chars, output_bytes, ok)
}
