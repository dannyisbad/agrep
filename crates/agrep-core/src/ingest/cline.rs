//! cline adapter: `<globalStorage>/saoudrizwan.claude-dev/tasks/<taskId>/` in every
//! VSCode-family editor, plus `~/.cline/data/` (the CLI / JetBrains root).
//!
//! Per task: `api_conversation_history.json` (Anthropic MessageParam array, the source
//! of truth for turns and native tool_use/tool_result blocks) and a sibling
//! `state/taskHistory.json` index at the root (HistoryItem[]: cwd, model, task title).
//! taskIds are `Date.now()` millisecond strings — that anchors timestamps for older
//! tasks whose messages predate the per-message `ts` field. Classic (non-native) tool
//! calls are XML inside assistant text and stay in the reply; only real tool_use
//! blocks become events.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use crate::ingest::{cap_str, is_wrapper, project_name, summarize_tool_input, EVENT_CAP};
use crate::model::{Event, Message, RawMessage};

/// Every globalStorage root cline can write on this machine. Which exist is checked by
/// the caller; missing editors simply don't contribute.
fn roots() -> Vec<PathBuf> {
    const PRODUCTS: &[&str] = &["Code", "Code - Insiders", "Cursor", "Windsurf", "VSCodium"];
    let mut out = Vec::new();
    // VSCode-family globalStorage, per OS convention
    if let Some(appdata) = std::env::var_os("APPDATA") {
        let base = PathBuf::from(appdata);
        for p in PRODUCTS {
            out.push(base.join(p).join("User").join("globalStorage").join("saoudrizwan.claude-dev"));
        }
    } else {
        let home = crate::ingest::home();
        let mac = home.join("Library").join("Application Support");
        let linux = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".config"));
        for base in [mac, linux] {
            for p in PRODUCTS {
                out.push(base.join(p).join("User").join("globalStorage").join("saoudrizwan.claude-dev"));
            }
        }
    }
    // standalone root (CLI / JetBrains), env-overridable
    let cline_dir = std::env::var_os("CLINE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::ingest::home().join(".cline"));
    out.push(cline_dir.join("data"));
    out
}

/// What taskHistory.json knows about each task: cwd, model, last-activity ts.
struct TaskMeta {
    cwd: String,
    model: String,
}

fn task_index(root: &Path) -> HashMap<String, TaskMeta> {
    let mut map = HashMap::new();
    let p = root.join("state").join("taskHistory.json");
    let data = match fs::read_to_string(&p) {
        Ok(d) => d,
        Err(_) => return map,
    };
    if let Ok(serde_json::Value::Array(items)) = serde_json::from_str(&data) {
        for it in items {
            let id = match it.get("id").and_then(|i| i.as_str()) {
                Some(i) => i.to_string(),
                None => continue,
            };
            map.insert(
                id,
                TaskMeta {
                    cwd: it
                        .get("cwdOnTaskInitialization")
                        .and_then(|c| c.as_str())
                        .unwrap_or_default()
                        .to_string(),
                    model: it.get("modelId").and_then(|m| m.as_str()).unwrap_or_default().to_string(),
                },
            );
        }
    }
    map
}

/// The user's actual words from a cline user turn: drop the bulky injected
/// `<environment_details>` block, unwrap `<task>`/`<feedback>`/`<answer>` envelopes.
fn clean_user_text(raw: &str) -> String {
    let mut s = raw.to_string();
    if let (Some(a), Some(b)) = (s.find("<environment_details>"), s.rfind("</environment_details>")) {
        if a < b {
            s.replace_range(a..b + "</environment_details>".len(), "");
        }
    }
    for tag in ["task", "feedback", "answer", "user_message"] {
        let open = format!("<{tag}>");
        let close = format!("</{tag}>");
        if let (Some(a), Some(b)) = (s.find(&open), s.rfind(&close)) {
            if a < b {
                s = s[a + open.len()..b].to_string();
                break;
            }
        }
    }
    s.trim().to_string()
}

fn parse_task(dir: &Path, meta: Option<&TaskMeta>) -> (Vec<Message>, Vec<Event>) {
    let task_id = dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let data = match fs::read_to_string(dir.join("api_conversation_history.json")) {
        Ok(d) => d,
        Err(_) => return (Vec::new(), Vec::new()), // task dir without history: normal
    };
    let msgs: Vec<serde_json::Value> = match serde_json::from_str(&data) {
        Ok(serde_json::Value::Array(a)) => a,
        _ => return (Vec::new(), Vec::new()),
    };
    // taskId is Date.now().to_string(): the session-start fallback for pre-`ts` messages
    let start_ts: i64 = task_id.parse().unwrap_or(0);
    let project = meta
        .map(|m| m.cwd.as_str())
        .filter(|c| !c.is_empty())
        .map(project_name)
        .unwrap_or_else(|| "cline".to_string());
    let fallback_model = meta.map(|m| m.model.clone()).unwrap_or_default();

    let mut out: Vec<RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;

    for m in &msgs {
        let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("");
        let ts = m.get("ts").and_then(|t| t.as_i64()).unwrap_or(start_ts);
        let model = m
            .get("modelInfo")
            .and_then(|mi| mi.get("modelId"))
            .and_then(|id| id.as_str())
            .unwrap_or(&fallback_model)
            .to_string();
        // content: plain string or an array of typed blocks
        let blocks: Vec<&serde_json::Value> = match m.get("content") {
            Some(serde_json::Value::Array(a)) => a.iter().collect(),
            Some(serde_json::Value::String(_)) => Vec::new(),
            _ => Vec::new(),
        };
        let plain = m.get("content").and_then(|c| c.as_str());

        if role == "user" {
            // pair tool_result blocks to their events first; they share the message
            // with (or entirely replace) the user's typed text
            let mut text = plain.map(str::to_string).unwrap_or_default();
            for b in &blocks {
                match b.get("type").and_then(|t| t.as_str()) {
                    Some("tool_result") => {
                        let id = b.get("tool_use_id").and_then(|i| i.as_str()).unwrap_or("");
                        if let Some(&i) = pending.get(id) {
                            let body = match b.get("content") {
                                Some(serde_json::Value::String(s)) => s.clone(),
                                Some(serde_json::Value::Array(parts)) => parts
                                    .iter()
                                    .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                                    .collect::<Vec<_>>()
                                    .join("\n"),
                                _ => String::new(),
                            };
                            let ev = &mut events[i];
                            ev.output = cap_str(&body, EVENT_CAP);
                            ev.ok = b
                                .get("is_error")
                                .and_then(|e| e.as_bool())
                                .map(|e| !e);
                            pending.remove(id);
                        }
                    }
                    Some("text") => {
                        if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                            if !text.is_empty() {
                                text.push('\n');
                            }
                            text.push_str(t);
                        }
                    }
                    _ => {}
                }
            }
            let text = clean_user_text(&text);
            if text.is_empty() || is_wrapper(&text) {
                continue;
            }
            out.push(RawMessage {
                agent: "cline",
                project: project.clone(),
                session: task_id.clone(),
                ts,
                turn,
                text,
                model,
                reply: String::new(),
            });
            turn += 1;
        } else if role == "assistant" {
            let mut reply = plain.map(str::to_string).unwrap_or_default();
            for b in &blocks {
                match b.get("type").and_then(|t| t.as_str()) {
                    Some("text") => {
                        if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                            if !reply.is_empty() {
                                reply.push('\n');
                            }
                            reply.push_str(t);
                        }
                    }
                    Some("tool_use") => {
                        let name = b
                            .get("name")
                            .and_then(|n| n.as_str())
                            .unwrap_or("?")
                            .to_string();
                        let call_id = b
                            .get("id")
                            .and_then(|i| i.as_str())
                            .unwrap_or_default()
                            .to_string();
                        let input = b.get("input").map(summarize_tool_input).unwrap_or_default();
                        if !call_id.is_empty() {
                            pending.insert(call_id.clone(), events.len());
                        }
                        events.push(Event {
                            agent: "cline",
                            session: task_id.clone(),
                            ts,
                            kind: "tool",
                            name,
                            input,
                            output: String::new(),
                            ok: None,
                            call_id,
                            child_session: String::new(),
                        });
                    }
                    _ => {}
                }
            }
            if !reply.trim().is_empty() {
                if let Some(last) = out.last_mut() {
                    crate::ingest::append_capped(&mut last.reply, &reply, 1600);
                    if last.model.is_empty() && !model.is_empty() {
                        last.model = model;
                    }
                }
            }
        }
    }
    (out.into_iter().map(RawMessage::freeze).collect(), events)
}

/// Walk every editor's cline globalStorage (plus ~/.cline/data) and collect tasks.
pub fn collect() -> (Vec<Message>, Vec<Event>) {
    let mut work: Vec<(PathBuf, Option<TaskMeta>)> = Vec::new();
    for root in roots() {
        let tasks = root.join("tasks");
        let rd = match fs::read_dir(&tasks) {
            Ok(d) => d,
            Err(_) => continue, // editor not installed / cline absent there
        };
        let mut index = task_index(&root);
        for entry in rd.flatten() {
            let dir = entry.path();
            if dir.is_dir() {
                let meta = index.remove(&entry.file_name().to_string_lossy().to_string());
                work.push((dir, meta));
            }
        }
    }

    let pairs: Vec<(Vec<Message>, Vec<Event>)> = work
        .par_iter()
        .map(|(dir, meta)| parse_task(dir, meta.as_ref()))
        .collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e) in pairs {
        msgs.extend(m);
        evts.extend(e);
    }
    (msgs, evts)
}
