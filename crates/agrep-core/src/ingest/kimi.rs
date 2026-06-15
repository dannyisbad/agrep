//! kimi adapter: ~/.kimi/sessions/<md5(workdir)>/<session-uuid>/{context,wire}.jsonl.
//!
//! context.jsonl is the model-facing transcript (kosong Message records, roles user/
//! assistant/tool plus `_`-prefixed internal directives) - canonical for WHO SAID WHAT,
//! but it carries no timestamps. wire.jsonl is the UI event log - every line is
//! `{"timestamp": <epoch secs float>, "message": {"type", "payload"}}` - canonical for
//! WHEN and for tool outcomes (ToolResult.return_value.is_error). Messages come from
//! context, with user-turn timestamps aligned in order against the wire's TurnBegin/
//! SteerInput events (steers land in context as plain user messages since kimi 1.21).
//!
//! The session dir's PARENT is md5(workdir path); ~/.kimi/kimi.json registers the real
//! paths, so hashing each registered path recovers the project. Rotated context_N.jsonl
//! generations (pre-/clear history) and subagents/ children are not ingested.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use crate::ingest::{cap_str, is_wrapper, project_name, summarize_tool_input, EVENT_CAP};
use crate::model::{Event, Message, RawMessage};

/// Concatenated text parts of a kosong content value (string, or array of typed parts).
/// `think` parts are the model's reasoning, not its reply - excluded.
fn text_of(content: &serde_json::Value) -> String {
    match content {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(parts) => {
            let mut out = String::new();
            for p in parts {
                if p.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(t) = p.get("text").and_then(|t| t.as_str()) {
                        if !out.is_empty() {
                            out.push('\n');
                        }
                        out.push_str(t);
                    }
                }
            }
            out
        }
        _ => String::new(),
    }
}

/// What the wire log contributes: ordered user-turn timestamps, per-call timestamps,
/// and per-call results (output + is_error, the only honest ok signal kimi records).
#[derive(Default)]
struct Wire {
    turn_ts: Vec<i64>,
    call_ts: HashMap<String, i64>,
    call_result: HashMap<String, (String, bool)>,
}

fn parse_wire(path: &Path) -> Wire {
    let mut w = Wire::default();
    let data = match fs::read_to_string(path) {
        Ok(d) => d,
        Err(_) => return w, // wire is enrichment; a session without one still ingests
    };
    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        let v: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let ts = (v.get("timestamp").and_then(|t| t.as_f64()).unwrap_or(0.0) * 1000.0) as i64;
        let msg = match v.get("message") {
            Some(m) => m,
            None => continue, // the protocol-version header line
        };
        let ty = msg.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let payload = msg.get("payload").unwrap_or(&serde_json::Value::Null);
        match ty {
            "TurnBegin" | "SteerInput" => w.turn_ts.push(ts),
            "ToolCall" => {
                if let Some(id) = payload.get("id").and_then(|i| i.as_str()) {
                    w.call_ts.insert(id.to_string(), ts);
                }
            }
            "ToolResult" => {
                let id = payload.get("tool_call_id").and_then(|i| i.as_str());
                if let (Some(id), Some(rv)) = (id, payload.get("return_value")) {
                    let out = rv
                        .get("output")
                        .map(|o| match o {
                            serde_json::Value::String(s) => cap_str(s, EVENT_CAP),
                            other => cap_str(&text_of(other), EVENT_CAP),
                        })
                        .unwrap_or_default();
                    let is_err = rv
                        .get("is_error")
                        .and_then(|e| e.as_bool())
                        .unwrap_or(false);
                    w.call_result.insert(id.to_string(), (out, is_err));
                }
            }
            _ => {}
        }
    }
    w
}

fn parse_session(dir: &Path, project: &str) -> (Vec<Message>, Vec<Event>) {
    let session = dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let data = match fs::read_to_string(dir.join("context.jsonl")) {
        Ok(d) => d,
        Err(_) => return (Vec::new(), Vec::new()), // empty/new session dir: normal
    };
    let wire = parse_wire(&dir.join("wire.jsonl"));

    let mut out: Vec<RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    // call_id -> index into `events`, so a context tool-role line can fill the output
    // when the wire didn't record a result.
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;
    let mut n_user = 0usize;
    let mut last_ts = 0i64;

    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        let v: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let role = v.get("role").and_then(|r| r.as_str()).unwrap_or("");
        match role {
            "user" => {
                let ts = wire.turn_ts.get(n_user).copied().unwrap_or(last_ts);
                n_user += 1;
                last_ts = ts;
                let text = v.get("content").map(text_of).unwrap_or_default();
                if text.trim().is_empty() || is_wrapper(&text) {
                    continue;
                }
                out.push(RawMessage {
                    agent: "kimi",
                    project: project.to_string(),
                    session: session.clone(),
                    ts,
                    turn,
                    text,
                    model: String::new(),
                    reply: String::new(),
                });
                turn += 1;
            }
            "assistant" => {
                if let Some(content) = v.get("content") {
                    let txt = text_of(content);
                    if !txt.trim().is_empty() {
                        if let Some(last) = out.last_mut() {
                            crate::ingest::append_capped(&mut last.reply, &txt, 1600);
                        }
                    }
                }
                if let Some(calls) = v.get("tool_calls").and_then(|c| c.as_array()) {
                    for c in calls {
                        let f = c.get("function").unwrap_or(&serde_json::Value::Null);
                        let name = f
                            .get("name")
                            .and_then(|n| n.as_str())
                            .unwrap_or("?")
                            .to_string();
                        let call_id = c
                            .get("id")
                            .and_then(|i| i.as_str())
                            .unwrap_or_default()
                            .to_string();
                        // arguments is a JSON string; summarize the parsed object
                        let input = f
                            .get("arguments")
                            .and_then(|a| a.as_str())
                            .map(|args| {
                                serde_json::from_str::<serde_json::Value>(args)
                                    .map(|v| summarize_tool_input(&v))
                                    .unwrap_or_else(|_| cap_str(args, EVENT_CAP))
                            })
                            .unwrap_or_default();
                        let (output, ok) = match wire.call_result.get(&call_id) {
                            Some((out, is_err)) => (out.clone(), Some(!is_err)),
                            None => (String::new(), None),
                        };
                        let ts = wire.call_ts.get(&call_id).copied().unwrap_or(last_ts);
                        if !call_id.is_empty() && ok.is_none() {
                            pending.insert(call_id.clone(), events.len());
                        }
                        events.push(Event {
                            agent: "kimi",
                            session: session.clone(),
                            ts,
                            kind: "tool",
                            name,
                            input,
                            output,
                            ok,
                            call_id,
                            child_session: String::new(),
                        });
                    }
                }
            }
            "tool" => {
                // fallback when the wire recorded no result: the transcript's tool message
                // carries the output, with errors flagged by a leading <system>ERROR: tag
                let id = v.get("tool_call_id").and_then(|i| i.as_str()).unwrap_or("");
                if let Some(&i) = pending.get(id) {
                    let txt = v.get("content").map(text_of).unwrap_or_default();
                    let ev = &mut events[i];
                    ev.output = cap_str(&txt, EVENT_CAP);
                    ev.ok = Some(!txt.trim_start().starts_with("<system>ERROR"));
                    pending.remove(id);
                }
            }
            _ => {} // system prompt, _checkpoint, _usage, ...
        }
    }
    (out.into_iter().map(RawMessage::freeze).collect(), events)
}

/// Map each `sessions/<md5>/` dir to its real workdir via the kimi.json registry.
fn workdir_registry(root: &Path) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let data = match fs::read_to_string(root.join("kimi.json")) {
        Ok(d) => d,
        Err(_) => return map,
    };
    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&data) {
        if let Some(dirs) = v.get("work_dirs").and_then(|d| d.as_array()) {
            for wd in dirs {
                if let Some(p) = wd.get("path").and_then(|p| p.as_str()) {
                    map.insert(format!("{:x}", md5::compute(p)), p.to_string());
                }
            }
        }
    }
    map
}

/// Walk every `sessions/<md5(workdir)>/<uuid>/` and collect kimi messages + tool events.
pub fn collect() -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".kimi");
    let sessions = root.join("sessions");
    let rd = match fs::read_dir(&sessions) {
        Ok(d) => d,
        Err(_) => return (Vec::new(), Vec::new()),
    };
    let registry = workdir_registry(&root);

    // (session dir, project) pairs; the project comes from the workdir registry,
    // falling back to "kimi" for unregistered (e.g. remote-KAOS) dirs.
    let mut work: Vec<(PathBuf, String)> = Vec::new();
    for entry in rd.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let key = entry.file_name().to_string_lossy().to_string();
        let project = registry
            .get(&key)
            .map(|p| project_name(p))
            .unwrap_or_else(|| "kimi".to_string());
        if let Ok(sess) = fs::read_dir(&dir) {
            for s in sess.flatten() {
                let sd = s.path();
                if sd.is_dir() {
                    work.push((sd, project.clone()));
                }
            }
        }
    }

    let pairs: Vec<(Vec<Message>, Vec<Event>)> = work
        .par_iter()
        .map(|(d, proj)| parse_session(d, proj))
        .collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e) in pairs {
        msgs.extend(m);
        evts.extend(e);
    }
    (msgs, evts)
}
