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
use std::path::{Path, PathBuf};

use rayon::prelude::*;
use serde::Deserialize;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_metadata};
use crate::ingest::{
    cap_event_output, cap_str, cap_str_with_chars, is_wrapper, project_name, EVENT_CAP,
};
use crate::intake::{Skip, Tally};
use crate::intake_batch::SourceReadIssue;
use crate::model::{Event, Message, RawMessage};

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

/// The inter-agent mailbox: `.system_generated/messages/*.json`, each
/// `{sender:"<uuid>/task-N"|..., timestamp, renderDetails:{messageTitle}, content}`.
/// Task-result mail becomes subagent_result events; system notices are skipped.
fn collect_mailbox(brain_dir: &Path, session: &str, evts: &mut Vec<Event>) -> bool {
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
        healthy &= read_mail(&p, session, evts);
    }
    healthy
}

/// One mailbox JSON record; false when it cannot be read.
fn read_mail(p: &Path, session: &str, evts: &mut Vec<Event>) -> bool {
    // seen = this mailbox JSON record
    let tally = crate::intake::tally();
    tally.seen();
    let data = match crate::ingest::read_lossy(p) {
        Ok(d) => d,
        Err(e) => {
            crate::ingest::warn_source_skip("antigravity", p, &e);
            tally.error(&format!("cannot read: {e}"));
            return false;
        }
    };
    let m: Mail = match serde_json::from_str(&data) {
        Ok(m) => m,
        Err(e) => {
            tally.error(&format!("{e}: {}", crate::intake::clip(&data, 80)));
            return true;
        }
    };
    let sender = m.sender.as_deref().unwrap_or("");
    if !sender.contains("/task-") {
        // cross-agent mail + system notices; only task results are subagent output
        tally.skip(Skip::Meta);
        return true;
    }
    let task = sender.rsplit('/').next().unwrap_or(sender).to_string();
    tally.agent_row();
    tally.event();
    evts.push(task_result(m, task, p, session));
    true
}

/// A task's result mail as a subagent_result event.
fn task_result(m: Mail, task: String, p: &Path, session: &str) -> Event {
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
    Event {
        turn: None,
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
        meta: String::new(),
        state: String::new(),
    }
}

fn parse_session(brain_dir: &Path) -> (Vec<Message>, Vec<Event>, bool) {
    let session = brain_dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let transcript = match transcript_path(brain_dir) {
        Ok(transcript) => transcript,
        Err(healthy) => return (Vec::new(), Vec::new(), healthy),
    };
    // seen = non-blank transcript.jsonl events
    let tally = crate::intake::tally();
    let data = match crate::ingest::read_lossy(&transcript) {
        Ok(d) => d,
        Err(e) => {
            crate::intake::diagnostic(format_args!(
                "  ! antigravity: cannot read {}: {}",
                crate::ingest::terminal_safe(transcript.display()),
                crate::ingest::terminal_safe(&e)
            ));
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };

    // Parse every event once; we need a full pass for the dominant Cwd anyway.
    let events = transcript_events(&data, &tally);
    let project = dominant_cwd(&events)
        .map(|cwd| project_name(&cwd))
        .unwrap_or_else(|| "antigravity".to_string());

    let mut parse = TranscriptParse {
        session: &session,
        project: &project,
        tally: &tally,
        out: Vec::new(),
        evts: Vec::new(),
        announced: VecDeque::new(),
        seq: 0,
        turn: 0,
        worked: false,
    };
    for e in &events {
        parse.event(e);
    }
    let TranscriptParse { out, mut evts, .. } = parse;
    let healthy = collect_mailbox(brain_dir, &session, &mut evts);
    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        evts,
        healthy,
    )
}

/// `.system_generated/logs/transcript.jsonl` when it is a plain file under plain directories,
/// or whether the session is healthy without one.
fn transcript_path(brain_dir: &Path) -> Result<PathBuf, bool> {
    let logs = match plain_dir_chain(brain_dir, &[".system_generated", "logs"]) {
        Ok(Some(logs)) => logs,
        Ok(None) => return Err(true),
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", brain_dir, &error);
            return Err(false);
        }
    };
    let transcript = logs.join("transcript.jsonl");
    match fs::symlink_metadata(&transcript) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => Ok(transcript),
        Ok(_) => Err(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Err(true),
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", &transcript, &error);
            Err(false)
        }
    }
}

fn transcript_events(data: &str, tally: &Tally) -> Vec<TLine> {
    let mut events: Vec<TLine> = Vec::new();
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
    events
}

/// One session's transcript events read in order.
struct TranscriptParse<'a> {
    session: &'a str,
    project: &'a str,
    tally: &'a Tally,
    out: Vec<RawMessage>,
    evts: Vec<Event>,
    /// PLANNER_RESPONSE announces tool calls; the matching action event follows with the
    /// output - pair FIFO.
    announced: VecDeque<(String, String, usize)>,
    seq: u32,
    turn: u32,
    /// The transcript orders prose and actions: an action sets the flag the next prose consumes.
    worked: bool,
}

impl TranscriptParse<'_> {
    fn event(&mut self, e: &TLine) {
        if e.ty.as_deref() == Some("PLANNER_RESPONSE") && e.source.as_deref() == Some("MODEL") {
            self.planner_response(e);
            return;
        }
        if e.source.as_deref() == Some("MODEL") {
            match e.ty.as_deref() {
                Some(ty) if is_action_type(ty) => {
                    self.action(e, ty);
                    return;
                }
                Some(ty @ "INVOKE_SUBAGENT") => {
                    self.subagent_start(e, ty);
                    return;
                }
                _ => {}
            }
        }
        self.user_input(e);
    }

    /// The model's prose turn: attach as the reply to the user message it answers, and queue
    /// any announced tool calls.
    fn planner_response(&mut self, e: &TLine) {
        // A new planner step supersedes announced-but-never-executed calls
        // (rejected/cancelled); a stale head would misname every later pairing.
        self.announced.clear();
        if let Some(txt) = e.content.as_ref().and_then(|c| c.as_str()) {
            if let Some(last) = self.out.last_mut() {
                crate::ingest::append_reply(last, txt);
                crate::ingest::ReplyOrder::joined_known(&mut last.reply_shape, self.worked);
                self.worked = false;
            }
        }
        if let Some(tcs) = &e.tool_calls {
            for tc in tcs {
                let name = tc.name.clone().unwrap_or_else(|| "?".to_string());
                let (input, input_chars) =
                    tc.args.as_ref().map(summarize_agy_args).unwrap_or_default();
                self.announced.push_back((name, input, input_chars));
            }
        }
        self.tally.agent_row();
    }

    /// Execution of an announced call: output in content, DONE/ERROR in status.
    fn action(&mut self, e: &TLine, ty: &str) {
        let (name, input, input_chars) = self
            .announced
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
        self.seq += 1;
        self.tally.agent_row();
        self.tally.event();
        self.worked |= !crate::ingest::is_bookkeeping_tool(&name);
        self.evts.push(Event {
            turn: None,
            agent: "antigravity",
            session: self.session.to_string(),
            ts: parse_timestamp::rfc3339(e.created_at.as_deref()),
            kind: "tool",
            name,
            input,
            output,
            input_chars,
            output_chars,
            output_bytes,
            ok,
            call_id: format!("ag{}", self.seq),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        });
    }

    fn subagent_start(&mut self, e: &TLine, ty: &str) {
        // Consume its announcement so later actions in this step stay paired.
        if self
            .announced
            .front()
            .is_some_and(|(name, _, _)| name.eq_ignore_ascii_case(ty))
        {
            self.announced.pop_front();
        }
        self.seq += 1;
        self.tally.agent_row();
        self.tally.event();
        self.worked = true;
        let (input, input_chars) = e
            .content
            .as_ref()
            .and_then(|c| c.as_str())
            .map(|s| cap_str_with_chars(s, EVENT_CAP))
            .unwrap_or_default();
        self.evts.push(Event {
            turn: None,
            agent: "antigravity",
            session: self.session.to_string(),
            ts: parse_timestamp::rfc3339(e.created_at.as_deref()),
            kind: "subagent_start",
            name: "subagent".to_string(),
            input,
            output: String::new(),
            input_chars,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id: format!("ag{}", self.seq),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        });
    }

    /// The user's own typed input only.
    fn user_input(&mut self, e: &TLine) {
        if e.ty.as_deref() != Some("USER_INPUT") || e.source.as_deref() != Some("USER_EXPLICIT") {
            self.tally.skip(Skip::NonMessage);
            return;
        }
        let Some(raw) = e.content.as_ref().and_then(|c| c.as_str()) else {
            self.tally.skip(Skip::EmptyText);
            return;
        };
        let Some(text) = extract_user_request(raw) else {
            self.tally.skip(Skip::EmptyText);
            return;
        };
        if is_wrapper(&text) || is_injected_metadata(&text) {
            self.tally.skip(Skip::Wrapper);
            return;
        }
        self.tally.row();
        self.worked = false;
        self.out.push(RawMessage {
            agent: "antigravity",
            project: self.project.to_string(),
            session: self.session.to_string(),
            ts: parse_timestamp::rfc3339(e.created_at.as_deref()),
            turn: self.turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }
}

type Collected = (Vec<Message>, Vec<Event>, bool, Vec<SourceReadIssue>);

/// Walk every `brain/<uuid>/` session and collect the user's Antigravity prompts + events.
fn collect_from_root(root: &Path) -> Collected {
    let dirs = match read_root(root) {
        Ok(dirs) => dirs,
        Err(healthy) => return (Vec::new(), Vec::new(), healthy, Vec::new()),
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
    parse_all(&session_dirs, healthy)
}

/// The store root's listing, or whether the store is healthy without one.
fn read_root(root: &Path) -> Result<fs::ReadDir, bool> {
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => return Err(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Err(true),
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", root, &error);
            return Err(false);
        }
    }
    match fs::read_dir(root) {
        Ok(d) => Ok(d),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Err(true),
        Err(error) => {
            crate::ingest::warn_source_skip("antigravity", root, &error);
            Err(false)
        }
    }
}

/// Parse every session directory in parallel, isolating panics, and merge in walk order.
fn parse_all(session_dirs: &[PathBuf], mut healthy: bool) -> Collected {
    let parsed: Vec<_> = session_dirs
        .par_iter()
        .map(|d| {
            crate::ingest::parse_guard::isolate("antigravity", d, || parse_session(d)).map_err(
                |panic| SourceReadIssue::new("antigravity", d, "source-read-failed", panic.reason),
            )
        })
        .collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    let mut issues = Vec::new();
    for session in parsed {
        match session {
            Ok((m, e, true)) => {
                msgs.extend(m);
                evts.extend(e);
            }
            Ok((_, _, false)) => healthy = false,
            Err(issue) => {
                healthy = false;
                issues.push(issue);
            }
        }
    }
    (msgs, evts, healthy, issues)
}

pub fn collect() -> Collected {
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
    fn collect(&self, _cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        let (messages, events, _, _) = collect();
        (messages, events)
    }
    fn collect_checked(
        &self,
        _cache: &mut crate::intake_batch::IntakeBatch,
    ) -> (
        Vec<Message>,
        Vec<Event>,
        crate::intake_batch::ReadOutcome,
        Vec<SourceReadIssue>,
    ) {
        let (messages, events, healthy, issues) = collect();
        let outcome = if healthy {
            crate::intake_batch::ReadOutcome::Complete
        } else {
            crate::intake_batch::ReadOutcome::Skipped
        };
        (messages, events, outcome, issues)
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
