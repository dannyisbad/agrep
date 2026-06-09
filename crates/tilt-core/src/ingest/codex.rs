//! Codex CLI adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
//! One JSONL file per session. The first line is `session_meta` (gives cwd -> project,
//! and the session id). Real human turns are `response_item` lines with
//! payload.type=="message" and payload.role=="user", text in `input_text`/`text` blocks.
//!
//! Codex injects a large first "user" turn (AGENTS.md / environment / instructions) plus
//! a stream of system-authored `role:user` notifications (turn_aborted, subagent_notification,
//! goal_context, delegations, image-paste markers). None of those are Danny's words, so we
//! drop them and keep only what he actually typed. Prefer under-including over mislabeling.
//!
//! NOTE: only `~/.codex/sessions/` is walked. `~/.codex/.tmp/**` (plugin test fixtures),
//! `~/.codex/history.jsonl`, and `~/.codex/archived_sessions/` are intentionally not read.

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
    timestamp: Option<String>,
    payload: Option<Payload>,
}

#[derive(Deserialize)]
struct Payload {
    #[serde(rename = "type")]
    ty: Option<String>,
    role: Option<String>,
    content: Option<Vec<Block>>,
    // session_meta fields
    id: Option<String>,
    cwd: Option<String>,
    // turn_context carries the active model (e.g. "gpt-5.3-codex-spark").
    model: Option<String>,
}

#[derive(Deserialize)]
struct Block {
    #[serde(rename = "type")]
    ty: Option<String>,
    text: Option<String>,
}

/// Concatenate the human-authored text blocks (`input_text`, also accept `text`).
fn extract_text(blocks: &[Block]) -> Option<String> {
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
fn extract_assistant(blocks: &[Block]) -> Option<String> {
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
/// something Danny typed. Checked in addition to the shared `is_wrapper`.
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
        // avoid emitting the marker as Danny's words.
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

fn parse_file(path: &Path) -> Vec<Message> {
    let data = match fs::read_to_string(path) {
        Ok(d) => d,
        Err(_) => return Vec::new(),
    };

    let mut out: Vec<Message> = Vec::new();
    let mut turn = 0u32;
    let mut project: Option<String> = None;
    let mut session: Option<String> = None;
    // The active model, updated by each turn_context line and stamped onto turns.
    let mut current_model = String::new();

    for line in data.lines() {
        if line.is_empty() {
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
        // Only chat messages; skip function_call / function_call_output / reasoning.
        if payload.ty.as_deref() != Some("message") {
            continue;
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

        out.push(Message {
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

    out
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

/// Walk all session rollouts and collect Danny's Codex messages.
pub fn collect() -> Vec<Message> {
    let root = crate::ingest::home().join(".codex").join("sessions");
    let mut files: Vec<std::path::PathBuf> = Vec::new();
    gather(&root, &mut files);

    files
        .par_iter()
        .flat_map(|p| parse_file(p))
        .collect()
}
