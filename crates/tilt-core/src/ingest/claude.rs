//! Claude Code adapter: ~/.claude/projects/<cwd-slug>/*.jsonl
//! Top-level session files only (skip `subagents/` and throwaway worker slugs).
//! Keeps real human turns: type=user, not meta/sidechain, userType external|absent,
//! string or text-block content, not a command/system wrapper.

use std::collections::HashMap;
use std::fs;
use std::path::Path;

use rayon::prelude::*;
use serde::Deserialize;

use crate::ingest::{cap_str, is_wrapper, project_name, summarize_tool_input, ts_millis, EVENT_CAP};
use crate::model::{Event, Message};

#[derive(Deserialize)]
struct Line {
    #[serde(rename = "type")]
    ty: Option<String>,
    #[serde(rename = "isMeta")]
    is_meta: Option<bool>,
    #[serde(rename = "isSidechain")]
    is_sidechain: Option<bool>,
    #[serde(rename = "userType")]
    user_type: Option<String>,
    message: Option<Msg>,
    timestamp: Option<String>,
    cwd: Option<String>,
    #[serde(rename = "sessionId")]
    session_id: Option<String>,
}

#[derive(Deserialize)]
struct Msg {
    role: Option<String>,
    content: Option<serde_json::Value>,
    model: Option<String>,
}

/// Pull human text out of a `message.content` that may be a string or a block array.
fn extract_text(content: &serde_json::Value) -> Option<String> {
    match content {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Array(blocks) => {
            let mut out = String::new();
            for b in blocks {
                if b.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                        if !out.is_empty() {
                            out.push('\n');
                        }
                        out.push_str(t);
                    }
                }
            }
            if out.is_empty() {
                None
            } else {
                Some(out)
            }
        }
        _ => None,
    }
}

/// Pull file paths out of a session's tool_use blocks (Read/Write/Edit/... carry file_path,
/// notebook_path, or path). These reveal where the work actually happened.
fn collect_tool_paths(content: &serde_json::Value, out: &mut Vec<String>) {
    if let serde_json::Value::Array(blocks) = content {
        for b in blocks {
            if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                continue;
            }
            if let Some(inp) = b.get("input") {
                for key in ["file_path", "notebook_path", "path"] {
                    if let Some(v) = inp.get(key).and_then(|v| v.as_str()) {
                        if v.contains('/') || v.contains('\\') {
                            out.push(v.to_string());
                        }
                    }
                }
            }
        }
    }
}

/// Reduce a directory to its project ROOT bucket: strip the home prefix and any
/// container segments (`Users/<name>/Desktop/...`), keep the first real segment.
/// `~/Desktop/tilt/py` -> Some("tilt"); a bare home dir -> None (no signal).
fn project_root(dir: &str) -> Option<String> {
    let d = dir.replace('\\', "/");
    let home = crate::ingest::home()
        .to_string_lossy()
        .replace('\\', "/")
        .to_lowercase();
    let rel = if d.to_lowercase().starts_with(&home) {
        &d[home.len().min(d.len())..]
    } else {
        &d[..]
    };
    let user = crate::ingest::home_leaf();
    let mut segs = rel.split('/').filter(|s| !s.is_empty());
    segs.find(|s| {
        let sl = s.to_ascii_lowercase();
        !sl.ends_with(':')
            && sl != user
            && !matches!(
                sl.as_str(),
                "users" | "desktop" | "documents" | "downloads" | "onedrive"
                    | "home" | "tmp" | "temp" | "appdata" | "src"
            )
    })
    .map(|s| s.to_string())
}

/// The project a session actually worked in: a histogram over EVERY line's cwd (Claude
/// updates it as the session cd's around) plus the parent dir of every file its tools
/// touched, reduced to project roots. The most-worked-in root wins. Sessions launched
/// from home that evolve into a real project ("i start A LOT of sessions in home dirs
/// that evolve into completely different shit") land on where the work went, not where
/// the terminal happened to open.
fn primary_project(cwd_counts: &HashMap<String, usize>, paths: &[String], first_cwd: &str) -> String {
    let mut counts: HashMap<String, usize> = HashMap::new();
    for (c, n) in cwd_counts {
        if let Some(k) = project_root(c) {
            *counts.entry(k).or_insert(0) += n;
        }
    }
    for p in paths {
        let pn = p.replace('\\', "/");
        if let Some(i) = pn.rfind('/') {
            if let Some(k) = project_root(&pn[..i]) {
                *counts.entry(k).or_insert(0) += 1;
            }
        }
    }
    // ties break on name so re-ingest is deterministic
    if let Some((k, _)) = counts
        .into_iter()
        .max_by(|a, b| a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0)))
    {
        return k;
    }
    project_name(first_cwd)
}

/// Text of a tool_result's `content`: a plain string, or an array of text blocks.
fn tool_result_text(content: &serde_json::Value) -> String {
    match content {
        serde_json::Value::String(s) => cap_str(s, EVENT_CAP),
        serde_json::Value::Array(blocks) => {
            let mut out = String::new();
            for b in blocks {
                if b.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                        if !out.is_empty() {
                            out.push('\n');
                        }
                        out.push_str(t);
                        if out.chars().count() > EVENT_CAP {
                            break;
                        }
                    }
                }
            }
            cap_str(&out, EVENT_CAP)
        }
        _ => String::new(),
    }
}

fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>) {
    let data = match fs::read_to_string(path) {
        Ok(d) => d,
        Err(_) => return (Vec::new(), Vec::new()),
    };
    let file_session = path
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let mut out: Vec<Message> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    // tool_use id -> index into `events`, so the later tool_result line can pair up.
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;
    let mut first_cwd = String::new();
    let mut cwd_counts: HashMap<String, usize> = HashMap::new();
    let mut tool_paths: Vec<String> = Vec::new();
    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(_) => continue,
        };
        if let Some(c) = l.cwd.as_deref() {
            if first_cwd.is_empty() {
                first_cwd = c.to_string();
            }
            *cwd_counts.entry(c.to_string()).or_insert(0) += 1;
        }
        let session = l
            .session_id
            .clone()
            .unwrap_or_else(|| file_session.clone());
        // Assistant turn -> attach its text + model to the user message it answers, and note
        // the files it touched (to infer the real working dir at the end).
        if l.ty.as_deref() == Some("assistant") {
            if let Some(m) = &l.message {
                if m.role.as_deref() == Some("assistant") {
                    if let Some(content) = &m.content {
                        collect_tool_paths(content, &mut tool_paths);
                        // tool_use blocks -> events (sidechain lines too: a subagent's
                        // internal calls carry the same sessionId and belong in the stream).
                        if let serde_json::Value::Array(blocks) = content {
                            for b in blocks {
                                if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                                    continue;
                                }
                                let name = b
                                    .get("name")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("?")
                                    .to_string();
                                let call_id = b
                                    .get("id")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or_default()
                                    .to_string();
                                let input = b
                                    .get("input")
                                    .map(summarize_tool_input)
                                    .unwrap_or_default();
                                let kind = if name == "Task" || name == "Agent" {
                                    "subagent_start"
                                } else {
                                    "tool"
                                };
                                if !call_id.is_empty() {
                                    pending.insert(call_id.clone(), events.len());
                                }
                                events.push(Event {
                                    agent: "claude",
                                    session: session.clone(),
                                    ts: ts_millis(l.timestamp.as_deref()),
                                    kind,
                                    name,
                                    input,
                                    output: String::new(),
                                    ok: None,
                                    call_id,
                                    child_session: String::new(),
                                });
                            }
                        }
                    }
                    if let Some(last) = out.last_mut() {
                        if last.model.is_empty() {
                            if let Some(md) = m.model.as_deref() {
                                if !md.is_empty() {
                                    last.model = md.to_string();
                                }
                            }
                        }
                        if let Some(txt) = m.content.as_ref().and_then(extract_text) {
                            crate::ingest::append_capped(&mut last.reply, &txt, 1600);
                        }
                    }
                }
            }
            continue;
        }
        if l.ty.as_deref() != Some("user") {
            continue;
        }
        // tool_result blocks arrive in user-typed lines (userType external, isMeta null),
        // so pair them BEFORE the human-turn filters would drop the line.
        if let Some(serde_json::Value::Array(blocks)) =
            l.message.as_ref().and_then(|m| m.content.as_ref())
        {
            for b in blocks {
                if b.get("type").and_then(|t| t.as_str()) != Some("tool_result") {
                    continue;
                }
                let id = b.get("tool_use_id").and_then(|v| v.as_str()).unwrap_or("");
                if let Some(&i) = pending.get(id) {
                    let ev = &mut events[i];
                    if let Some(c) = b.get("content") {
                        ev.output = tool_result_text(c);
                    }
                    ev.ok = Some(b.get("is_error").and_then(|v| v.as_bool()) != Some(true));
                    pending.remove(id);
                }
            }
        }
        if l.is_meta == Some(true) || l.is_sidechain == Some(true) {
            continue;
        }
        if let Some(ut) = l.user_type.as_deref() {
            if ut != "external" {
                continue;
            }
        }
        let msg = match &l.message {
            Some(m) => m,
            None => continue,
        };
        if msg.role.as_deref() != Some("user") {
            continue;
        }
        let text = match msg.content.as_ref().and_then(extract_text) {
            Some(t) if !t.trim().is_empty() => t,
            _ => continue,
        };
        if is_wrapper(&text) {
            continue;
        }
        out.push(Message {
            agent: "claude",
            project: String::new(), // filled once per session below
            session,
            ts: ts_millis(l.timestamp.as_deref()),
            turn,
            text,
            model: String::new(),
            reply: String::new(),
        });
        turn += 1;
    }
    // One project for the whole session: where the work actually happened.
    let project = primary_project(&cwd_counts, &tool_paths, &first_cwd);
    for m in &mut out {
        m.project = project.clone();
    }
    (out, events)
}

/// Walk all real project dirs and collect the user's Claude messages + tool events.
pub fn collect() -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".claude").join("projects");
    let dirs = match fs::read_dir(&root) {
        Ok(d) => d,
        Err(_) => return (Vec::new(), Vec::new()),
    };

    // Gather top-level *.jsonl from each non-throwaway project dir.
    let mut files: Vec<std::path::PathBuf> = Vec::new();
    for entry in dirs.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.contains("Temp-claude") || name.contains("claude-worker") {
            continue;
        }
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        if let Ok(rd) = fs::read_dir(&dir) {
            for f in rd.flatten() {
                let p = f.path();
                if p.extension().and_then(|e| e.to_str()) == Some("jsonl") {
                    files.push(p);
                }
            }
        }
    }

    let pairs: Vec<(Vec<Message>, Vec<Event>)> = files.par_iter().map(|p| parse_file(p)).collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e) in pairs {
        msgs.extend(m);
        evts.extend(e);
    }
    (msgs, evts)
}
