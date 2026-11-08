//! One pi JSONL record as the parser keeps it: the fields it reads, with
//! unbounded text capped and measured once so a checkpoint never stores it twice.

use serde_json::Value;

use crate::ingest::{cap_event_output, summarize_tool_input_with_chars};

/// A record from an incremental read: decoded, or kept raw so the parse can
/// count and sample it exactly as a full read would.
#[derive(Clone)]
pub(crate) enum PreparedRecord {
    Value(Value),
    Malformed {
        error: String,
        raw: Box<crate::ingest::jsonl::RawRecord>,
    },
}
impl PreparedRecord {
    pub(crate) fn value(&self) -> Option<&Value> {
        match self {
            Self::Value(value) => Some(value),
            Self::Malformed { .. } => None,
        }
    }
    pub(crate) fn into_value(self) -> Option<Value> {
        match self {
            Self::Value(value) => Some(value),
            Self::Malformed { .. } => None,
        }
    }
    pub(crate) fn encoded_len(&self) -> serde_json::Result<usize> {
        match self {
            Self::Value(value) => serde_json::to_vec(value).map(|v| v.len() + 1),
            Self::Malformed { raw, .. } => Ok(raw.len() + 1),
        }
    }
}

pub(super) fn text_of(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(blocks)) => {
            let mut out = String::new();
            for block in blocks {
                if block.get("type").and_then(|t| t.as_str()) != Some("text") {
                    continue;
                }
                if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                    if text.is_empty() {
                        continue;
                    }
                    if !out.is_empty() {
                        out.push('\n');
                    }
                    out.push_str(text);
                }
            }
            out
        }
        _ => String::new(),
    }
}

pub(super) fn is_role(message: &Value, role: &str) -> bool {
    message.get("role").and_then(|r| r.as_str()) == Some(role)
}

/// Output chars and bytes a compacted message measured before capping its text.
pub(super) fn precapped(message: &Value) -> Option<(usize, usize)> {
    let chars = message.get("__agrep_chars").and_then(|v| v.as_u64())?;
    let bytes = message.get("__agrep_bytes").and_then(|v| v.as_u64())?;
    Some((chars as usize, bytes as usize))
}

/// Retain native graph records without duplicating unbounded ignored/thinking/tool text.
pub fn compact_record(mut v: Value) -> Value {
    if let Some(m) = v.get_mut("message") {
        compact_message(m);
    }
    if let Some(fields) = v.as_object_mut() {
        fields.retain(|key, _| keep_record_field(key));
    }
    v
}

fn compact_message(m: &mut Value) {
    if is_role(m, "user") && m["synthetic"] == true {
        m["content"] = Value::Null;
    }
    if is_role(m, "bashExecution") && m.get("__agrep_chars").is_none() {
        let capped = cap_event_output(m.get("output").and_then(|v| v.as_str()).unwrap_or_default());
        store_capped(m, "output", capped);
    }
    if is_role(m, "toolResult") && m.get("__agrep_chars").is_none() {
        let capped = cap_event_output(&text_of(m.get("content")));
        store_capped(m, "content", capped);
    } else if let Some(blocks) = m.get_mut("content").and_then(|v| v.as_array_mut()) {
        compact_blocks(blocks);
    }
    if let Some(fields) = m.as_object_mut() {
        fields.retain(|key, _| keep_message_field(key));
    }
}

fn store_capped(m: &mut Value, field: &str, (text, chars, bytes): (String, usize, usize)) {
    m[field] = serde_json::json!(text);
    m["__agrep_chars"] = serde_json::json!(chars);
    m["__agrep_bytes"] = serde_json::json!(bytes);
}

/// Keep text and tool calls; a tool call keeps its summarized input, not its arguments.
fn compact_blocks(blocks: &mut Vec<Value>) {
    blocks.retain(|b| {
        matches!(
            b.get("type").and_then(|v| v.as_str()),
            Some("text" | "toolCall")
        )
    });
    for block in blocks {
        if block["type"] == "toolCall" && block.get("__agrep_input").is_none() {
            summarize_tool_call(block);
        }
        if let Some(fields) = block.as_object_mut() {
            fields.retain(|key, _| keep_block_field(key));
        }
    }
}

fn summarize_tool_call(block: &mut Value) {
    let (input, chars) = block
        .get("arguments")
        .map(summarize_tool_input_with_chars)
        .unwrap_or_default();
    let state = crate::ingest::task_state(
        block["name"].as_str().unwrap_or("?"),
        block.get("arguments"),
    );
    block["__agrep_input"] = serde_json::json!(input);
    block["__agrep_input_chars"] = serde_json::json!(chars);
    block["__agrep_state"] = serde_json::json!(state);
}

fn keep_block_field(key: &str) -> bool {
    matches!(
        key,
        "type" | "text" | "id" | "name" | "__agrep_input" | "__agrep_input_chars" | "__agrep_state"
    )
}

pub(super) fn keep_message_field(key: &str) -> bool {
    matches!(
        key,
        "role"
            | "content"
            | "model"
            | "timestamp"
            | "synthetic"
            | "toolCallId"
            | "isError"
            | "command"
            | "output"
            | "cancelled"
            | "exitCode"
            | "__agrep_chars"
            | "__agrep_bytes"
    )
}

pub(super) fn keep_record_field(key: &str) -> bool {
    matches!(
        key,
        "type"
            | "id"
            | "parentId"
            | "timestamp"
            | "version"
            | "cwd"
            | "modelId"
            | "model"
            | "summary"
            | "message"
            | "__agrep_ordinal"
    )
}
