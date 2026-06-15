//! Codex CLI adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
//! One JSONL file per session. The first line is `session_meta` (gives cwd -> project,
//! and the session id). Real human turns are `response_item` lines with
//! payload.type=="message" and payload.role=="user", text in `input_text`/`text` blocks.
//!
//! Codex injects a large first "user" turn (AGENTS.md / environment / instructions) plus
//! a stream of system-authored `role:user` notifications (turn_aborted, subagent_notification,
//! goal_context, delegations, image-paste markers). None of those are the user's words, so we
//! drop them and keep only what he actually typed. Prefer under-including over mislabeling.
//!
//! NOTE: only `~/.codex/sessions/` is walked. `~/.codex/.tmp/**` (plugin test fixtures),
//! `~/.codex/history.jsonl`, and `~/.codex/archived_sessions/` are intentionally not read.

use std::borrow::Cow;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

use memchr::memmem;
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::ingest::{
    cap_str, is_wrapper, project_name, summarize_tool_input, ts_millis, EVENT_CAP,
};
use crate::model::{Event, Message};

// Borrowed deserialization (see claude.rs for the soundness argument): Cow scalars
// borrow from the line, RawValue defers the output/action DOMs until a call actually
// pairs up. The lines that dominate rollout bytes (reasoning items with encrypted
// blobs) never reach serde at all - parse_file's prefilter rejects them at memchr speed.
#[derive(Deserialize)]
struct Line<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    #[serde(borrow)]
    payload: Option<Payload<'a>>,
}

#[derive(Deserialize)]
struct Payload<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    role: Option<Cow<'a, str>>,
    #[serde(borrow)]
    content: Option<Vec<Block<'a>>>,
    // session_meta fields
    #[serde(borrow)]
    id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    cwd: Option<Cow<'a, str>>,
    // turn_context carries the active model (e.g. "gpt-5.3-codex-spark").
    #[serde(borrow)]
    model: Option<Cow<'a, str>>,
    // tool-call fields: function_call {name, arguments(JSON-string), call_id};
    // function_call_output {call_id, output}; custom_tool_call {name, input, status};
    // web_search_call {action:{query,...}}.
    #[serde(borrow)]
    name: Option<Cow<'a, str>>,
    #[serde(borrow)]
    arguments: Option<Cow<'a, str>>,
    #[serde(borrow)]
    call_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    output: Option<&'a RawValue>,
    #[serde(borrow)]
    input: Option<Cow<'a, str>>,
    #[serde(borrow)]
    status: Option<Cow<'a, str>>,
    #[serde(borrow)]
    action: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct Block<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    text: Option<Cow<'a, str>>,
}

/// Concatenate the human-authored text blocks (`input_text`, also accept `text`).
fn extract_text(blocks: &[Block<'_>]) -> Option<String> {
    let mut out = String::new();
    for b in blocks {
        match b.ty.as_deref() {
            Some("input_text") | Some("text") => {
                if let Some(t) = b.text.as_deref() {
                    if !out.is_empty() {
                        out.push('\n');
                    }
                    out.push_str(t);
                }
            }
            _ => {}
        }
    }
    if out.trim().is_empty() {
        None
    } else {
        Some(out)
    }
}

/// Concatenate the assistant's visible prose blocks (`output_text`, also accept `text`).
fn extract_assistant(blocks: &[Block<'_>]) -> Option<String> {
    let mut out = String::new();
    for b in blocks {
        match b.ty.as_deref() {
            Some("output_text") | Some("text") => {
                if let Some(t) = b.text.as_deref() {
                    if !out.is_empty() {
                        out.push('\n');
                    }
                    out.push_str(t);
                }
            }
            _ => {}
        }
    }
    if out.trim().is_empty() {
        None
    } else {
        Some(out)
    }
}

/// Codex-specific preambles and system-injected `role:user` notifications that are NOT
/// something the user typed. Checked in addition to the shared `is_wrapper`.
fn is_codex_injected(text: &str) -> bool {
    let t = text.trim_start();
    // Injected first-turn preamble (AGENTS.md / environment / instructions blocks).
    t.starts_with("# AGENTS.md")
        || t.starts_with("<environment_context")
        || t.starts_with("<INSTRUCTIONS")
        || t.starts_with("<permissions")
        || t.starts_with("<user_instructions")
        || t.contains("<user_instructions>")
        // System-authored notifications that arrive as role:user.
        || t.starts_with("<turn_aborted")
        || t.starts_with("<subagent_notification")
        || t.starts_with("<goal_context")
        || t.starts_with("<codex_internal_context")
        || t.starts_with("<codex_delegation")
        || t.starts_with("<realtime_delegation")
        || t.starts_with("<user_action")
        // Image-paste marker wrapper; the typed text is entangled with it, so skip to
        // avoid emitting the marker as the user's words.
        || t.starts_with("<image")
}

/// Derive a session id from a `rollout-<ISO>-<uuid>.jsonl` filename (fallback when the
/// `session_meta` line is missing its id).
fn session_from_filename(path: &Path) -> String {
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or_default();
    // The id is the trailing UUID (5 dash-separated groups). Take the last 5 segments.
    let parts: Vec<&str> = stem.split('-').collect();
    if parts.len() >= 5 {
        parts[parts.len() - 5..].join("-")
    } else {
        stem.to_string()
    }
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

/// A function_call_output's `output`: usually a plain string, which is sometimes itself
/// JSON `{"output": "...", "metadata": {"exit_code": 0}}`. Returns (text, ok-if-known).
fn parse_call_output(v: &serde_json::Value) -> (String, Option<bool>) {
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
                    return (cap_str(&text, EVENT_CAP), ok);
                }
            }
            (cap_str(s, EVENT_CAP), sniff_exit_code(s))
        }
        serde_json::Value::Object(o) => {
            let (text, ok) = from_obj(o);
            (cap_str(&text, EVENT_CAP), ok)
        }
        serde_json::Value::Null => (String::new(), None),
        other => (cap_str(&other.to_string(), EVENT_CAP), None),
    }
}

fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>) {
    let data = match fs::read_to_string(path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("  ! codex: cannot read {}: {e}", path.display());
            return (Vec::new(), Vec::new());
        }
    };

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    // call_id -> index into `events`, so the *_output line can pair up.
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;
    let mut project: Option<String> = None;
    let mut session: Option<String> = None;
    // The active model, updated by each turn_context line and stamped onto turns.
    let mut current_model = String::new();

    // Raw-byte prefilter (sound for the quoted key needles - see claude.rs
    // raw_str_value doc). Every line kind we consume is admitted by one of these:
    // messages carry `"role":`, tool calls/outputs carry `"call_id":`, plus the two
    // meta line types (and web_search_call, whose call_id is not guaranteed). The
    // reasoning items that dominate rollout bytes match none and are skipped without
    // touching serde; an unquoted-needle false positive just costs one wasted parse.
    let f_role = memmem::Finder::new(b"\"role\":");
    let f_callid = memmem::Finder::new(b"\"call_id\":");
    let f_meta = memmem::Finder::new(b"session_meta");
    let f_turnctx = memmem::Finder::new(b"turn_context");
    let f_websearch = memmem::Finder::new(b"web_search_call");
    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        let bytes = line.as_bytes();
        if f_role.find(bytes).is_none()
            && f_callid.find(bytes).is_none()
            && f_meta.find(bytes).is_none()
            && f_turnctx.find(bytes).is_none()
            && f_websearch.find(bytes).is_none()
        {
            continue;
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(_) => continue,
        };

        // Capture session metadata (project + session id) from the meta line.
        if l.ty.as_deref() == Some("session_meta") {
            if let Some(p) = &l.payload {
                if let Some(cwd) = p.cwd.as_deref() {
                    project = Some(project_name(cwd));
                }
                if let Some(id) = p.id.as_deref() {
                    if !id.is_empty() {
                        session = Some(id.to_string());
                    }
                }
            }
            continue;
        }
        // turn_context announces the model in force for the turns that follow.
        if l.ty.as_deref() == Some("turn_context") {
            if let Some(md) = l.payload.as_ref().and_then(|p| p.model.as_deref()) {
                if !md.is_empty() {
                    current_model = md.to_string();
                }
            }
            continue;
        }

        if l.ty.as_deref() != Some("response_item") {
            continue;
        }
        let payload = match &l.payload {
            Some(p) => p,
            None => continue,
        };
        // Tool stream -> events. (Reasoning stays skipped.)
        let sess = || {
            session
                .clone()
                .unwrap_or_else(|| session_from_filename(path))
        };
        match payload.ty.as_deref() {
            Some("function_call") | Some("custom_tool_call") => {
                let name = payload.name.as_deref().unwrap_or("?").to_string();
                // function_call carries `arguments` as a JSON string; custom_tool_call
                // carries `input` as a raw string (e.g. an apply_patch body).
                let input = if let Some(args) = payload.arguments.as_deref() {
                    serde_json::from_str::<serde_json::Value>(args)
                        .map(|v| summarize_tool_input(&v))
                        .unwrap_or_else(|_| cap_str(args, EVENT_CAP))
                } else {
                    payload
                        .input
                        .as_deref()
                        .map(|s| cap_str(s, EVENT_CAP))
                        .unwrap_or_default()
                };
                let ok = match payload.status.as_deref() {
                    Some("completed") => Some(true),
                    Some("failed") | Some("error") => Some(false),
                    _ => None,
                };
                let call_id = payload.call_id.as_deref().unwrap_or_default().to_string();
                if !call_id.is_empty() {
                    pending.insert(call_id.clone(), events.len());
                }
                events.push(Event {
                    agent: "codex",
                    session: sess(),
                    ts: ts_millis(l.timestamp.as_deref()),
                    kind: "tool",
                    name,
                    input,
                    output: String::new(),
                    ok,
                    call_id,
                    child_session: String::new(),
                });
                continue;
            }
            Some("function_call_output") | Some("custom_tool_call_output") => {
                if let Some(id) = payload.call_id.as_deref() {
                    if let Some(&i) = pending.get(id) {
                        let (text, ok) = payload
                            .output
                            .and_then(|r| serde_json::from_str::<serde_json::Value>(r.get()).ok())
                            .map(|v| parse_call_output(&v))
                            .unwrap_or_default();
                        events[i].output = text;
                        if ok.is_some() {
                            events[i].ok = ok;
                        }
                        // No fabricated Some(true) when the store records nothing: codex
                        // output is raw text without exit codes, and "arrived" != "worked".
                        // ok stays None => the stats layer reports it as not-recorded.
                        pending.remove(id);
                    }
                }
                continue;
            }
            Some("web_search_call") => {
                let action_val = payload
                    .action
                    .and_then(|r| serde_json::from_str::<serde_json::Value>(r.get()).ok());
                let query = action_val
                    .as_ref()
                    .and_then(|a| a.get("query"))
                    .and_then(|q| q.as_str())
                    .unwrap_or("");
                events.push(Event {
                    agent: "codex",
                    session: sess(),
                    ts: ts_millis(l.timestamp.as_deref()),
                    kind: "tool",
                    name: "web_search".to_string(),
                    input: cap_str(query, EVENT_CAP),
                    output: String::new(),
                    ok: None,
                    call_id: payload.call_id.as_deref().unwrap_or_default().to_string(),
                    child_session: String::new(),
                });
                continue;
            }
            Some("message") => {}
            _ => continue,
        }
        let blocks = match payload.content.as_deref() {
            Some(b) => b,
            None => continue,
        };

        // Assistant prose -> attach to the user message it answers.
        if payload.role.as_deref() == Some("assistant") {
            if let Some(txt) = extract_assistant(blocks) {
                if let Some(last) = out.last_mut() {
                    crate::ingest::append_capped(&mut last.reply, &txt, 1600);
                    if last.model.is_empty() && !current_model.is_empty() {
                        last.model = current_model.clone();
                    }
                }
            }
            continue;
        }
        // Only the human past here; skip developer / system.
        if payload.role.as_deref() != Some("user") {
            continue;
        }
        let text = match extract_text(blocks) {
            Some(t) => t,
            None => continue,
        };
        if is_wrapper(&text) || is_codex_injected(&text) {
            continue;
        }

        out.push(crate::model::RawMessage {
            agent: "codex",
            project: project.clone().unwrap_or_else(|| "unknown".to_string()),
            session: session
                .clone()
                .unwrap_or_else(|| session_from_filename(path)),
            ts: ts_millis(l.timestamp.as_deref()),
            turn,
            text,
            model: current_model.clone(),
            reply: String::new(),
        });
        turn += 1;
    }

    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        events,
    )
}

/// Recursively collect rollout files under `~/.codex/sessions/` (YYYY/MM/DD nesting).
fn gather(dir: &Path, files: &mut Vec<std::path::PathBuf>) {
    let rd = match fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(_) => return,
    };
    for entry in rd.flatten() {
        let p = entry.path();
        if p.is_dir() {
            gather(&p, files);
        } else if let Some(name) = p.file_name().and_then(|n| n.to_str()) {
            if name.starts_with("rollout-") && name.ends_with(".jsonl") {
                files.push(p);
            }
        }
    }
}

/// Walk all session rollouts and collect the user's Codex messages + tool events.
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".codex").join("sessions");
    let mut files: Vec<std::path::PathBuf> = Vec::new();
    gather(&root, &mut files);

    let pass = crate::ingest_cache::collect_cached(cache, &root, &files, parse_file);
    (pass.messages, pass.events)
}
