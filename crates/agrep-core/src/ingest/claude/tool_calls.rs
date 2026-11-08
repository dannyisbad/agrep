//! Tool calls: the metadata a tool_use carries and what its paired tool_result adds,
//! including the agent run a Task/Agent or SendMessage call names.

use serde::Deserialize;
use serde_json::value::RawValue;

use super::notifications::notification_token;
use super::records::{joined_text_blocks, Line};
use crate::ingest::cap_event_output;
use crate::model::Event;

/// The event kind of a tool_use block named `name`.
pub(super) fn tool_kind(name: &str) -> &'static str {
    if name == "Task" || name == "Agent" {
        "subagent_start"
    } else {
        "tool"
    }
}

/// The side session a stored agent id names: Claude Code writes its transcript as
/// `agent-<id>.jsonl`. None for an id that could not be a plain file stem.
fn agent_session(agent_id: &str) -> Option<String> {
    let id = notification_token(Some(agent_id))?;
    id.bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        .then(|| format!("agent-{id}"))
}

/// The `meta` token an agent call's or a message's own input carries: the name that makes the
/// spawned agent addressable, or the recipient SendMessage was given.
pub(super) fn call_meta(name: &str, input: Option<&serde_json::Value>) -> String {
    let key = match name {
        "Task" | "Agent" => "name",
        "SendMessage" => "to",
        _ => return String::new(),
    };
    notification_token(
        input
            .and_then(|input| input.get(key))
            .and_then(|value| value.as_str()),
    )
    .map(|token| format!("{key}={token}"))
    .unwrap_or_default()
}

/// The `toolUseResult` fields that tie an agent call or a message to an agent's run. Raw, so one
/// unexpected shape leaves the other fields readable.
#[derive(Deserialize)]
struct AgentOutcome<'a> {
    #[serde(borrow)]
    status: Option<&'a RawValue>,
    #[serde(rename = "agentId", borrow)]
    agent_id: Option<&'a RawValue>,
    #[serde(rename = "resumedAgentId", borrow)]
    resumed_agent_id: Option<&'a RawValue>,
    #[serde(rename = "inlineHandback", borrow)]
    inline_handback: Option<&'a RawValue>,
    #[serde(borrow)]
    message: Option<&'a RawValue>,
}

fn push_meta(event: &mut Event, token: &str) {
    if !event.meta.is_empty() {
        event.meta.push(' ');
    }
    event.meta.push_str(token);
}

/// Record, uncapped, the agent run a Task/Agent or SendMessage result names: `child_session` is
/// that agent's transcript, and `meta` gains the call's status or the message's inline hand-back.
fn apply_agent_outcome(event: &mut Event, raw: &RawValue) {
    let Ok(outcome) = serde_json::from_str::<AgentOutcome>(raw.get()) else {
        return;
    };
    let text = |value: Option<&RawValue>| {
        value.and_then(|value| serde_json::from_str::<String>(value.get()).ok())
    };
    let agent_id = if event.kind == "subagent_start" {
        if let Some(status) = notification_token(text(outcome.status).as_deref()) {
            push_meta(event, &format!("status={status}"));
        }
        text(outcome.agent_id)
    } else {
        // 2.1.289 marks an inline result with `inlineHandback` and 2.1.199 in `message`; 2.1.240's
        // `Resumed agent <id or name>. Result:` is left to summary's reading of the display text
        let inline = outcome.inline_handback.is_some()
            || text(outcome.message)
                .is_some_and(|message| message.contains("ran to completion. Result:"));
        if inline {
            push_meta(event, "handback=inline");
        }
        text(outcome.resumed_agent_id)
    };
    if let Some(session) = agent_id.as_deref().and_then(agent_session) {
        event.child_session = session;
    }
}

/// Text of a tool_result's `content`: a plain string, or an array of text blocks.
fn tool_result_full_text(content: &serde_json::Value) -> String {
    match content {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(blocks) => joined_text_blocks(blocks),
        _ => String::new(),
    }
}

fn tool_result_text(content: &serde_json::Value) -> (String, usize, usize) {
    match content {
        serde_json::Value::String(s) => cap_event_output(s),
        serde_json::Value::Array(_) => cap_event_output(&tool_result_full_text(content)),
        _ => (String::new(), 0, 0),
    }
}

/// Fill a pending call's event from its `tool_result` block on row `line`. `lone`: the row
/// carries only this result, so its `toolUseResult` belongs to this call.
pub(super) fn apply_tool_result(
    event: &mut Event,
    block: &serde_json::Value,
    line: &Line,
    lone: bool,
) {
    if let Some(c) = block.get("content") {
        (event.output, event.output_chars, event.output_bytes) = tool_result_text(c);
        if event.name == "TaskList" {
            event.state = crate::ingest::task_listing(&tool_result_full_text(c));
        }
    }
    event.ok = Some(block.get("is_error").and_then(|v| v.as_bool()) != Some(true));
    if event.kind == "subagent_start" || event.name == "SendMessage" {
        if let Some(raw) = line.tool_use_result.filter(|_| lone) {
            apply_agent_outcome(event, raw);
        }
        // the result row is written once the call returned, after any run
        // it waited on had ended
        let returned = line.ts();
        if returned > 0 {
            push_meta(event, &format!("returned={returned}"));
        }
    }
}
