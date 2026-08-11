//! Antigravity (Google's "agy" CLI) adapter.
//!
//! Store layout: `~/.gemini/antigravity-cli/brain/<session-uuid>/`
//!   - `.system_generated/messages/*.json` is the *inter-agent mailbox* (sender =
//!     `<uuid>/task-N` task results, cross-uuid subagent `send_message`s, or
//!     `system` notices) - NONE of these are the user, so we ignore that dir entirely.
//!   - `.system_generated/logs/transcript.jsonl` is the real conversation log. Each
//!     line is an event `{type, source, content, created_at, tool_calls, ...}`.
//!
//! Discriminator for the user's own typed prompts: an event with `type == "USER_INPUT"` AND
//! `source == "USER_EXPLICIT"`. This pairing is exact and exclusive - USER_INPUT
//! only ever appears with USER_EXPLICIT, and the model's own turns use
//! `source == "MODEL"`, system text uses `source == "SYSTEM"`. The human's text is
//! wrapped as `<USER_REQUEST>...</USER_REQUEST>` followed by injected
//! `<ADDITIONAL_METADATA>` / `<USER_SETTINGS_CHANGE>` preambles; we extract only the
//! inner USER_REQUEST body and drop the injected wrappers.
//!
//! Project: there is no cwd field on the user events, so we infer the workspace from
//! the most frequent `tool_calls[*].args.Cwd` seen in the session's transcript
//! (dominant value is the working dir), then run
//! it through `project_name`. Falls back to "antigravity" when no Cwd is present.

use std::collections::{HashMap, VecDeque};
use std::fs;
use std::path::Path;

use rayon::prelude::*;
use serde::Deserialize;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_metadata};
use crate::ingest::{
    cap_event_output, cap_str, cap_str_with_chars, is_wrapper, project_name, EVENT_CAP,
};
use crate::model::{Event, Message};

fn plain_dir_chain(
    root: &Path,
    components: &[&str],
) -> std::io::Result<Option<std::path::PathBuf>> {
    let mut path = root.to_path_buf();
    for component in std::iter::once(None).chain(components.iter().copied().map(Some)) {
        if let Some(component) = component {
            path.push(component);
        }
        match fs::symlink_metadata(&path) {
            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
            Ok(_) => return Ok(None),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        }
    }
    Ok(Some(path))
}

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
fn summarize_agy_args(args: &serde_json::Value) -> (String, usize) {
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
                    return cap_str_with_chars(&s, EVENT_CAP);
                }
            }
        }
    }
    cap_str_with_chars(&args.to_string(), EVENT_CAP)
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

/// Injected `<ADDITIONAL_METADATA>`/`<USER_SETTINGS_CHANGE>` blocks that reach the
/// fallback path when an event carries no `<USER_REQUEST>` wrapper at all
/// (settings-change-only events); they are not something the user typed.
fn is_injected_metadata(text: &str) -> bool {
    let t = text.trim_start();
    t.starts_with("<ADDITIONAL_METADATA") || t.starts_with("<USER_SETTINGS_CHANGE")
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
fn collect_mailbox(brain_dir: &Path, session: &str, evts: &mut Vec<Event>) -> bool {
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

    let dir = match plain_dir_chain(brain_dir, &[".system_generated", "messages"]) {
        Ok(Some(dir)) => dir,
        Ok(None) => return true,
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", brain_dir, &error);
            return false;
        }
    };
    let rd = match fs::read_dir(&dir) {
        Ok(rd) => rd,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return true,
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", &dir, &error);
            return false;
        }
    };
    let mut healthy = true;
    for entry in rd {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("antigravity", &dir, &error);
                healthy = false;
                continue;
            }
        };
        let p = entry.path();
        let is_file = match plain_metadata(&p) {
            Ok(Some(meta)) => meta.is_file(),
            Ok(None) => false,
            Err(error) => {
                crate::ingest::warn_source_skip("antigravity", &p, &error);
                healthy = false;
                continue;
            }
        };
        if !is_file {
            continue;
        }
        if p.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        // seen = this mailbox JSON record
        let tally = crate::intake::file("antigravity", &p);
        tally.seen();
        let data = match crate::ingest::read_lossy(&p) {
            Ok(d) => d,
            Err(e) => {
                crate::ingest::warn_source_skip("antigravity", &p, &e);
                tally.error(&format!("cannot read: {e}"));
                healthy = false;
                continue;
            }
        };
        let m: Mail = match serde_json::from_str(&data) {
            Ok(m) => m,
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(&data, 80)));
                continue;
            }
        };
        let sender = m.sender.as_deref().unwrap_or("");
        if !sender.contains("/task-") {
            tally.skip(crate::intake::Skip::Meta);
            continue; // cross-agent mail + system notices; only task results are subagent output
        }
        let task = sender.rsplit('/').next().unwrap_or(sender).to_string();
        let ts = m.timestamp.as_ref().map(parse_timestamp::json).unwrap_or(0);
        let title = m
            .render
            .as_ref()
            .and_then(|r| r.title.as_deref())
            .unwrap_or("");
        let input_chars = task.chars().count();
        let (output, output_chars, output_bytes) = m
            .content
            .as_deref()
            .map(cap_event_output)
            .unwrap_or_default();
        tally.agent_row();
        tally.event();
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
            output,
            input_chars,
            output_chars,
            output_bytes,
            ok: None,
            call_id: m.id.unwrap_or_else(|| {
                p.file_stem()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_default()
            }),
            child_session: String::new(),
        });
    }
    healthy
}

fn parse_session(brain_dir: &Path) -> (Vec<Message>, Vec<Event>, bool) {
    let session = brain_dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();

    let logs = match plain_dir_chain(brain_dir, &[".system_generated", "logs"]) {
        Ok(Some(logs)) => logs,
        Ok(None) => return (Vec::new(), Vec::new(), true),
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", brain_dir, &error);
            return (Vec::new(), Vec::new(), false);
        }
    };
    let transcript = logs.join("transcript.jsonl");
    match fs::symlink_metadata(&transcript) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => {}
        Ok(_) => return (Vec::new(), Vec::new(), true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (Vec::new(), Vec::new(), true)
        }
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", &transcript, &error);
            return (Vec::new(), Vec::new(), false);
        }
    }
    // seen = non-blank transcript.jsonl events
    let tally = crate::intake::file("antigravity", &transcript);
    let data = match crate::ingest::read_lossy(&transcript) {
        Ok(d) => d,
        Err(e) => {
            eprintln!(
                "  ! antigravity: cannot read {}: {}",
                crate::ingest::terminal_safe(transcript.display()),
                crate::ingest::terminal_safe(&e)
            );
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };

    // Parse every event once; we need a full pass for the dominant Cwd anyway.
    let mut events: Vec<TLine> = Vec::new();
    let mut healthy = true;
    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        tally.seen();
        match serde_json::from_str::<TLine>(line) {
            Ok(event) => events.push(event),
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(line, 80)));
            }
        }
    }

    let project = dominant_cwd(&events)
        .map(|cwd| project_name(&cwd))
        .unwrap_or_else(|| "antigravity".to_string());

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut evts: Vec<Event> = Vec::new();
    // PLANNER_RESPONSE announces tool calls; the matching action event follows with the output - pair FIFO.
    let mut announced: VecDeque<(String, String, usize)> = VecDeque::new();
    let mut seq = 0u32;
    let mut turn = 0u32;
    for e in &events {
        // The model's prose turn (PLANNER_RESPONSE) -> attach as the reply to the
        // user message it answers, and queue any announced tool calls.
        if e.ty.as_deref() == Some("PLANNER_RESPONSE") && e.source.as_deref() == Some("MODEL") {
            // A new planner step supersedes announced-but-never-executed calls
            // (rejected/cancelled); a stale head would misname every later pairing.
            announced.clear();
            if let Some(txt) = e.content.as_ref().and_then(|c| c.as_str()) {
                if let Some(last) = out.last_mut() {
                    let chars = crate::ingest::append_capped(
                        &mut last.reply,
                        txt,
                        crate::ingest::REPLY_CAP,
                    );
                    last.reply_chars += chars;
                }
            }
            if let Some(tcs) = &e.tool_calls {
                for tc in tcs {
                    let name = tc.name.clone().unwrap_or_else(|| "?".to_string());
                    let (input, input_chars) =
                        tc.args.as_ref().map(summarize_agy_args).unwrap_or_default();
                    announced.push_back((name, input, input_chars));
                }
            }
            tally.agent_row();
            continue;
        }
        // Execution of an announced call: output in content, DONE/ERROR in status.
        if e.source.as_deref() == Some("MODEL") {
            if let Some(ty) = e.ty.as_deref() {
                if is_action_type(ty) {
                    let (name, input, input_chars) = announced
                        .pop_front()
                        .unwrap_or_else(|| (ty.to_lowercase(), String::new(), 0));
                    let (output, output_chars, output_bytes) = e
                        .content
                        .as_ref()
                        .and_then(|c| c.as_str())
                        .map(cap_event_output)
                        .unwrap_or_default();
                    let ok = match e.status.as_deref() {
                        Some("DONE") => Some(true),
                        Some("ERROR") => Some(false),
                        _ => None,
                    };
                    seq += 1;
                    tally.agent_row();
                    tally.event();
                    evts.push(Event {
                        agent: "antigravity",
                        session: session.clone(),
                        ts: parse_timestamp::rfc3339(e.created_at.as_deref()),
                        kind: "tool",
                        name,
                        input,
                        output,
                        input_chars,
                        output_chars,
                        output_bytes,
                        ok,
                        call_id: format!("ag{}", seq),
                        child_session: String::new(),
                    });
                    continue;
                }
                if ty == "INVOKE_SUBAGENT" {
                    // Consume its announcement so later actions in this step stay paired.
                    if announced
                        .front()
                        .is_some_and(|(name, _, _)| name.eq_ignore_ascii_case(ty))
                    {
                        announced.pop_front();
                    }
                    seq += 1;
                    tally.agent_row();
                    tally.event();
                    let (input, input_chars) = e
                        .content
                        .as_ref()
                        .and_then(|c| c.as_str())
                        .map(|s| cap_str_with_chars(s, EVENT_CAP))
                        .unwrap_or_default();
                    evts.push(Event {
                        agent: "antigravity",
                        session: session.clone(),
                        ts: parse_timestamp::rfc3339(e.created_at.as_deref()),
                        kind: "subagent_start",
                        name: "subagent".to_string(),
                        input,
                        output: String::new(),
                        input_chars,
                        output_chars: 0,
                        output_bytes: 0,
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
            tally.skip(crate::intake::Skip::NonMessage);
            continue;
        }
        let raw = match e.content.as_ref().and_then(|c| c.as_str()) {
            Some(s) => s,
            None => {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
        };
        let text = match extract_user_request(raw) {
            Some(t) => t,
            None => {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
        };
        if is_wrapper(&text) || is_injected_metadata(&text) {
            tally.skip(crate::intake::Skip::Wrapper);
            continue;
        }
        tally.row();
        out.push(crate::model::RawMessage {
            agent: "antigravity",
            project: project.clone(),
            session: session.clone(),
            ts: parse_timestamp::rfc3339(e.created_at.as_deref()),
            turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
        });
        turn += 1;
    }

    healthy &= collect_mailbox(brain_dir, &session, &mut evts);
    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        evts,
        healthy,
    )
}

/// Walk every `brain/<uuid>/` session and collect the user's Antigravity prompts + events.
fn collect_from_root(root: &Path) -> (Vec<Message>, Vec<Event>, bool) {
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => return (Vec::new(), Vec::new(), true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (Vec::new(), Vec::new(), true)
        }
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", root, &error);
            return (Vec::new(), Vec::new(), false);
        }
    }
    let dirs = match fs::read_dir(root) {
        Ok(d) => d,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (Vec::new(), Vec::new(), true)
        }
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", root, &error);
            return (Vec::new(), Vec::new(), false);
        }
    };

    let mut session_dirs = Vec::new();
    let mut healthy = true;
    for entry in dirs {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("antigravity", root, &error);
                healthy = false;
                continue;
            }
        };
        let path = entry.path();
        match plain_metadata(&path) {
            Ok(Some(meta)) if meta.is_dir() => session_dirs.push(path),
            Ok(_) => {}
            Err(error) => {
                crate::ingest::warn_source_skip("antigravity", &path, &error);
                healthy = false;
            }
        }
    }

    let pairs: Vec<(Vec<Message>, Vec<Event>, bool)> =
        session_dirs.par_iter().map(|d| parse_session(d)).collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e, session_healthy) in pairs {
        if session_healthy {
            msgs.extend(m);
            evts.extend(e);
        } else {
            healthy = false;
        }
    }
    (msgs, evts, healthy)
}

pub fn collect() -> (Vec<Message>, Vec<Event>, bool) {
    collect_from_root(
        &crate::ingest::home()
            .join(".gemini")
            .join("antigravity-cli")
            .join("brain"),
    )
}

/// Registry entry (see ingest::registry). Full-parse: ignores the cache.
pub struct Antigravity;
impl crate::ingest::registry::Adapter for Antigravity {
    fn name(&self) -> &'static str {
        "antigravity"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Always
    }
    fn collect(&self, _cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        let (messages, events, _) = collect();
        (messages, events)
    }
    fn collect_checked(
        &self,
        _cache: &mut crate::ingest_cache::IngestCache,
    ) -> (
        Vec<Message>,
        Vec<Event>,
        crate::ingest_cache::ReadOutcome,
        Vec<crate::ingest_cache::SourceReadIssue>,
    ) {
        let (messages, events, healthy) = collect();
        let outcome = if healthy {
            crate::ingest_cache::ReadOutcome::Complete
        } else {
            crate::ingest_cache::ReadOutcome::Skipped
        };
        (messages, events, outcome, Vec::new())
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home()
            .join(".gemini")
            .join("antigravity-cli")
            .join("brain")]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        matches!(
            path.extension().and_then(|x| x.to_str()),
            Some("json") | Some("jsonl")
        )
    }
    fn runtime_issue_root(&self) -> std::path::PathBuf {
        crate::ingest::home()
            .join(".gemini")
            .join("antigravity-cli")
            .join("brain")
    }
}

#[cfg(test)]
mod tests {
    use super::parse_session;

    fn brain_dir(name: &str, transcript: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "agrep-agy-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let logs = dir.join(".system_generated").join("logs");
        std::fs::create_dir_all(&logs).unwrap();
        std::fs::write(logs.join("transcript.jsonl"), transcript).unwrap();
        dir
    }

    #[test]
    fn unexecuted_announcement_does_not_shift_later_pairings() {
        // The view_file announcement is never executed (rejected); the next planner
        // step's run_command must not inherit its name/input.
        let dir = brain_dir(
            "stale",
            concat!(
                r#"{"type":"PLANNER_RESPONSE","source":"MODEL","created_at":"2026-01-02T10:00:00Z","content":"I'll open the file.","tool_calls":[{"name":"view_file","args":{"AbsolutePath":"/tmp/a.txt"}}]}"#,
                "\n",
                r#"{"type":"PLANNER_RESPONSE","source":"MODEL","created_at":"2026-01-02T10:00:10Z","content":"Running tests instead.","tool_calls":[{"name":"run_command","args":{"CommandLine":"cargo test","Cwd":"/work/proj"}}]}"#,
                "\n",
                r#"{"type":"RUN_COMMAND","source":"MODEL","status":"DONE","created_at":"2026-01-02T10:00:11Z","content":"ok"}"#,
                "\n",
            ),
        );
        let (_, events, healthy) = parse_session(&dir);
        assert!(healthy);
        let tools: Vec<_> = events.iter().filter(|e| e.kind == "tool").collect();
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].name, "run_command");
        assert_eq!(tools[0].input, "cargo test");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn subagent_invocation_consumes_its_announcement() {
        let dir = brain_dir(
            "subagent",
            concat!(
                r#"{"type":"PLANNER_RESPONSE","source":"MODEL","created_at":"2026-01-02T10:00:00Z","content":"Delegating, then testing.","tool_calls":[{"name":"invoke_subagent","args":{"toolSummary":"audit the lexer"}},{"name":"run_command","args":{"CommandLine":"cargo test","Cwd":"/work/proj"}}]}"#,
                "\n",
                r#"{"type":"INVOKE_SUBAGENT","source":"MODEL","created_at":"2026-01-02T10:00:01Z","content":"audit the lexer"}"#,
                "\n",
                r#"{"type":"RUN_COMMAND","source":"MODEL","status":"DONE","created_at":"2026-01-02T10:00:02Z","content":"ok"}"#,
                "\n",
            ),
        );
        let (_, events, healthy) = parse_session(&dir);
        assert!(healthy);
        let tools: Vec<_> = events.iter().filter(|e| e.kind == "tool").collect();
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].name, "run_command");
        assert_eq!(tools[0].input, "cargo test");
        assert!(events.iter().any(|e| e.kind == "subagent_start"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn settings_only_events_are_not_user_words() {
        let dir = brain_dir(
            "settings",
            concat!(
                r#"{"type":"USER_INPUT","source":"USER_EXPLICIT","created_at":"2026-01-02T10:00:00Z","content":"<USER_SETTINGS_CHANGE>autonomy: turbo</USER_SETTINGS_CHANGE>"}"#,
                "\n",
                r#"{"type":"USER_INPUT","source":"USER_EXPLICIT","created_at":"2026-01-02T10:00:01Z","content":"<ADDITIONAL_METADATA>{\"open_files\":[]}</ADDITIONAL_METADATA><USER_SETTINGS_CHANGE>mode</USER_SETTINGS_CHANGE>"}"#,
                "\n",
                r#"{"type":"USER_INPUT","source":"USER_EXPLICIT","created_at":"2026-01-02T10:00:02Z","content":"<USER_REQUEST>real question</USER_REQUEST>"}"#,
                "\n",
                r#"{"type":"USER_INPUT","source":"USER_EXPLICIT","created_at":"2026-01-02T10:00:03Z","content":"plain old-build prompt"}"#,
                "\n",
            ),
        );
        let (messages, _, healthy) = parse_session(&dir);
        assert!(healthy);
        let texts: Vec<&str> = messages.iter().map(|m| &*m.text).collect();
        assert_eq!(texts, vec!["real question", "plain old-build prompt"]);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(unix)]
    #[test]
    fn parse_rejects_symlinked_transcript_and_mailbox_paths() {
        use std::os::unix::fs::symlink;

        let dir = brain_dir(
            "links",
            "{\"type\":\"USER_INPUT\",\"source\":\"USER_EXPLICIT\",\"content\":\"inside\"}\n",
        );
        let outside = dir.with_extension("outside");
        let outside_logs = outside.join("logs");
        std::fs::create_dir_all(&outside_logs).unwrap();
        let outside_transcript = outside_logs.join("transcript.jsonl");
        std::fs::write(
            &outside_transcript,
            "{\"type\":\"USER_INPUT\",\"source\":\"USER_EXPLICIT\",\"content\":\"outside\"}\n",
        )
        .unwrap();
        let transcript = dir.join(".system_generated/logs/transcript.jsonl");
        std::fs::remove_file(&transcript).unwrap();
        symlink(&outside_transcript, &transcript).unwrap();
        let (messages, events, healthy) = parse_session(&dir);
        assert!(healthy);
        assert!(messages.is_empty());
        assert!(events.is_empty());

        std::fs::remove_file(&transcript).unwrap();
        std::fs::write(
            &transcript,
            "{\"type\":\"USER_INPUT\",\"source\":\"USER_EXPLICIT\",\"content\":\"inside\"}\n",
        )
        .unwrap();
        let messages_dir = dir.join(".system_generated/messages");
        std::fs::create_dir_all(&messages_dir).unwrap();
        let outside_mail = outside.join("mail.json");
        std::fs::write(
            &outside_mail,
            r#"{"id":"outside","sender":"root/task-1","content":"outside"}"#,
        )
        .unwrap();
        symlink(&outside_mail, messages_dir.join("linked.json")).unwrap();
        let (messages, events, healthy) = parse_session(&dir);
        assert!(healthy);
        assert_eq!(messages.len(), 1);
        assert!(events.is_empty());
        let _ = std::fs::remove_dir_all(dir);
        let _ = std::fs::remove_dir_all(outside);
    }

    #[cfg(unix)]
    #[test]
    fn discovery_rejects_symlinked_session_and_nested_log_dirs() {
        use std::os::unix::fs::symlink;

        let root = brain_dir("root-placeholder", "");
        std::fs::remove_dir_all(&root).unwrap();
        std::fs::create_dir_all(&root).unwrap();
        let outside = brain_dir(
            "outside-session",
            "{\"type\":\"USER_INPUT\",\"source\":\"USER_EXPLICIT\",\"content\":\"outside\"}\n",
        );
        symlink(&outside, root.join("linked-session")).unwrap();

        let nested = root.join("nested");
        std::fs::create_dir_all(nested.join(".system_generated")).unwrap();
        symlink(
            outside.join(".system_generated/logs"),
            nested.join(".system_generated/logs"),
        )
        .unwrap();
        let (messages, events, healthy) = super::collect_from_root(&root);
        assert!(healthy);
        assert!(messages.is_empty());
        assert!(events.is_empty());
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(outside);
    }
}
