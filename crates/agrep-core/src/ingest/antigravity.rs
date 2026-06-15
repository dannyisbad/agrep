//! Antigravity (Google's "agy" CLI) adapter.
//!
//! Store layout: `~/.gemini/antigravity-cli/brain/<session-uuid>/`
//!   - `.system_generated/messages/*.json` is the *inter-agent mailbox* (sender =
//!     `<uuid>/task-N` task results, cross-uuid subagent `send_message`s, or
//!     `system` notices) - NONE of these are the user, so we ignore that dir entirely.
//!   - `.system_generated/logs/transcript.jsonl` is the real conversation log. Each
//!     line is an event `{type, source, content, created_at, tool_calls, ...}`.
//!
//! Discriminator for the user's own typed prompts (verified across all 3 non-empty
//! sessions, 68 events): an event with `type == "USER_INPUT"` AND
//! `source == "USER_EXPLICIT"`. This pairing is exact and exclusive - USER_INPUT
//! only ever appears with USER_EXPLICIT, and the model's own turns use
//! `source == "MODEL"`, system text uses `source == "SYSTEM"`. The human's text is
//! wrapped as `<USER_REQUEST>...</USER_REQUEST>` followed by injected
//! `<ADDITIONAL_METADATA>` / `<USER_SETTINGS_CHANGE>` preambles; we extract only the
//! inner USER_REQUEST body and drop the injected wrappers.
//!
//! Project: there is no cwd field on the user events, so we infer the workspace from
//! the most frequent `tool_calls[*].args.Cwd` seen in the session's transcript
//! (dominant value is the working dir, e.g. `~\opencode-dev`), then run
//! it through `project_name`. Falls back to "antigravity" when no Cwd is present.

use std::collections::{HashMap, VecDeque};
use std::fs;
use std::path::Path;

use rayon::prelude::*;
use serde::Deserialize;

use crate::ingest::{cap_str, is_wrapper, project_name, ts_millis, EVENT_CAP};
use crate::model::{Event, Message};

#[derive(Deserialize)]
struct TLine {
    #[serde(rename = "type")]
    ty: Option<String>,
    source: Option<String>,
    status: Option<String>,
    content: Option<serde_json::Value>,
    created_at: Option<String>,
    tool_calls: Option<Vec<ToolCall>>,
}

#[derive(Deserialize)]
struct ToolCall {
    name: Option<String>,
    args: Option<serde_json::Value>,
}

/// Transcript event types that are the EXECUTION of a previously announced tool call
/// (PLANNER_RESPONSE carries the intent in `tool_calls`; one of these follows with the
/// output in `content` and a DONE/ERROR status).
fn is_action_type(ty: &str) -> bool {
    matches!(
        ty,
        "RUN_COMMAND" | "VIEW_FILE" | "GREP_SEARCH" | "CODE_ACTION" | "LIST_DIRECTORY" | "GENERIC"
    )
}

/// Antigravity args values are JSON-encoded strings *inside* JSON ("\"git status\"").
fn unq(v: &serde_json::Value) -> String {
    match v.as_str() {
        Some(s) => {
            serde_json::from_str::<String>(s).unwrap_or_else(|_| s.trim_matches('"').to_string())
        }
        None => v.to_string(),
    }
}

/// Human-meaningful summary of an antigravity tool_call's args.
fn summarize_agy_args(args: &serde_json::Value) -> String {
    if let Some(obj) = args.as_object() {
        for k in [
            "CommandLine",
            "AbsolutePath",
            "Query",
            "SearchDirectory",
            "toolSummary",
        ] {
            if let Some(v) = obj.get(k) {
                let s = unq(v);
                if !s.trim().is_empty() {
                    return cap_str(&s, EVENT_CAP);
                }
            }
        }
    }
    cap_str(&args.to_string(), EVENT_CAP)
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
fn dominant_cwd(events: &[TLine]) -> Option<String> {
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

/// The inter-agent mailbox: `.system_generated/messages/*.json`, each
/// `{sender:"<uuid>/task-N"|..., timestamp, renderDetails:{messageTitle}, content}`.
/// Task-result mail becomes subagent_result events; system notices are skipped.
fn collect_mailbox(brain_dir: &Path, session: &str, evts: &mut Vec<Event>) {
    #[derive(Deserialize)]
    struct Mail {
        id: Option<String>,
        sender: Option<String>,
        timestamp: Option<serde_json::Value>,
        #[serde(rename = "renderDetails")]
        render: Option<RenderDetails>,
        content: Option<String>,
    }
    #[derive(Deserialize)]
    struct RenderDetails {
        #[serde(rename = "messageTitle")]
        title: Option<String>,
    }

    let dir = brain_dir.join(".system_generated").join("messages");
    let rd = match fs::read_dir(&dir) {
        Ok(rd) => rd,
        Err(_) => return,
    };
    for entry in rd.flatten() {
        let p = entry.path();
        if p.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let m: Mail = match fs::read_to_string(&p)
            .ok()
            .and_then(|d| serde_json::from_str(&d).ok())
        {
            Some(m) => m,
            None => continue,
        };
        let sender = m.sender.as_deref().unwrap_or("");
        if !sender.contains("/task-") {
            continue; // cross-agent mail + system notices; only task results are subagent output
        }
        let task = sender.rsplit('/').next().unwrap_or(sender).to_string();
        let ts = match &m.timestamp {
            Some(serde_json::Value::String(s)) => ts_millis(Some(s)),
            Some(serde_json::Value::Number(n)) => {
                let v = n.as_i64().unwrap_or(0);
                if v > 0 && v < 100_000_000_000 {
                    v * 1000
                } else {
                    v
                }
            }
            _ => 0,
        };
        let title = m
            .render
            .as_ref()
            .and_then(|r| r.title.as_deref())
            .unwrap_or("");
        evts.push(Event {
            agent: "antigravity",
            session: session.to_string(),
            ts,
            kind: "subagent_result",
            name: if title.is_empty() {
                task.clone()
            } else {
                cap_str(title, 200)
            },
            input: task,
            output: m
                .content
                .as_deref()
                .map(|c| cap_str(c, EVENT_CAP))
                .unwrap_or_default(),
            ok: None,
            call_id: m.id.unwrap_or_else(|| {
                p.file_stem()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_default()
            }),
            child_session: String::new(),
        });
    }
}

fn parse_session(brain_dir: &Path) -> (Vec<Message>, Vec<Event>) {
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
        // missing transcript is normal (empty/new session dirs); only report real reads
        Err(e) if transcript.exists() => {
            eprintln!("  ! antigravity: cannot read {}: {e}", transcript.display());
            return (Vec::new(), Vec::new());
        }
        Err(_) => return (Vec::new(), Vec::new()),
    };

    // Parse every event once; we need a full pass for the dominant Cwd anyway.
    let events: Vec<TLine> = data
        .lines()
        .filter(|l| !l.is_empty())
        .filter_map(|l| serde_json::from_str::<TLine>(l).ok())
        .collect();

    let project = dominant_cwd(&events)
        .map(|cwd| project_name(&cwd))
        .unwrap_or_else(|| "antigravity".to_string());

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut evts: Vec<Event> = Vec::new();
    // PLANNER_RESPONSE announces tool calls (name+args); the matching action event
    // (RUN_COMMAND/VIEW_FILE/...) follows with the output. Pair them FIFO.
    let mut announced: VecDeque<(String, String)> = VecDeque::new();
    let mut seq = 0u32;
    let mut turn = 0u32;
    for e in &events {
        // The model's prose turn (PLANNER_RESPONSE) -> attach as the reply to the
        // user message it answers, and queue any announced tool calls.
        if e.ty.as_deref() == Some("PLANNER_RESPONSE") && e.source.as_deref() == Some("MODEL") {
            if let Some(txt) = e.content.as_ref().and_then(|c| c.as_str()) {
                if let Some(last) = out.last_mut() {
                    crate::ingest::append_capped(&mut last.reply, txt, 1600);
                }
            }
            if let Some(tcs) = &e.tool_calls {
                for tc in tcs {
                    let name = tc.name.clone().unwrap_or_else(|| "?".to_string());
                    let input = tc.args.as_ref().map(summarize_agy_args).unwrap_or_default();
                    announced.push_back((name, input));
                }
            }
            continue;
        }
        // Execution of an announced call: output in content, DONE/ERROR in status.
        if e.source.as_deref() == Some("MODEL") {
            if let Some(ty) = e.ty.as_deref() {
                if is_action_type(ty) {
                    let (name, input) = announced
                        .pop_front()
                        .unwrap_or_else(|| (ty.to_lowercase(), String::new()));
                    let output = e
                        .content
                        .as_ref()
                        .and_then(|c| c.as_str())
                        .map(|s| cap_str(s, EVENT_CAP))
                        .unwrap_or_default();
                    let ok = match e.status.as_deref() {
                        Some("DONE") => Some(true),
                        Some("ERROR") => Some(false),
                        _ => None,
                    };
                    seq += 1;
                    evts.push(Event {
                        agent: "antigravity",
                        session: session.clone(),
                        ts: ts_millis(e.created_at.as_deref()),
                        kind: "tool",
                        name,
                        input,
                        output,
                        ok,
                        call_id: format!("ag{}", seq),
                        child_session: String::new(),
                    });
                    continue;
                }
                if ty == "INVOKE_SUBAGENT" {
                    seq += 1;
                    evts.push(Event {
                        agent: "antigravity",
                        session: session.clone(),
                        ts: ts_millis(e.created_at.as_deref()),
                        kind: "subagent_start",
                        name: "subagent".to_string(),
                        input: e
                            .content
                            .as_ref()
                            .and_then(|c| c.as_str())
                            .map(|s| cap_str(s, EVENT_CAP))
                            .unwrap_or_default(),
                        output: String::new(),
                        ok: None,
                        call_id: format!("ag{}", seq),
                        child_session: String::new(),
                    });
                    continue;
                }
            }
        }
        // the user's own typed input only.
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
        out.push(crate::model::RawMessage {
            agent: "antigravity",
            project: project.clone(),
            session: session.clone(),
            ts: ts_millis(e.created_at.as_deref()),
            turn,
            text,
            model: String::new(),
            reply: String::new(),
        });
        turn += 1;
    }

    collect_mailbox(brain_dir, &session, &mut evts);
    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        evts,
    )
}

/// Walk every `brain/<uuid>/` session and collect the user's Antigravity prompts + events.
pub fn collect() -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home()
        .join(".gemini")
        .join("antigravity-cli")
        .join("brain");
    let dirs = match fs::read_dir(&root) {
        Ok(d) => d,
        Err(_) => return (Vec::new(), Vec::new()),
    };

    let session_dirs: Vec<std::path::PathBuf> = dirs
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect();

    let pairs: Vec<(Vec<Message>, Vec<Event>)> =
        session_dirs.par_iter().map(|d| parse_session(d)).collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e) in pairs {
        msgs.extend(m);
        evts.extend(e);
    }
    (msgs, evts)
}
