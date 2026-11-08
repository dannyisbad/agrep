//! Tool-call records: input summaries, outcomes and output payloads.

use serde_json::value::RawValue;

use super::records::Payload;
use crate::ingest::{
    cap_event_output, cap_str_with_chars, summarize_tool_input_with_chars, EVENT_CAP,
};

/// A call's summarized input plus what that input reveals about the call.
pub(super) struct CallInput {
    pub(super) input: String,
    pub(super) input_chars: usize,
    /// An encrypted `send_message` handoff: control traffic rather than a tool call.
    pub(super) control: bool,
    pub(super) task_state: String,
}

/// function_call carries `arguments` as a JSON string; custom_tool_call carries `input`
/// as a raw string (e.g. an apply_patch body).
pub(super) fn call_input(name: &str, payload: &Payload<'_>) -> CallInput {
    if let Some(raw) = payload.arguments {
        return arguments_input(name, raw);
    }
    let Some(raw) = payload.input.as_deref() else {
        return CallInput {
            input: String::new(),
            input_chars: 0,
            control: false,
            task_state: String::new(),
        };
    };
    let control = serde_json::from_str(raw)
        .ok()
        .is_some_and(|value| ciphertext_send_message(name, &value));
    let (input, input_chars) = cap_str_with_chars(raw, EVENT_CAP);
    CallInput {
        input,
        input_chars,
        control,
        task_state: String::new(),
    }
}

fn arguments_input(name: &str, raw: &RawValue) -> CallInput {
    let args_val: serde_json::Value = serde_json::from_str(raw.get()).unwrap_or_default();
    let control = ciphertext_send_message(name, &args_val);
    let mut task_state = String::new();
    let (input, input_chars) = match args_val {
        serde_json::Value::String(inner) => serde_json::from_str::<serde_json::Value>(&inner)
            .map(|v| {
                task_state = crate::ingest::task_state(name, Some(&v));
                summarize_tool_input_with_chars(&v)
            })
            .unwrap_or_else(|_| cap_str_with_chars(&inner, EVENT_CAP)),
        v => {
            task_state = crate::ingest::task_state(name, Some(&v));
            summarize_tool_input_with_chars(&v)
        }
    };
    CallInput {
        input,
        input_chars,
        control,
        task_state,
    }
}

pub(super) fn call_status(status: Option<&str>) -> Option<bool> {
    match status {
        Some("completed") => Some(true),
        Some("failed") | Some("error") => Some(false),
        _ => None,
    }
}

/// A web_search_call's `action.query`, else its `action.queries` joined.
pub(super) fn web_search_query(action: Option<&RawValue>) -> String {
    let action_val = action.and_then(|r| serde_json::from_str::<serde_json::Value>(r.get()).ok());
    action_val
        .as_ref()
        .and_then(|a| a.get("query"))
        .and_then(|q| q.as_str())
        .map(str::to_string)
        .or_else(|| {
            action_val
                .as_ref()
                .and_then(|a| a.get("queries"))
                .and_then(|q| q.as_array())
                .map(|queries| {
                    queries
                        .iter()
                        .filter_map(|query| query.as_str())
                        .collect::<Vec<_>>()
                        .join(" · ")
                })
        })
        .unwrap_or_default()
}

/// The codex shell wrapper prints `Process exited with code N` into otherwise
/// unstructured output text -- the only outcome record many codex tool calls have.
fn sniff_exit_code(s: &str) -> Option<bool> {
    const MARK: &str = "Process exited with code ";
    let i = s.find(MARK)?;
    let digits: String = s[i + MARK.len()..]
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    if digits.is_empty() {
        None
    } else {
        Some(digits == "0")
    }
}

fn ciphertext_token(value: &str) -> bool {
    const FERNET_PREFIX: &[u8] = b"gAAAAAB";
    const ENTROPY_MIN_LEN: usize = 512;
    const ENTROPY_MIN_BITS: f64 = 5.75;

    let token = value.trim();
    let padding = token.bytes().rev().take_while(|byte| *byte == b'=').count();
    if padding > 2 || token.len() < 96 {
        return false;
    }
    let body = &token.as_bytes()[..token.len() - padding];
    if !body
        .iter()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'+' | b'/'))
    {
        return false;
    }
    if body.starts_with(FERNET_PREFIX) {
        return true;
    }
    if body.len() < ENTROPY_MIN_LEN {
        return false;
    }

    let mut counts = [0u32; 256];
    for byte in body {
        counts[*byte as usize] += 1;
    }
    let len = body.len() as f64;
    let entropy = counts
        .iter()
        .filter(|count| **count != 0)
        .map(|count| {
            let probability = f64::from(*count) / len;
            -probability * probability.log2()
        })
        .sum::<f64>();
    entropy >= ENTROPY_MIN_BITS
}

fn ciphertext_send_message(name: &str, input: &serde_json::Value) -> bool {
    if name != "send_message" {
        return false;
    }
    match input {
        serde_json::Value::Object(fields) => fields
            .get("message")
            .and_then(serde_json::Value::as_str)
            .is_some_and(ciphertext_token),
        serde_json::Value::String(inner) => serde_json::from_str(inner)
            .ok()
            .is_some_and(|decoded| ciphertext_send_message(name, &decoded)),
        _ => false,
    }
}

/// A function_call_output's `output`: usually a plain string, which is sometimes itself
/// JSON `{"output": "...", "metadata": {"exit_code": 0}}`. Returns (text, ok-if-known).
pub(super) fn parse_call_output(v: &serde_json::Value) -> (String, usize, usize, Option<bool>) {
    let from_obj = |o: &serde_json::Map<String, serde_json::Value>| {
        let text = o
            .get("output")
            .and_then(|x| x.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| serde_json::Value::Object(o.clone()).to_string());
        let ok = o
            .get("metadata")
            .and_then(|m| m.get("exit_code"))
            .and_then(|c| c.as_i64())
            .map(|c| c == 0)
            .or_else(|| sniff_exit_code(&text));
        (text, ok)
    };
    match v {
        serde_json::Value::String(s) => {
            let t = s.trim_start();
            if t.starts_with('{') {
                if let Ok(serde_json::Value::Object(o)) = serde_json::from_str(t) {
                    let (text, ok) = from_obj(&o);
                    let (text, chars, bytes) = cap_event_output(&text);
                    return (text, chars, bytes, ok);
                }
            }
            let (text, chars, bytes) = cap_event_output(s);
            (text, chars, bytes, sniff_exit_code(s))
        }
        serde_json::Value::Object(o) => {
            let (text, ok) = from_obj(o);
            let (text, chars, bytes) = cap_event_output(&text);
            (text, chars, bytes, ok)
        }
        serde_json::Value::Null => (String::new(), 0, 0, None),
        other => {
            let (text, chars, bytes) = cap_event_output(&other.to_string());
            (text, chars, bytes, None)
        }
    }
}
