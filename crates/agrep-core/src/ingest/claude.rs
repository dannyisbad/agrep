//! Claude Code adapter: ~/.claude/projects/<cwd-slug>/*.jsonl
//! Session files plus `subagents/` side transcripts; structurally identified
//! throwaway-worker records are accounted as named skips.
//! Keeps real human turns: type=user, not meta/sidechain, userType external|absent,
//! string or text-block content, not a command/system wrapper.

use std::borrow::Cow;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

use memchr::memmem;
use rayon::prelude::*;
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata, StoreLink};
use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, project_name,
    summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::model::{Event, Message};

/// Control-event names for structural moments; they are never tool calls.
pub const API_ERROR_MARKER: &str = "api_error";
pub const TASK_NOTIFICATION_MARKER: &str = "task_notification";

// Borrowed deserialization: scalar fields are Cow (borrow from the line when escape-free,
// allocate only when JSON-escaped) and `content` stays a RawValue slice, parsed into a
// DOM only by the lines that actually need one - most fail a filter first.
#[derive(Deserialize)]
struct Line<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(rename = "isMeta")]
    is_meta: Option<bool>,
    #[serde(rename = "isSidechain")]
    is_sidechain: Option<bool>,
    #[serde(rename = "isCompactSummary")]
    is_compact_summary: Option<bool>,
    #[serde(rename = "userType", borrow)]
    user_type: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Msg<'a>>,
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    #[serde(rename = "sessionId", borrow)]
    session_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    cwd: Option<&'a RawValue>,
    // Raw so an unexpected shape degrades to "not a marker" instead of failing the row.
    #[serde(rename = "isApiErrorMessage", borrow)]
    is_api_error: Option<&'a RawValue>,
    #[serde(borrow)]
    origin: Option<&'a RawValue>,
    #[serde(borrow)]
    attachment: Option<&'a RawValue>,
    // the tool's structured result beside its display text; raw, parsed only for agent calls
    #[serde(rename = "toolUseResult", borrow)]
    tool_use_result: Option<&'a RawValue>,
}

/// A `queued_command` attachment: a prompt queued while a turn ran and delivered into it.
#[derive(Deserialize)]
struct QueuedCommand<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(rename = "commandMode", borrow)]
    command_mode: Option<Cow<'a, str>>,
    #[serde(borrow)]
    origin: Option<&'a RawValue>,
    #[serde(borrow)]
    prompt: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct Origin<'a> {
    #[serde(borrow)]
    kind: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
struct ProjectLine<'a> {
    #[serde(borrow)]
    cwd: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct Msg<'a> {
    #[serde(borrow)]
    role: Option<Cow<'a, str>>,
    #[serde(borrow)]
    content: Option<&'a RawValue>,
    #[serde(borrow)]
    model: Option<Cow<'a, str>>,
}

/// Extract the raw (still-escaped) bytes of the JSON string value that follows `finder`'s
/// `"key":"` needle. The escaped form is fine for project-histogram keys: segmentation
/// splits on both slash kinds and drops empty segments, so `C:\\Users` (escaped) and
/// `C:\Users` bucket to the same project root.
///
/// SOUNDNESS of raw-byte key needles, here and in the prefilters: inside any JSON string
/// value a quote is escaped to `\"`, so the byte sequence `"key":` can only occur as a
/// real object key - string contents can never produce a false match on it.
fn raw_str_value<'a>(finder: &memmem::Finder, bytes: &'a [u8]) -> Option<&'a str> {
    let start = finder.find(bytes)? + finder.needle().len();
    let mut j = start;
    while j < bytes.len() {
        match bytes[j] {
            b'\\' => j += 2,
            b'"' => return std::str::from_utf8(&bytes[start..j]).ok(),
            _ => j += 1,
        }
    }
    None
}

fn has_json_key(finder: &memmem::Finder, bytes: &[u8]) -> bool {
    let mut offset = 0;
    while let Some(found) = finder.find(&bytes[offset..]) {
        let mut cursor = offset + found + finder.needle().len();
        while matches!(bytes.get(cursor), Some(b' ' | b'\t' | b'\r' | b'\n')) {
            cursor += 1;
        }
        if bytes.get(cursor) == Some(&b':') {
            return true;
        }
        offset = cursor.min(bytes.len());
        if offset == bytes.len() {
            break;
        }
    }
    false
}

fn optional_cwd(value: Option<&RawValue>) -> Option<String> {
    value
        .and_then(|raw| serde_json::from_str::<String>(raw.get()).ok())
        .filter(|cwd| !cwd.is_empty())
}

fn is_api_error_row(line: &Line) -> bool {
    line.is_api_error.is_some_and(|raw| raw.get() == "true")
}

fn is_task_notification_row(line: &Line) -> bool {
    line.origin
        .and_then(|raw| serde_json::from_str::<Origin>(raw.get()).ok())
        .is_some_and(|origin| origin.kind.as_deref() == Some("task-notification"))
}

/// The prompt of a queued-command attachment carrying a task notification, the mid-turn form of
/// an origin-tagged user row. As in Claude Code, a set origin decides; only a missing or null
/// origin falls back to `commandMode`.
fn queued_notification_prompt(attachment: Option<&RawValue>) -> Option<serde_json::Value> {
    let queued: QueuedCommand = serde_json::from_str(attachment?.get()).ok()?;
    if queued.ty.as_deref() != Some("queued_command") {
        return None;
    }
    let kind = match queued.origin {
        Some(raw) => serde_json::from_str::<Origin>(raw.get()).ok()?.kind,
        None => queued.command_mode,
    };
    if kind.as_deref() != Some("task-notification") {
        return None;
    }
    Some(
        queued
            .prompt
            .and_then(|raw| serde_json::from_str(raw.get()).ok())
            .unwrap_or_default(),
    )
}

/// One `<task-notification>` block's leading header tags. Claude Code writes `<task-id>`,
/// `<tool-use-id>`, `<task-type>`, `<output-file>`, `<status>`, `<summary>` (each optional, in
/// that order) before any body, so only that leading run is read: a result body can quote tags.
#[derive(Debug, Default, PartialEq)]
struct TaskNotification<'a> {
    task_id: Option<&'a str>,
    tool_use_id: Option<&'a str>,
    status: Option<&'a str>,
}

/// The text blocks of a row's content. Each carries at most one notification, its first
/// opening tag, so a notification quoted inside a result body is never read as another.
fn content_texts(content: &serde_json::Value) -> Vec<&str> {
    match content {
        serde_json::Value::String(text) => vec![text.as_str()],
        serde_json::Value::Array(blocks) => blocks
            .iter()
            .filter(|b| b.get("type").and_then(|t| t.as_str()) == Some("text"))
            .filter_map(|b| b.get("text").and_then(|t| t.as_str()))
            .collect(),
        _ => Vec::new(),
    }
}

fn task_notification(text: &str) -> Option<TaskNotification<'_>> {
    const OPEN: &str = "<task-notification";
    let mut rest = text;
    let after = loop {
        let after = &rest[rest.find(OPEN)? + OPEN.len()..];
        if after.starts_with(|c: char| c == '>' || c.is_whitespace()) {
            break after;
        }
        rest = after;
    };
    let mut cursor = &after[after.find('>')? + 1..];
    let mut found = TaskNotification::default();
    while let Some((tag, value, next)) = leading_tag(cursor) {
        let slot = match tag {
            "task-id" => Some(&mut found.task_id),
            "tool-use-id" => Some(&mut found.tool_use_id),
            "status" => Some(&mut found.status),
            _ => None,
        };
        if let Some(slot) = slot {
            slot.get_or_insert(value);
        }
        cursor = next;
    }
    Some(found)
}

/// `<tag>value</tag>` at the start of `text` (after whitespace), with a markup-free value.
fn leading_tag(text: &str) -> Option<(&str, &str, &str)> {
    let text = text.trim_start().strip_prefix('<')?;
    let end = text.find('>')?;
    let tag = &text[..end];
    if tag.is_empty() || !tag.bytes().all(|b| b.is_ascii_lowercase() || b == b'-') {
        return None;
    }
    let body = &text[end + 1..];
    let stop = body.find('<')?;
    let rest = body[stop..]
        .strip_prefix("</")?
        .strip_prefix(tag)?
        .strip_prefix('>')?;
    Some((tag, &body[..stop], rest))
}

/// A notification field kept in the compact marker input: one bounded token.
fn notification_token(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|token| {
        !token.is_empty()
            && token.len() <= 200
            && !token.contains('=')
            && !token.chars().any(char::is_whitespace)
    })
}

/// Claude Code writes a subagent transcript as `agent-<agentId>.jsonl`, and an agent's task id is
/// its agent id: `a` + 16 hex (optionally `a<label>-` + 16 hex) or the older `a` + 7 hex. Every
/// other task kind uses a one-letter prefix plus 8 base-36 chars, which never takes these shapes.
fn agent_task_session(task_id: &str) -> Option<String> {
    let hex = |s: &str| s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
    let rest = task_id.strip_prefix('a')?;
    let plain = matches!(rest.len(), 7 | 16) && hex(rest);
    let labelled = rest.rsplit_once('-').is_some_and(|(label, tail)| {
        (1..=63).contains(&label.len())
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            && tail.len() == 16
            && hex(tail)
    });
    (plain || labelled).then(|| format!("agent-{task_id}"))
}

/// The side session a stored agent id names: Claude Code writes its transcript as
/// `agent-<id>.jsonl`. None for an id that could not be a plain file stem.
fn agent_session(agent_id: &str) -> Option<String> {
    let id = notification_token(Some(agent_id))?;
    id.bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        .then(|| format!("agent-{id}"))
}

/// The `meta` token an agent call's or a message's own input carries: the name that makes the
/// spawned agent addressable, or the recipient SendMessage was given.
fn call_meta(name: &str, input: Option<&serde_json::Value>) -> String {
    let key = match name {
        "Task" | "Agent" => "name",
        "SendMessage" => "to",
        _ => return String::new(),
    };
    notification_token(
        input
            .and_then(|input| input.get(key))
            .and_then(|value| value.as_str()),
    )
    .map(|token| format!("{key}={token}"))
    .unwrap_or_default()
}

/// The `toolUseResult` fields that tie an agent call or a message to an agent's run. Raw, so one
/// unexpected shape leaves the other fields readable.
#[derive(Deserialize)]
struct AgentOutcome<'a> {
    #[serde(borrow)]
    status: Option<&'a RawValue>,
    #[serde(rename = "agentId", borrow)]
    agent_id: Option<&'a RawValue>,
    #[serde(rename = "resumedAgentId", borrow)]
    resumed_agent_id: Option<&'a RawValue>,
    #[serde(rename = "inlineHandback", borrow)]
    inline_handback: Option<&'a RawValue>,
    #[serde(borrow)]
    message: Option<&'a RawValue>,
}

fn push_meta(event: &mut Event, token: &str) {
    if !event.meta.is_empty() {
        event.meta.push(' ');
    }
    event.meta.push_str(token);
}

/// Record, uncapped, the agent run a Task/Agent or SendMessage result names: `child_session` is
/// that agent's transcript, and `meta` gains the call's status or the message's inline hand-back.
fn apply_agent_outcome(event: &mut Event, raw: &RawValue) {
    let Ok(outcome) = serde_json::from_str::<AgentOutcome>(raw.get()) else {
        return;
    };
    let text = |value: Option<&RawValue>| {
        value.and_then(|value| serde_json::from_str::<String>(value.get()).ok())
    };
    let agent_id = if event.kind == "subagent_start" {
        if let Some(status) = notification_token(text(outcome.status).as_deref()) {
            push_meta(event, &format!("status={status}"));
        }
        text(outcome.agent_id)
    } else {
        // 2.1.289 marks an inline result with `inlineHandback` and 2.1.199 in `message`; 2.1.240's
        // `Resumed agent <id or name>. Result:` is left to summary's reading of the display text
        let inline = outcome.inline_handback.is_some()
            || text(outcome.message)
                .is_some_and(|message| message.contains("ran to completion. Result:"));
        if inline {
            push_meta(event, "handback=inline");
        }
        text(outcome.resumed_agent_id)
    };
    if let Some(session) = agent_id.as_deref().and_then(agent_session) {
        event.child_session = session;
    }
}

fn notification_blocks(content: Option<&serde_json::Value>) -> Vec<TaskNotification<'_>> {
    content
        .map(content_texts)
        .unwrap_or_default()
        .into_iter()
        .filter_map(task_notification)
        .collect()
}

/// One marker per notification block of a row. A block both the user-row content and a queued
/// attachment carry is recorded once, under the user-row call id; a row with no block gets one.
fn push_task_notifications(
    events: &mut Vec<Event>,
    tally: &crate::intake::Tally,
    (session, ts, row_id): (&str, i64, &str),
    user: Option<&[TaskNotification]>,
    queued: Option<&[TaskNotification]>,
) {
    const USER: &str = "task-notification";
    const QUEUED: &str = "queued-task-notification";
    let empty = TaskNotification::default();
    let user_blocks = user.unwrap_or_default();
    let mut found: Vec<(&str, usize, &TaskNotification)> = user_blocks
        .iter()
        .enumerate()
        .map(|(index, block)| (USER, index, block))
        .collect();
    for (index, block) in queued.unwrap_or_default().iter().enumerate() {
        if !user_blocks.contains(block) {
            found.push((QUEUED, index, block));
        }
    }
    if found.is_empty() {
        found.push((if user.is_some() { USER } else { QUEUED }, 0, &empty));
    }
    for (form, index, notification) in found {
        let task_id = notification_token(notification.task_id);
        let fields = [
            ("status", notification_token(notification.status)),
            ("task_id", task_id),
            ("tool_use_id", notification_token(notification.tool_use_id)),
        ];
        let input = fields
            .iter()
            .filter_map(|(key, value)| value.map(|value| format!("{key}={value}")))
            .collect::<Vec<_>>()
            .join(" ");
        let mut call_id = format!("{row_id}:{form}");
        if index > 0 {
            call_id.push_str(&format!(":{index}"));
        }
        tally.event();
        events.push(Event {
            turn: None,
            agent: "claude",
            session: session.to_string(),
            ts,
            kind: "control",
            name: TASK_NOTIFICATION_MARKER.to_string(),
            input_chars: input.chars().count(),
            input,
            output: String::new(),
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id,
            child_session: task_id.and_then(agent_task_session).unwrap_or_default(),
            meta: String::new(),
            state: String::new(),
        });
    }
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
/// container segments (`Users/<name>/Desktop/...`, `projects/`, `repos/`), keep the
/// first real segment. `~/Desktop/projects/myproj/src` -> Some("myproj"); a bare home
/// dir or bare container -> None (no signal).
fn project_root(dir: &str) -> Option<String> {
    let d = dir.replace('\\', "/");
    let mut segs: Vec<&str> = d.split('/').filter(|s| !s.is_empty()).collect();
    // UNC/WSL paths (//wsl.localhost/Ubuntu/..., //server/share/...) lead with host
    // and share segments that would otherwise win the bucket
    if d.starts_with("//") {
        segs.drain(..2.min(segs.len()));
    }
    let home = crate::ingest::home().to_string_lossy().replace('\\', "/");
    let home_segs: Vec<&str> = home.split('/').filter(|s| !s.is_empty()).collect();
    // strip home per component: a byte-prefix compare on a lowercased copy can slice
    // mid-char, and would false-match a shorter home prefix inside a longer username
    if segs.len() >= home_segs.len()
        && segs
            .iter()
            .zip(&home_segs)
            .all(|(a, b)| a.eq_ignore_ascii_case(b))
    {
        segs.drain(..home_segs.len());
    }
    let user = crate::ingest::home_leaf();
    // the segment right after a Users/home container is a username on ANY machine
    // (a migrated corpus's cwds carry another box's user) - home_leaf() only knows
    // this one's, so any post-Users/home segment is skipped unconditionally.
    let mut after_user_container = false;
    // macOS TMPDIR: /private/var/folders/<2 chars>/<random>/T/<name>
    let mut after_folders = 0u8;
    segs.into_iter()
        .find(|s| {
            let sl = s.to_ascii_lowercase();
            if sl.ends_with(':') || std::mem::take(&mut after_user_container) {
                return false;
            }
            if after_folders > 0 {
                after_folders -= 1;
                return false;
            }
            // Containers win over the home-leaf skip: a HOME ending in `home` or `users`
            // must not hide the container and promote the username to the project.
            if matches!(sl.as_str(), "users" | "home") {
                after_user_container = true;
                return false;
            }
            if sl == user {
                return false;
            }
            if sl == "folders" {
                after_folders = 3;
                return false;
            }
            !matches!(
                sl.as_str(),
                "desktop"
                    | "documents"
                    | "downloads"
                    | "onedrive"
                    | "tmp"
                    | "temp"
                    | "private"
                    | "var"
                    | "appdata"
                    | "local"
                    | "locallow"
                    | "roaming"
                    | "src"
                    | "projects"
                    | "project"
                    | "code"
                    | "repos"
                    | "repositories"
                    | "git"
                    | "github"
                    | "work"
                    | "dev"
                    | "workspace"
                    | "workspaces"
            )
        })
        .map(|s| s.to_string())
}

/// The project a session actually worked in: a histogram over EVERY line's cwd (Claude
/// updates it as the session cd's around) plus the parent dir of every file its tools
/// touched, reduced to project roots. The most-worked-in root wins, so sessions launched
/// from a home dir that evolve into a real project land on where the work went, not
/// where the terminal happened to open.
fn primary_project(
    cwd_counts: &HashMap<String, usize>,
    paths: &[String],
    first_cwd: &str,
    fallback: &str,
    votes: &mut HashMap<String, usize>,
) -> String {
    let mut counts = std::mem::take(votes);
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
    *votes = counts.clone();
    // ties break on name so re-ingest is deterministic
    if let Some((k, _)) = counts
        .into_iter()
        .max_by(|a, b| a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0)))
    {
        return k;
    }
    if first_cwd.is_empty() {
        fallback.to_string()
    } else {
        project_name(first_cwd)
    }
}

fn directory_project(path: &Path) -> String {
    let parts: Vec<&str> = path.iter().filter_map(|part| part.to_str()).collect();
    if let Some(index) = parts.iter().rposition(|part| *part == "projects") {
        if let Some(slug) = parts.get(index + 1) {
            return (*slug).to_string();
        }
    }
    if let Some(index) = parts.iter().rposition(|part| *part == "subagents") {
        if let Some(slug) = index.checked_sub(2).and_then(|value| parts.get(value)) {
            return (*slug).to_string();
        }
    }
    path.parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .unwrap_or("claude")
        .to_string()
}

/// Text of a tool_result's `content`: a plain string, or an array of text blocks.
fn tool_result_full_text(content: &serde_json::Value) -> String {
    match content {
        serde_json::Value::String(s) => s.clone(),
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
            out
        }
        _ => String::new(),
    }
}

fn tool_result_text(content: &serde_json::Value) -> (String, usize, usize) {
    match content {
        serde_json::Value::String(s) => cap_event_output(s),
        serde_json::Value::Array(_) => cap_event_output(&tool_result_full_text(content)),
        _ => (String::new(), 0, 0),
    }
}

/// Side-session context, derived from the path so the cached per-file parse
/// signature stays untouched: `<project>/<parent-session>/subagents/**/agent-*.jsonl`
/// -> Some(parent session id). Top-level session files -> None.
fn side_context(path: &Path) -> Option<String> {
    let mut comps: Vec<&str> = path.iter().filter_map(|c| c.to_str()).collect();
    comps.pop(); // the file itself
    let sub = comps.iter().rposition(|c| *c == "subagents")?;
    comps.get(sub.checked_sub(1)?).map(|s| s.to_string())
}

fn parse_file_stamped(
    path: &Path,
    mtime_ns: i64,
    size: u64,
) -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    parse_file_with_tally(
        path,
        crate::intake::file_stamped("claude", path, mtime_ns, size),
    )
}

fn parse_file_with_tally(
    path: &Path,
    tally: std::sync::Arc<crate::intake::Tally>,
) -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            // a present-but-unreadable file is a real problem; silence would make a
            // permissions break look identical to "no history"
            crate::intake::diagnostic(format_args!(
                "  ! claude: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            ));
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest::read_failure_outcome(&e),
            );
        }
    };
    parse_data(path, tally, &data, None).0
}

#[derive(Default, serde::Serialize, serde::Deserialize)]
struct ClaudeState {
    out: Vec<crate::model::RawMessage>,
    recap_turns: Vec<u32>,
    events: Vec<Event>,
    order: crate::ingest::ReplyOrder,
    pending: HashMap<String, usize>,
    turn: u32,
    first_cwd: String,
    project_votes: HashMap<String, usize>,
    side_parent: Option<String>,
    ordinal: usize,
}
pub(crate) fn resume_parse(
    path: &Path,
    data: &str,
    previous: Option<&str>,
) -> anyhow::Result<(
    Vec<Message>,
    Vec<Event>,
    crate::intake_batch::ReadOutcome,
    String,
)> {
    let state = previous.map(serde_json::from_str).transpose()?;
    let ((m, e, o), next) = parse_data(path, crate::intake::file("claude", path), data, state);
    Ok((m, e, o, serde_json::to_string(&next)?))
}
fn parse_data(
    path: &Path,
    tally: std::sync::Arc<crate::intake::Tally>,
    data: &str,
    previous: Option<ClaudeState>,
) -> (
    (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome),
    ClaudeState,
) {
    let resumed = previous.is_some();
    let state = previous.unwrap_or_default();
    let mut side_parent = if resumed {
        state.side_parent.clone()
    } else {
        side_context(path)
    };
    let is_side = side_parent.is_some();
    if is_throwaway_path(path) {
        // Keep byte-for-byte parity with audit's binary `line.strip()` census:
        // ASCII-whitespace-only lines are blank; non-ASCII bytes remain records.
        let records = data
            .lines()
            .filter(|line| line.bytes().any(|byte| !byte.is_ascii_whitespace()))
            .count() as u64;
        tally.seen_n(records);
        tally.skip_n(crate::intake::Skip::Throwaway, records);
        return (
            (
                Vec::new(),
                Vec::new(),
                crate::intake_batch::ReadOutcome::Complete,
            ),
            state,
        );
    }
    let file_session = path
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let mut out = state.out;
    let mut recap_turns = state.recap_turns;
    let mut events = state.events;
    let mut order = state.order;
    // tool_use id -> index into `events`, so the later tool_result line can pair up.
    let mut pending = state.pending;
    let mut turn = state.turn;
    let ordinal = state.ordinal;
    let mut project_votes = state.project_votes;
    let mut first_cwd = state.first_cwd;
    let mut cwd_counts: HashMap<String, usize> = HashMap::new();
    let mut tool_paths: Vec<String> = Vec::new();
    // Cheap key probes choose which borrowed schema to parse; attribution still comes from JSON.
    let f_msg = memmem::Finder::new(b"\"message\"");
    let f_cwd = memmem::Finder::new(b"\"cwd\"");
    let f_toolres = memmem::Finder::new(b"\"tool_result\"");
    let f_queued = memmem::Finder::new(b"\"queued_command\"");
    // A side file's parent is decided ONCE, before any row is emitted: the first inner
    // sessionId (the parent's indexed identity, preferred since a dir stem can drift),
    // else the dir stem. A parent that flips mid-file splits one child across two ids.
    if is_side && !resumed {
        let f_sess = memmem::Finder::new(b"\"sessionId\":\"");
        if let Some(pid) = data
            .lines()
            .find_map(|line| raw_str_value(&f_sess, line.as_bytes()).filter(|s| !s.is_empty()))
        {
            side_parent = Some(pid.to_string());
        }
    }
    let row_session = |l: &Line| match l.session_id.as_deref() {
        Some(id) if !is_side => id.to_string(),
        _ => file_session.clone(),
    };
    for (offset, line) in data.lines().enumerate() {
        let record_ordinal = ordinal + offset;
        if line.is_empty() {
            continue;
        }
        tally.seen();
        let bytes = line.as_bytes();
        if !has_json_key(&f_msg, bytes) {
            // a notification delivered mid-turn is an attachment row, which has no message
            if f_queued.find(bytes).is_some() {
                if let Ok(l) = serde_json::from_str::<Line>(line) {
                    let inline_side = l.is_sidechain == Some(true) && side_parent.is_none();
                    if let Some(prompt) = queued_notification_prompt(l.attachment) {
                        if !inline_side {
                            push_task_notifications(
                                &mut events,
                                &tally,
                                (
                                    &row_session(&l),
                                    parse_timestamp::rfc3339(l.timestamp.as_deref()),
                                    &format!("claude:{file_session}:{record_ordinal}"),
                                ),
                                None,
                                Some(&notification_blocks(Some(&prompt))),
                            );
                        }
                    }
                }
            }
            if has_json_key(&f_cwd, bytes) {
                if let Ok(fields) = serde_json::from_str::<ProjectLine>(line) {
                    if let Some(cwd) = optional_cwd(fields.cwd) {
                        if first_cwd.is_empty() {
                            first_cwd = cwd.clone();
                        }
                        *cwd_counts.entry(cwd).or_insert(0) += 1;
                    }
                }
            }
            // progress / summary / file-history lines: structural, no message payload
            tally.skip(crate::intake::Skip::NonMessage);
            continue;
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(line, 80)));
                continue;
            }
        };
        if let Some(cwd) = optional_cwd(l.cwd) {
            if first_cwd.is_empty() {
                first_cwd = cwd.clone();
            }
            *cwd_counts.entry(cwd).or_insert(0) += 1;
        }
        let session = row_session(&l);
        if l.is_sidechain == Some(true) && side_parent.is_none() {
            // inline sidechain rows duplicate the subagents/ child transcripts (their own
            // side sessions); dropping the whole line covers events and reply text too
            tally.skip(crate::intake::Skip::Sidechain);
            continue;
        }
        // Assistant turn -> attach its text + model to the user message it answers, and note
        // the files it touched (to infer the real working dir at the end).
        if l.ty.as_deref() == Some("assistant") {
            if let Some(m) = &l.message {
                if m.role.as_deref() == Some("assistant") {
                    // Assistant content is always needed (tool paths, events, reply text):
                    // parse the RawValue into a DOM once.
                    let content_val: Option<serde_json::Value> =
                        m.content.and_then(|r| serde_json::from_str(r.get()).ok());
                    // events recorded before the row's last text block were answered by it
                    let mut text_at: Option<usize> = None;
                    if let Some(content) = &content_val {
                        collect_tool_paths(content, &mut tool_paths);
                        // tool_use blocks -> events (inline sidechain lines never reach
                        // here; the subagents/ child transcript is their one source)
                        if let serde_json::Value::Array(blocks) = content {
                            for (block_ordinal, b) in blocks.iter().enumerate() {
                                if b.get("type").and_then(|t| t.as_str()) == Some("text") {
                                    if b.get("text")
                                        .and_then(|t| t.as_str())
                                        .is_some_and(|t| !t.trim().is_empty())
                                    {
                                        text_at = Some(events.len());
                                    }
                                    continue;
                                }
                                if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                                    continue;
                                }
                                let name = b
                                    .get("name")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("?")
                                    .to_string();
                                let native_call_id = b
                                    .get("id")
                                    .and_then(|v| v.as_str())
                                    .filter(|id| !id.trim().is_empty());
                                let call_id =
                                    native_call_id.map(str::to_string).unwrap_or_else(|| {
                                        format!(
                                        "claude:{file_session}:{record_ordinal}:{block_ordinal}"
                                    )
                                    });
                                let (input, input_chars) = b
                                    .get("input")
                                    .map(summarize_tool_input_with_chars)
                                    .unwrap_or_default();
                                let kind = if name == "Task" || name == "Agent" {
                                    "subagent_start"
                                } else {
                                    "tool"
                                };
                                if native_call_id.is_some() {
                                    pending.insert(call_id.clone(), events.len());
                                }
                                let meta = call_meta(&name, b.get("input"));
                                let state = crate::ingest::task_state(&name, b.get("input"));
                                tally.event();
                                events.push(Event {
                                    turn: out.last().map(|m| m.turn),
                                    agent: "claude",
                                    session: session.clone(),
                                    ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
                                    kind,
                                    name,
                                    input,
                                    output: String::new(),
                                    input_chars,
                                    output_chars: 0,
                                    output_bytes: 0,
                                    ok: None,
                                    call_id,
                                    child_session: String::new(),
                                    meta,
                                    state,
                                });
                            }
                        }
                    }
                    let text = content_val.as_ref().and_then(extract_text);
                    if is_api_error_row(&l) {
                        // the text also stays in the reply, so stored replies and search keep it
                        let error = text.as_deref().map_or("", str::trim);
                        let (input, input_chars) = cap_str_with_chars(error, EVENT_CAP);
                        tally.event();
                        events.push(Event {
                            turn: out.last().map(|m| m.turn),
                            agent: "claude",
                            session: session.clone(),
                            ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
                            kind: "control",
                            name: API_ERROR_MARKER.to_string(),
                            input,
                            output: String::new(),
                            input_chars,
                            output_chars: 0,
                            output_bytes: 0,
                            ok: None,
                            call_id: format!("claude:{file_session}:{record_ordinal}:api-error"),
                            child_session: String::new(),
                            meta: String::new(),
                            state: String::new(),
                        });
                    }
                    if let Some(last) = out.last_mut() {
                        if last.model.is_empty() {
                            if let Some(md) = l.message.as_ref().and_then(|m| m.model.as_deref()) {
                                // "<synthetic>" is a marker, not a model: a hook-injected
                                // reply must not stamp the row it follows (it once
                                // reclassified compact summaries and poisoned the pool).
                                if !md.is_empty() && md != "<synthetic>" {
                                    last.model = md.to_string();
                                }
                            }
                        }
                        if let Some(txt) = &text {
                            let chars = crate::ingest::append_reply(last, txt);
                            if chars > 0 {
                                let at = text_at.unwrap_or(events.len());
                                order.joined(&mut last.reply_shape, &events, at);
                                order.reply(at);
                            }
                        }
                    }
                }
            }
            tally.agent_row();
            continue;
        }
        if l.ty.as_deref() != Some("user") {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }
        // User content parses lazily: the tool_result pairing only runs when the raw line
        // contains the marker, and lines that fail the human filters never build a DOM.
        let raw_content = l.message.as_ref().and_then(|m| m.content);
        let mut content_val: Option<serde_json::Value> = None;
        // tool_result blocks arrive in user-typed lines (userType external, isMeta null),
        // so pair them BEFORE the human-turn filters would drop the line.
        if f_toolres.find(bytes).is_some() {
            content_val = raw_content.and_then(|r| serde_json::from_str(r.get()).ok());
            if let Some(serde_json::Value::Array(blocks)) = &content_val {
                let is_result = |b: &serde_json::Value| {
                    b.get("type").and_then(|t| t.as_str()) == Some("tool_result")
                };
                // a row's toolUseResult belongs to its call only when it carries that one result
                let lone = blocks.iter().filter(|b| is_result(b)).count() == 1;
                for b in blocks {
                    if !is_result(b) {
                        continue;
                    }
                    let id = b.get("tool_use_id").and_then(|v| v.as_str()).unwrap_or("");
                    if let Some(&i) = pending.get(id) {
                        let ev = &mut events[i];
                        if let Some(c) = b.get("content") {
                            (ev.output, ev.output_chars, ev.output_bytes) = tool_result_text(c);
                            if ev.name == "TaskList" {
                                ev.state = crate::ingest::task_listing(&tool_result_full_text(c));
                            }
                        }
                        ev.ok = Some(b.get("is_error").and_then(|v| v.as_bool()) != Some(true));
                        if ev.kind == "subagent_start" || ev.name == "SendMessage" {
                            if let Some(raw) = l.tool_use_result.filter(|_| lone) {
                                apply_agent_outcome(ev, raw);
                            }
                            // the result row is written once the call returned, after any run
                            // it waited on had ended
                            let returned = parse_timestamp::rfc3339(l.timestamp.as_deref());
                            if returned > 0 {
                                push_meta(ev, &format!("returned={returned}"));
                            }
                        }
                        pending.remove(id);
                    }
                }
            }
        }
        let user_form = is_task_notification_row(&l);
        let queued_prompt = queued_notification_prompt(l.attachment);
        if user_form || queued_prompt.is_some() {
            if user_form && content_val.is_none() {
                content_val = raw_content.and_then(|r| serde_json::from_str(r.get()).ok());
            }
            let user = user_form.then(|| notification_blocks(content_val.as_ref()));
            let queued = queued_prompt
                .as_ref()
                .map(|prompt| notification_blocks(Some(prompt)));
            push_task_notifications(
                &mut events,
                &tally,
                (
                    &session,
                    parse_timestamp::rfc3339(l.timestamp.as_deref()),
                    &format!("claude:{file_session}:{record_ordinal}"),
                ),
                user.as_deref(),
                queued.as_deref(),
            );
        }
        if l.is_meta == Some(true) {
            tally.skip(crate::intake::Skip::Meta);
            continue;
        }
        if let Some(ut) = l.user_type.as_deref() {
            if ut != "external" {
                tally.skip(crate::intake::Skip::NonHuman);
                continue;
            }
        }
        let msg = match &l.message {
            Some(m) => m,
            None => {
                tally.skip(crate::intake::Skip::NonMessage);
                continue;
            }
        };
        if msg.role.as_deref() != Some("user") {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }
        if content_val.is_none() {
            content_val = raw_content.and_then(|r| serde_json::from_str(r.get()).ok());
        }
        let text = match content_val.as_ref().and_then(extract_text) {
            Some(t) if !t.trim().is_empty() => t,
            _ => {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
        };
        if is_wrapper(&text) {
            tally.skip(crate::intake::Skip::Wrapper);
            continue;
        }
        tally.row();
        if l.is_compact_summary == Some(true) {
            recap_turns.push(turn);
        } else {
            order.prompt(&mut events);
        }
        out.push(crate::model::RawMessage {
            agent: "claude",
            project: String::new(), // filled once per session below
            session,
            ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
            turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: is_side,
            parent: side_parent.clone().unwrap_or_default(),
            reply_shape: Default::default(),
        });
        turn += 1;
    }
    order.finish(&mut events);
    // One project for the whole session: where the work actually happened.
    let project = primary_project(
        &cwd_counts,
        &tool_paths,
        &first_cwd,
        &directory_project(path),
        &mut project_votes,
    );
    for m in &mut out {
        m.project = project.clone();
    }
    let read_outcome = if out.is_empty()
        && events.is_empty()
        && data
            .lines()
            .any(|line| !line.trim().is_empty() && serde_json::from_str::<&RawValue>(line).is_err())
    {
        crate::intake_batch::ReadOutcome::Invalid
    } else {
        crate::intake_batch::ReadOutcome::Complete
    };

    let mut keep: Vec<_> = pending.values().copied().collect();
    keep.sort_unstable();
    keep.dedup();
    let next = ClaudeState {
        out: out.last().cloned().into_iter().collect(),
        recap_turns: out
            .last()
            .filter(|m| recap_turns.contains(&m.turn))
            .map(|m| vec![m.turn])
            .unwrap_or_default(),
        events: keep.iter().map(|i| events[*i].clone()).collect(),
        order: order.checkpoint(&events, &keep),
        pending: pending
            .into_iter()
            .map(|(id, i)| (id, keep.binary_search(&i).unwrap()))
            .collect(),
        turn,
        first_cwd,
        project_votes,
        side_parent,
        ordinal: ordinal + data.lines().count(),
    };
    let messages = out
        .into_iter()
        .map(|raw| {
            let turn = raw.turn;
            let mut m = raw.freeze();
            if recap_turns.contains(&turn) {
                m.who = "recap".into();
                m.model_source = "recap".into();
            }
            m
        })
        .collect();
    ((messages, events, read_outcome), next)
}

/// Walk all real project dirs and collect the user's Claude messages + tool events.
pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".claude").join("projects");
    let mut discovered_mtimes = None;
    let files = cache.preflight_source_paths(&root).unwrap_or_else(|| {
        let mut files = Vec::new();
        let dirs = match fs::symlink_metadata(&root) {
            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => match fs::read_dir(&root) {
                Ok(dirs) => Some(dirs),
                Err(error) => {
                    crate::ingest::warn_source_skip("claude", &root, &error);
                    None
                }
            },
            Ok(_) => {
                crate::ingest::warn_source_skip(
                    "claude",
                    &root,
                    "source root is not a plain directory",
                );
                None
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                crate::ingest::warn_source_skip("claude", &root, &error);
                None
            }
        };
        let mut links = Vec::new();
        if let Some(dirs) = dirs {
            for entry in dirs {
                let entry = match entry {
                    Ok(entry) => entry,
                    Err(error) => {
                        crate::ingest::warn_source_skip("claude", &root, &error);
                        continue;
                    }
                };
                let dir = entry.path();
                match plain_entry_metadata(&entry) {
                    Ok(Some(meta)) if meta.is_dir() => {
                        gather_jsonl(&dir, &mut files, &mut links, 0)
                    }
                    Ok(None) => links.push(dir),
                    Ok(Some(_)) => {}
                    Err(error) => crate::ingest::warn_source_skip("claude", &dir, &error),
                }
            }
            files.sort_unstable();
        }
        let (files, mtimes): (Vec<_>, Vec<_>) = files.into_iter().unzip();
        warn_foreign_links(&root, &files, links);
        discovered_mtimes = Some(mtimes);
        files
    });

    let pass = crate::intake_batch::collect_cached_stamped_hinted_for(
        cache,
        "claude",
        &root,
        &files,
        discovered_mtimes.as_deref(),
        parse_file_stamped,
    );
    (pass.messages, pass.events)
}

/// Recursively gather every transcript jsonl under a project dir (sessions,
/// subagents/ side transcripts, nested project-slug dirs). Deliberately skipped
/// worker transcripts remain in this set so freshness and intake accounting see them.
/// Links are collected for `warn_foreign_links`, which needs the finished file set.
fn gather_jsonl(
    dir: &Path,
    files: &mut Vec<(std::path::PathBuf, i64)>,
    links: &mut Vec<std::path::PathBuf>,
    depth: u8,
) {
    if depth > 5 {
        return;
    }
    match fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("claude", dir, "source is not a plain directory");
            return;
        }
        Err(error) => {
            crate::ingest::warn_source_skip("claude", dir, &error);
            return;
        }
    }
    let rd = match fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(error) => {
            crate::ingest::warn_source_skip("claude", dir, &error);
            return;
        }
    };
    let entries: Vec<_> = rd
        .filter_map(|entry| match entry {
            Ok(entry) => Some(entry),
            Err(error) => {
                crate::ingest::warn_source_skip("claude", dir, &error);
                None
            }
        })
        .collect();
    enum EntryKind {
        Directory,
        Link,
        Jsonl(i64),
        Other,
        Error(String),
    }
    // Directory enumeration is sequential, but classifying the many transcript entries is
    // independent work and should not delay the newest-file streamed first hit.
    let classified: Vec<_> = entries
        .into_par_iter()
        .map(|entry| {
            let path = entry.path();
            let kind = match plain_entry_metadata(&entry) {
                Ok(Some(metadata)) if metadata.is_dir() => EntryKind::Directory,
                Ok(Some(metadata))
                    if metadata.is_file()
                        && path.extension().and_then(|e| e.to_str()) == Some("jsonl") =>
                {
                    let mtime = metadata
                        .modified()
                        .ok()
                        .and_then(crate::intake_batch::epoch_ns)
                        .unwrap_or(0);
                    EntryKind::Jsonl(mtime)
                }
                Ok(None) => EntryKind::Link,
                Ok(Some(_)) => EntryKind::Other,
                Err(error) => EntryKind::Error(error.to_string()),
            };
            (path, kind)
        })
        .collect();
    for (path, kind) in classified {
        match kind {
            EntryKind::Directory => gather_jsonl(&path, files, links, depth + 1),
            EntryKind::Link => links.push(path),
            EntryKind::Jsonl(mtime) => files.push((path, mtime)),
            EntryKind::Other => {}
            EntryKind::Error(error) => crate::ingest::warn_source_skip("claude", &path, error),
        }
    }
}

/// agrep's own resume links (aliases of gathered transcripts, or dangling inside the
/// store) are skipped silently, like the registry walk; any other link is warned about.
fn warn_foreign_links(root: &Path, files: &[std::path::PathBuf], links: Vec<std::path::PathBuf>) {
    let accepts = |path: &Path| path.extension().and_then(|e| e.to_str()) == Some("jsonl");
    for link in links {
        match crate::ingest::registry::classify_store_link(&link, root, &accepts) {
            StoreLink::Alias(walked) if files.binary_search(&walked).is_ok() => {}
            StoreLink::Dangling => {}
            _ => {
                crate::ingest::warn_source_skip("claude", &link, "symlink sources are not followed")
            }
        }
    }
}

fn is_throwaway_name(name: &std::ffi::OsStr) -> bool {
    use std::sync::OnceLock;

    static TEMP_WORKER_PREFIXES: OnceLock<Vec<String>> = OnceLock::new();
    let prefixes = TEMP_WORKER_PREFIXES.get_or_init(|| {
        let temp = std::env::temp_dir();
        let mut roots = vec![temp.clone()];
        if let Ok(canonical) = temp.canonicalize() {
            if canonical != temp {
                roots.push(canonical);
            }
        }
        roots
            .iter()
            .map(|root| claude_worker_temp_prefix(root))
            .collect()
    });
    is_throwaway_name_for_prefixes(name, prefixes)
}

fn claude_worker_temp_prefix(root: &Path) -> String {
    // Claude's project-folder encoding replaces every non-ASCII-alphanumeric
    // character with '-'. Re-encode the actual OS temp root instead of trying
    // to decode a lossy slug or accepting any nested directory named "tmp".
    let mut slug: String = root
        .to_string_lossy()
        .chars()
        .map(|value| {
            if value.is_ascii_alphanumeric() {
                value.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    slug.push_str("-claude-worker-");
    slug
}

fn is_throwaway_name_for_prefixes(name: &std::ffi::OsStr, prefixes: &[String]) -> bool {
    // tempfile adds exactly eight characters. An underscore in that suffix is
    // encoded as '-' (for example `_zgq26av` becomes `-zgq26av`).
    let name = name.to_string_lossy().to_ascii_lowercase();
    prefixes.iter().any(|prefix| {
        name.strip_prefix(prefix).is_some_and(|token| {
            token.len() == 8
                && token
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        })
    })
}

fn is_throwaway_path(path: &Path) -> bool {
    path.parent().is_some_and(|parent| {
        parent
            .components()
            .any(|component| is_throwaway_name(component.as_os_str()))
    })
}

fn is_discovered_transcript(path: &Path) -> bool {
    if path.extension().and_then(|x| x.to_str()) != Some("jsonl") {
        return false;
    }
    let root = crate::ingest::home().join(".claude").join("projects");
    let relative =
        crate::ingest::registry::relative_path(path, &root).unwrap_or_else(|| path.to_path_buf());
    let depth = relative.components().count();
    (2..=7).contains(&depth)
}

/// Registry entry (see ingest::registry). Byte-identical wrapper over `collect`.
pub struct Claude;
impl crate::ingest::registry::Adapter for Claude {
    fn name(&self) -> &'static str {
        "claude"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".claude").join("projects")]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        path.extension().and_then(|x| x.to_str()) == Some("jsonl")
    }
    fn freshness_content(&self, path: &std::path::Path) -> bool {
        is_discovered_transcript(path)
    }
}
