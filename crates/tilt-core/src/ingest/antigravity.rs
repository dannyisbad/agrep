//! Antigravity (Google's "agy" CLI) adapter.
//!
//! Store layout: `~/.gemini/antigravity-cli/brain/<session-uuid>/`
//!   - `.system_generated/messages/*.json` is the *inter-agent mailbox* (sender =
//!     `<uuid>/task-N` task results, cross-uuid subagent `send_message`s, or
//!     `system` notices) — NONE of these are Danny, so we ignore that dir entirely.
//!   - `.system_generated/logs/transcript.jsonl` is the real conversation log. Each
//!     line is an event `{type, source, content, created_at, tool_calls, ...}`.
//!
//! Discriminator for Danny's own typed prompts (verified across all 3 non-empty
//! sessions, 68 events): an event with `type == "USER_INPUT"` AND
//! `source == "USER_EXPLICIT"`. This pairing is exact and exclusive — USER_INPUT
//! only ever appears with USER_EXPLICIT, and the model's own turns use
//! `source == "MODEL"`, system text uses `source == "SYSTEM"`. The human's text is
//! wrapped as `<USER_REQUEST>...</USER_REQUEST>` followed by injected
//! `<ADDITIONAL_METADATA>` / `<USER_SETTINGS_CHANGE>` preambles; we extract only the
//! inner USER_REQUEST body and drop the injected wrappers.
//!
//! Project: there is no cwd field on the user events, so we infer the workspace from
//! the most frequent `tool_calls[*].args.Cwd` seen in the session's transcript
//! (dominant value is the working dir, e.g. `C:\Users\Danny\opencode-dev`), then run
//! it through `project_name`. Falls back to "antigravity" when no Cwd is present.

use std::collections::HashMap;
use std::fs;
use std::path::Path;

use rayon::prelude::*;
use serde::Deserialize;

use crate::ingest::{is_wrapper, project_name, ts_millis};
use crate::model::Message;

#[derive(Deserialize)]
struct Event {
    #[serde(rename = "type")]
    ty: Option<String>,
    source: Option<String>,
    content: Option<serde_json::Value>,
    created_at: Option<String>,
    tool_calls: Option<Vec<ToolCall>>,
}

#[derive(Deserialize)]
struct ToolCall {
    args: Option<serde_json::Value>,
}

/// Pull the human's actual prompt out of the `<USER_REQUEST>...</USER_REQUEST>`
/// wrapper, discarding the injected `<ADDITIONAL_METADATA>` / `<USER_SETTINGS_CHANGE>`
/// preambles. If the markers are somehow absent, fall back to the trimmed whole.
fn extract_user_request(content: &str) -> Option<String> {
    const OPEN: &str = "<USER_REQUEST>";
    const CLOSE: &str = "</USER_REQUEST>";
    let inner = match (content.find(OPEN), content.find(CLOSE)) {
        (Some(o), Some(c)) if c > o + OPEN.len() => &content[o + OPEN.len()..c],
        _ => content,
    };
    let trimmed = inner.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Most frequent `tool_calls[*].args.Cwd` across the transcript → the session's
/// working directory. Returns None if no Cwd was ever recorded.
fn dominant_cwd(events: &[Event]) -> Option<String> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    for e in events {
        if let Some(tcs) = &e.tool_calls {
            for tc in tcs {
                if let Some(cwd) = tc
                    .args
                    .as_ref()
                    .and_then(|a| a.get("Cwd"))
                    .and_then(|v| v.as_str())
                {
                    if !cwd.is_empty() {
                        *counts.entry(cwd.to_string()).or_insert(0) += 1;
                    }
                }
            }
        }
    }
    counts
        .into_iter()
        .max_by_key(|(_, n)| *n)
        .map(|(cwd, _)| cwd)
}

fn parse_session(brain_dir: &Path) -> Vec<Message> {
    let session = brain_dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();

    let transcript = brain_dir
        .join(".system_generated")
        .join("logs")
        .join("transcript.jsonl");
    let data = match fs::read_to_string(&transcript) {
        Ok(d) => d,
        Err(_) => return Vec::new(),
    };

    // Parse every event once; we need a full pass for the dominant Cwd anyway.
    let events: Vec<Event> = data
        .lines()
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_str::<Event>(l).ok())
        .collect();

    let project = dominant_cwd(&events)
        .map(|cwd| project_name(&cwd))
        .unwrap_or_else(|| "antigravity".to_string());

    let mut out = Vec::new();
    let mut turn = 0u32;
    for e in &events {
        // Danny's own typed input only.
        if e.ty.as_deref() != Some("USER_INPUT") || e.source.as_deref() != Some("USER_EXPLICIT") {
            continue;
        }
        let raw = match e.content.as_ref().and_then(|c| c.as_str()) {
            Some(s) => s,
            None => continue,
        };
        let text = match extract_user_request(raw) {
            Some(t) => t,
            None => continue,
        };
        if is_wrapper(&text) {
            continue;
        }
        out.push(Message {
            agent: "antigravity",
            project: project.clone(),
            session: session.clone(),
            ts: ts_millis(e.created_at.as_deref()),
            turn,
            text,
        });
        turn += 1;
    }
    out
}

/// Walk every `brain/<uuid>/` session and collect Danny's Antigravity prompts.
pub fn collect() -> Vec<Message> {
    let root = crate::ingest::home()
        .join(".gemini")
        .join("antigravity-cli")
        .join("brain");
    let dirs = match fs::read_dir(&root) {
        Ok(d) => d,
        Err(_) => return Vec::new(),
    };

    let session_dirs: Vec<std::path::PathBuf> = dirs
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect();

    session_dirs
        .par_iter()
        .flat_map(|d| parse_session(d))
        .collect()
}
