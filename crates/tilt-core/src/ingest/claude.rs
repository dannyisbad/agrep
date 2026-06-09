//! Claude Code adapter: ~/.claude/projects/<cwd-slug>/*.jsonl
//! Top-level session files only (skip `subagents/` and throwaway worker slugs).
//! Keeps real human turns: type=user, not meta/sidechain, userType external|absent,
//! string or text-block content, not a command/system wrapper.

use std::fs;
use std::path::Path;

use rayon::prelude::*;
use serde::Deserialize;

use crate::ingest::{is_wrapper, project_name, ts_millis};
use crate::model::Message;

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

fn parse_file(path: &Path) -> Vec<Message> {
    let data = match fs::read_to_string(path) {
        Ok(d) => d,
        Err(_) => return Vec::new(),
    };
    let mut out: Vec<Message> = Vec::new();
    let mut turn = 0u32;
    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(_) => continue,
        };
        // Assistant turn -> attach its text + model to the user message it answers.
        if l.ty.as_deref() == Some("assistant") {
            if let Some(m) = &l.message {
                if m.role.as_deref() == Some("assistant") {
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
        let project = project_name(l.cwd.as_deref().unwrap_or(""));
        out.push(Message {
            agent: "claude",
            project,
            session: l.session_id.clone().unwrap_or_default(),
            ts: ts_millis(l.timestamp.as_deref()),
            turn,
            text,
            model: String::new(),
            reply: String::new(),
        });
        turn += 1;
    }
    out
}

/// Walk all real project dirs and collect Danny's Claude messages.
pub fn collect() -> Vec<Message> {
    let root = crate::ingest::home().join(".claude").join("projects");
    let dirs = match fs::read_dir(&root) {
        Ok(d) => d,
        Err(_) => return Vec::new(),
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

    files
        .par_iter()
        .flat_map(|p| parse_file(p))
        .collect()
}
