//! `message` records by role: user prompts, assistant replies and their tool
//! calls, and pi's own bash executions.

use serde_json::Value;

use super::super::branch::{Record, ToolResult};
use super::super::record::{is_role, precapped, text_of};
use super::Parser;
use crate::ingest::{
    cap_event_output, is_wrapper, parse_timestamp, summarize_tool_input_with_chars,
};
use crate::intake::Skip;
use crate::model::Event;

impl Parser<'_> {
    pub(super) fn message(&mut self, record: &Record<'_>) {
        let Some(message) = record.value.get("message") else {
            self.tally.skip(Skip::NonHuman);
            return;
        };
        let ts = parse_timestamp::rfc3339(
            message
                .get("timestamp")
                .and_then(|t| t.as_str())
                .or_else(|| record.value.get("timestamp").and_then(|t| t.as_str())),
        );
        if is_role(message, "user") {
            self.user(message, ts);
        } else if is_role(message, "assistant") {
            self.assistant(record.ordinal, message, ts);
        } else if is_role(message, "bashExecution") {
            self.bash_execution(record, message, ts);
        } else {
            // toolResult rows were consumed by tool_results; developer/custom
            // roles are the harness talking to itself, never the user
            self.tally.skip(Skip::NonHuman);
        }
    }

    fn user(&mut self, message: &Value, ts: i64) {
        // Sidecar streams (advisor and friends) mirror the watched session
        // as user-role records flagged `synthetic: true`: transcript copies
        // already indexed at their source, so sidechain, never prompts.
        if message
            .get("synthetic")
            .and_then(|s| s.as_bool())
            .unwrap_or(false)
        {
            self.mirrored_anchor = true;
            self.tally.skip(Skip::Sidechain);
            return;
        }
        self.mirrored_anchor = false;
        let text = text_of(message.get("content"));
        if text.trim().is_empty() {
            self.tally.skip(Skip::EmptyText);
            return;
        }
        if is_wrapper(&text) {
            self.tally.skip(Skip::Wrapper);
            return;
        }
        self.tally.row();
        self.order.prompt(&mut self.events);
        self.push_row(ts, text);
    }

    fn assistant(&mut self, ordinal: usize, message: &Value, ts: i64) {
        if let Some(named) = message.get("model").and_then(|m| m.as_str()) {
            if !named.is_empty() {
                self.model = named.to_string();
            }
        }
        let reply = text_of(message.get("content"));
        // A sidecar assistant whose anchor was skipped as a synthetic
        // mirror has no prompt row to reply to: that text is the
        // sidecar's own voice (advisor advisories) and becomes a row.
        if self.side && self.mirrored_anchor {
            self.sidecar_voice(ts, reply);
        } else {
            self.join_reply(&reply);
        }
        let blocks = message
            .get("content")
            .and_then(|c| c.as_array())
            .map(Vec::as_slice)
            .unwrap_or_default();
        for (call_ordinal, block) in blocks.iter().enumerate() {
            if block.get("type").and_then(|t| t.as_str()) != Some("toolCall") {
                continue;
            }
            self.tool_call(block, ordinal, call_ordinal, ts);
        }
    }

    fn sidecar_voice(&mut self, ts: i64, reply: String) {
        if reply.trim().is_empty() {
            self.tally.skip(Skip::EmptyText);
            return;
        }
        self.tally.row();
        self.push_row(ts, reply);
    }

    /// The reply joins the latest row, which also takes the model if it has none.
    fn join_reply(&mut self, reply: &str) {
        if let Some(last) = self.out.last_mut() {
            if !reply.trim().is_empty() {
                let chars = crate::ingest::append_reply(last, reply);
                if chars > 0 {
                    self.order
                        .joined(&mut last.reply_shape, &self.events, self.events.len());
                    self.order.reply(self.events.len());
                }
            }
            if last.model.is_empty() {
                last.model = self.model.clone();
            }
        }
        self.tally.agent_row();
    }

    fn tool_call(&mut self, block: &Value, ordinal: usize, call_ordinal: usize, ts: i64) {
        let agent = self.agent;
        let name = block.get("name").and_then(|n| n.as_str()).unwrap_or("?");
        let (input, input_chars) = tool_input(block);
        let state = block
            .get("__agrep_state")
            .and_then(|v| v.as_str())
            .map(str::to_owned)
            .unwrap_or_else(|| crate::ingest::task_state(name, block.get("arguments")));
        let call_id = block
            .get("id")
            .and_then(|i| i.as_str())
            .filter(|id| !id.trim().is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| format!("{agent}:{ordinal}:{call_ordinal}"));
        let ToolResult {
            output,
            ok,
            chars: output_chars,
            bytes: output_bytes,
        } = self.results.get(&call_id).cloned().unwrap_or_default();
        if !self.results.contains_key(&call_id) {
            self.pending.insert(call_id.clone(), self.events.len());
        }
        self.tally.event();
        self.events.push(Event {
            turn: self.out.last().map(|m| m.turn),
            agent,
            session: self.session.clone(),
            ts,
            kind: "tool",
            name: name.to_string(),
            input,
            output,
            input_chars,
            output_chars,
            output_bytes,
            ok,
            call_id,
            child_session: String::new(),
            meta: String::new(),
            state,
        });
    }

    /// pi runs a bash command as its own role, not a toolCall:
    /// no content, the command and output are message fields.
    fn bash_execution(&mut self, record: &Record<'_>, message: &Value, ts: i64) {
        let agent = self.agent;
        let command = message
            .get("command")
            .and_then(|c| c.as_str())
            .unwrap_or_default();
        let raw_output = message
            .get("output")
            .and_then(|o| o.as_str())
            .unwrap_or_default();
        let (output, output_chars, output_bytes) = match precapped(message) {
            Some((chars, bytes)) => (raw_output.to_owned(), chars, bytes),
            None => cap_event_output(raw_output),
        };
        let cancelled = message
            .get("cancelled")
            .and_then(|c| c.as_bool())
            .unwrap_or(false);
        let ok = message
            .get("exitCode")
            .and_then(Value::as_i64)
            .map(|code| code == 0 && !cancelled);
        self.tally.event();
        self.events.push(Event {
            turn: self.out.last().map(|m| m.turn),
            agent,
            session: self.session.clone(),
            ts,
            kind: "tool",
            name: "bash".to_string(),
            input: command.to_string(),
            output,
            input_chars: command.chars().count(),
            output_chars,
            output_bytes,
            ok,
            call_id: record
                .value
                .get("id")
                .and_then(|i| i.as_str())
                .map(str::to_string)
                .unwrap_or_else(|| format!("{agent}:bash:{}", record.ordinal)),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        });
        // the user ran this (`!cmd`): never the agent's unfinished work
        self.order.exclude(self.events.len() - 1);
        self.tally.agent_row();
    }
}

/// The call's summarized input: precomputed by `compact_record`, else summarized here.
fn tool_input(block: &Value) -> (String, usize) {
    if let Some(input) = block.get("__agrep_input").and_then(|v| v.as_str()) {
        return (
            input.to_owned(),
            block["__agrep_input_chars"].as_u64().unwrap_or(0) as usize,
        );
    }
    block
        .get("arguments")
        .map(summarize_tool_input_with_chars)
        .unwrap_or_default()
}
