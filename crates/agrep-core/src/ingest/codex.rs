//! Codex CLI/Desktop adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
//! One JSONL file per session. The first line is `session_meta` (gives cwd -> project,
//! and the session id). Real human turns are `response_item` lines with
//! payload.type=="message" and payload.role=="user", text in `input_text`/`text` blocks.
//!
//! Much of what arrives under `role:user` is codex's own composition: AGENTS.md and the
//! environment block, plugin and skill catalogs, delegation payloads, system notifications.
//! Their shape is indistinguishable from typed prose, so eligibility comes from the rollout's
//! `event_msg` submission log (`Submissions`), in legacy and Desktop schemas.
//! `is_wrapper`/`is_codex_injected` still veto client echoes the log does carry,
//! such as slash-command markup. Prefer under-including over mislabeling.
//!
//! NOTE: only `~/.codex/sessions/` is walked. `~/.codex/.tmp/**` (plugin test fixtures),
//! `~/.codex/history.jsonl`, and `~/.codex/archived_sessions/` are intentionally not read.

use std::borrow::Cow;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

use memchr::memmem;
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, project_name,
    summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::model::{Event, Message};

/// Control-event name for a native compaction moment; it is never a tool call.
pub const COMPACTED_MARKER: &str = "compacted";

// Borrowed deserialization (soundness argument in claude.rs); RawValue defers the DOMs
// until a call pairs up, and parse_file's prefilter keeps reasoning blobs out of serde.
#[derive(Deserialize)]
struct Line<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    #[serde(borrow)]
    payload: Option<Payload<'a>>,
}

#[derive(Deserialize)]
struct Payload<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    role: Option<Cow<'a, str>>,
    #[serde(borrow)]
    content: Option<Vec<Block<'a>>>,
    // session_meta fields
    #[serde(borrow)]
    id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    cwd: Option<Cow<'a, str>>,
    #[serde(borrow)]
    thread_source: Option<Cow<'a, str>>,
    #[serde(borrow)]
    parent_thread_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    agent_path: Option<Cow<'a, str>>,
    #[serde(borrow)]
    source: Option<&'a RawValue>,
    // Inter-agent handoffs are response_item/agent_message records. Their task body
    // may be encrypted, but the routing metadata and visible header remain available.
    #[serde(borrow)]
    author: Option<Cow<'a, str>>,
    #[serde(borrow)]
    recipient: Option<Cow<'a, str>>,
    #[serde(borrow)]
    turn_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    internal_chat_message_metadata_passthrough: Option<ChatMessageMeta<'a>>,
    // turn_context carries the active model (e.g. "gpt-5.3-codex-spark").
    #[serde(borrow)]
    model: Option<Cow<'a, str>>,
    /// Tool-call fields (`name` through `action`): function_call {name,
    /// arguments(JSON-string), call_id}; function_call_output {call_id, output};
    /// custom_tool_call {name, input, status}; web_search_call {action:{query,...}};
    /// tool_search_call carries `arguments` as an OBJECT, so it stays raw and each
    /// consumer decodes its own shape.
    #[serde(borrow)]
    name: Option<Cow<'a, str>>,
    #[serde(borrow)]
    arguments: Option<&'a RawValue>,
    #[serde(borrow)]
    call_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    output: Option<&'a RawValue>,
    #[serde(borrow)]
    input: Option<Cow<'a, str>>,
    #[serde(borrow)]
    status: Option<Cow<'a, str>>,
    #[serde(borrow)]
    action: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct SessionSource<'a> {
    #[serde(borrow)]
    subagent: Option<SubagentSource<'a>>,
}

#[derive(Deserialize)]
struct SubagentSource<'a> {
    #[serde(borrow)]
    thread_spawn: Option<ThreadSpawn<'a>>,
    /// Codex-internal control children use `other:"guardian"` for approval
    /// assessments rather than real delegated work.
    #[serde(borrow)]
    other: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
struct ThreadSpawn<'a> {
    #[serde(borrow)]
    agent_path: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
struct ChatMessageMeta<'a> {
    #[serde(borrow)]
    turn_id: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
struct Block<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    text: Option<Cow<'a, str>>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FastClass {
    /// The canonical prefix proves this is a record the parser consumes.
    Parse,
    /// Canonical tool output: extract call_id/output directly and avoid walking the
    /// often multi-megabyte record once just to capture output as RawValue.
    CallOutput,
    /// Canonical bookkeeping which the adapter deliberately does not materialize.
    Meta,
    /// A native Codex context-compaction boundary. Its replacement history stays
    /// opaque; only the timestamped structural boundary becomes a recap row.
    Compact,
    /// A canonical structural record with no searchable message/event contribution.
    NonMessage,
    /// Non-canonical spacing/order: use the conservative key-needle fallback.
    Fallback,
}

/// Classify the compact JSON emitted by Codex from its first ~100 bytes.
///
/// Codex writes top-level records as
/// `{"timestamp":"...","type":"...","payload":{"type":"...",...}}`. Anchoring the
/// match at byte zero and at the timestamp's closing quote is important: quoted text inside a
/// payload cannot masquerade as one of these structural fields. If spacing/order ever changes,
/// return `Fallback` and let the conservative whole-line key-needle scan decide.
///
/// Multi-megabyte `mcp_tool_call_end` and compacted-history records contain nested
/// `call_id`/`role` keys; a whole-line key scan would admit them to serde even though the
/// adapter immediately discards their top-level record kind.
fn fast_class(bytes: &[u8]) -> FastClass {
    let Some(after_timestamp) = bytes.strip_prefix(b"{\"timestamp\":\"") else {
        return FastClass::Fallback;
    };
    let Some(timestamp_end) = memchr::memchr(b'"', after_timestamp) else {
        return FastClass::Fallback;
    };
    let Some(after_type) = after_timestamp[timestamp_end..].strip_prefix(b"\",\"type\":\"") else {
        return FastClass::Fallback;
    };
    let Some(type_end) = memchr::memchr(b'"', after_type) else {
        return FastClass::Fallback;
    };
    let top = &after_type[..type_end];

    match top {
        b"session_meta" | b"turn_context" => FastClass::Parse,
        b"event_msg" | b"response_item" => {
            let Some(after_payload_type) =
                after_type[type_end..].strip_prefix(b"\",\"payload\":{\"type\":\"")
            else {
                return FastClass::Fallback;
            };
            let Some(payload_type_end) = memchr::memchr(b'"', after_payload_type) else {
                return FastClass::Fallback;
            };
            let payload = &after_payload_type[..payload_type_end];
            if top == b"event_msg" {
                // task_started supplies the child-owned turn boundary. Every other event_msg
                // is bookkeeping (including huge duplicated MCP tool outputs).
                if payload == b"task_started" {
                    FastClass::Parse
                } else {
                    FastClass::Meta
                }
            } else {
                match payload {
                    b"message" | b"agent_message" | b"function_call" | b"custom_tool_call"
                    | b"tool_search_call" | b"web_search_call" => FastClass::Parse,
                    b"function_call_output" | b"custom_tool_call_output" => FastClass::CallOutput,
                    _ => FastClass::NonMessage,
                }
            }
        }
        b"compacted" => FastClass::Compact,
        // any other outer record kind deserializes to the non-message arm anyway; nested
        // message-shaped data must not make the prefilter parse it
        _ => FastClass::NonMessage,
    }
}

fn canonical_timestamp(bytes: &[u8]) -> Option<&str> {
    let after = bytes.strip_prefix(b"{\"timestamp\":\"")?;
    let end = memchr::memchr(b'"', after)?;
    let raw = &after[..end];
    if raw.iter().any(|byte| *byte < 0x20 || *byte == b'\\') {
        return None;
    }
    std::str::from_utf8(raw).ok()
}

fn compact_boundary_message(
    path: &Path,
    project: Option<&str>,
    session: Option<&str>,
    timestamp: Option<&str>,
    turn: u32,
    subagent: Option<&SubagentContext>,
) -> crate::model::RawMessage {
    crate::model::RawMessage {
        agent: "codex",
        project: project.unwrap_or("unknown").to_string(),
        session: session
            .map(str::to_string)
            .unwrap_or_else(|| session_from_filename(path)),
        ts: parse_timestamp::rfc3339(timestamp),
        turn,
        text: String::new(),
        model: String::new(),
        reply: String::new(),
        reply_chars: 0,
        side: subagent.is_some(),
        parent: subagent
            .map(|ctx| ctx.parent_session.clone())
            .unwrap_or_default(),
        reply_shape: Default::default(),
    }
}

fn compaction_marker(
    path: &Path,
    session: Option<&str>,
    timestamp: Option<&str>,
    source_stem: &str,
    record_ordinal: usize,
) -> Event {
    Event {
        turn: None,
        agent: "codex",
        session: session
            .map(str::to_string)
            .unwrap_or_else(|| session_from_filename(path)),
        ts: parse_timestamp::rfc3339(timestamp),
        kind: "control",
        name: COMPACTED_MARKER.to_string(),
        input: String::new(),
        output: String::new(),
        input_chars: 0,
        output_chars: 0,
        output_bytes: 0,
        ok: None,
        call_id: format!("codex:{source_stem}:{record_ordinal}:compacted"),
        child_session: String::new(),
        meta: String::new(),
        state: String::new(),
    }
}

/// Parse one JSON value at ``start`` without materializing it. ``RawValue`` validates
/// strings, escapes, primitives, and balanced containers; the stream offset gives the
/// exact end even when a compact object field follows immediately.
#[inline]
fn is_json_whitespace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r')
}

fn raw_json_value_at(bytes: &[u8], mut start: usize) -> Option<(&str, usize)> {
    while bytes.get(start).copied().is_some_and(is_json_whitespace) {
        start += 1;
    }
    let mut stream = serde_json::Deserializer::from_slice(&bytes[start..]).into_iter::<&RawValue>();
    let raw = stream.next()?.ok()?;
    Some((raw.get(), start + stream.byte_offset()))
}

/// Extract a canonical call output while proving the *entire* record is valid.
///
/// This deliberately recognizes only Codex's compact field layouts. Unknown/reordered
/// fields fall back to the ordinary borrowed serde path. Parsing every field boundary
/// avoids accepting a partially-written final JSONL record merely because its output
/// value happened to close before the truncation point.
fn raw_call_output(line: &str) -> Option<(&str, &str)> {
    let bytes = line.as_bytes();
    let after_timestamp = bytes.strip_prefix(b"{\"timestamp\":\"")?;
    let timestamp_end = memchr::memchr(b'"', after_timestamp)?;
    if after_timestamp[..timestamp_end]
        .iter()
        .any(|byte| *byte < 0x20 || *byte == b'\\')
    {
        return None;
    }
    let after_type = after_timestamp[timestamp_end..]
        .strip_prefix(b"\",\"type\":\"response_item\",\"payload\":{\"type\":\"")?;
    let payload_type_end = memchr::memchr(b'"', after_type)?;
    if !matches!(
        &after_type[..payload_type_end],
        b"function_call_output" | b"custom_tool_call_output"
    ) {
        return None;
    }

    let mut i = bytes.len() - after_type.len() + payload_type_end + 1;
    let mut call_id = None;
    let mut output = None;
    let mut seen_id = false;
    let mut seen_metadata = false;
    while bytes.get(i) == Some(&b',') {
        i += 1;
        if bytes.get(i) != Some(&b'"') {
            return None;
        }
        i += 1;
        let key_end = i + memchr::memchr(b'"', &bytes[i..])?;
        let key = &bytes[i..key_end];
        if key.contains(&b'\\') || !key.is_ascii() {
            return None;
        }
        i = key_end + 1;
        if bytes.get(i) != Some(&b':') {
            return None;
        }
        i += 1;
        let (raw, end) = raw_json_value_at(bytes, i)?;
        match key {
            b"id" if !seen_id => {
                // Match Payload::id exactly: null or a (possibly escaped) string.
                let _: Option<Cow<'_, str>> = serde_json::from_str(raw).ok()?;
                seen_id = true;
            }
            b"call_id" if call_id.is_none() => {
                let id = serde_json::from_str::<&str>(raw).ok()?;
                if !id.is_ascii() {
                    return None;
                }
                call_id = Some(id);
            }
            b"output" if output.is_none() => output = Some(raw),
            b"internal_chat_message_metadata_passthrough" if !seen_metadata => {
                // Do not let the raw shortcut hide a type error the ordinary Line
                // deserializer would count. Unknown object fields remain accepted.
                let _: Option<ChatMessageMeta<'_>> = serde_json::from_str(raw).ok()?;
                seen_metadata = true;
            }
            _ => return None,
        }
        i = end;
    }
    if bytes.get(i) != Some(&b'}') || bytes.get(i + 1) != Some(&b'}') {
        return None;
    }
    i += 2;
    if bytes[i..]
        .iter()
        .copied()
        .any(|byte| !is_json_whitespace(byte))
    {
        return None;
    }
    Some((call_id?, output?))
}

#[derive(Deserialize)]
struct SubmissionLine<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    payload: Option<SubmissionPayload<'a>>,
}

/// Legacy `message` values must be strings; object-valued event messages are not submissions.
#[derive(Deserialize)]
struct SubmissionPayload<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Cow<'a, str>>,
    #[serde(borrow)]
    thread_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    turn_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    item: Option<SubmissionItem<'a>>,
}

#[derive(Deserialize)]
struct SubmissionItem<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    content: Option<Vec<Block<'a>>>,
}

/// Only native submission events attest composed `role:user` input as human prose.
#[derive(Default, serde::Serialize, serde::Deserialize)]
struct Submissions {
    /// Sorted and deduped, so `attests` binary-searches instead of scanning.
    texts: Vec<String>,
    /// Desktop text is scoped to its thread and turn so routed copies cannot attest it.
    desktop: Vec<(String, String, String)>,
}

impl Submissions {
    fn collect(data: &(impl crate::ingest::jsonl::Text + ?Sized)) -> Self {
        let legacy_finder = memmem::Finder::new(b"\"user_message\"");
        let desktop_finder = memmem::Finder::new(b"\"UserMessage\"");
        let mut texts: Vec<String> = Vec::new();
        let mut desktop = Vec::new();
        for line in data.lines() {
            let line = line.as_ref();
            if legacy_finder.find(line.as_bytes()).is_none()
                && desktop_finder.find(line.as_bytes()).is_none()
            {
                continue;
            }
            let parsed: SubmissionLine = match serde_json::from_str(line) {
                Ok(parsed) => parsed,
                Err(_) => continue,
            };
            if parsed.ty.as_deref() != Some("event_msg") {
                continue;
            }
            let Some(payload) = parsed.payload else {
                continue;
            };
            match payload.ty.as_deref() {
                Some("user_message") => {
                    if let Some(text) = payload
                        .message
                        .as_deref()
                        .map(str::trim)
                        .filter(|text| !text.is_empty())
                    {
                        texts.push(text.to_string());
                    }
                }
                Some("item_completed") => {
                    let (Some(thread_id), Some(turn_id), Some(item)) =
                        (payload.thread_id, payload.turn_id, payload.item)
                    else {
                        continue;
                    };
                    if thread_id.is_empty()
                        || turn_id.is_empty()
                        || item.ty.as_deref() != Some("UserMessage")
                    {
                        continue;
                    }
                    let mut parts = item
                        .content
                        .unwrap_or_default()
                        .into_iter()
                        .filter(|block| block.ty.as_deref() == Some("text"))
                        .filter_map(|block| block.text);
                    let Some(first) = parts.next() else {
                        continue;
                    };
                    let mut text = first.into_owned();
                    for part in parts {
                        if !text.is_empty() {
                            text.push('\n');
                        }
                        text.push_str(&part);
                    }
                    text.truncate(text.trim_end().len());
                    text.replace_range(..text.len() - text.trim_start().len(), "");
                    if !text.is_empty() {
                        desktop.push((thread_id.into_owned(), turn_id.into_owned(), text));
                    }
                }
                _ => {}
            }
        }
        texts.sort_unstable();
        texts.dedup();
        desktop.sort_unstable();
        desktop.dedup();
        Self { texts, desktop }
    }

    fn is_empty(&self) -> bool {
        self.texts.is_empty() && self.desktop.is_empty()
    }

    /// Legacy submissions allow appended paste markers; Desktop requires matching thread, turn and text.
    fn attests(&self, text: &str, session: Option<&str>, turn_id: Option<&str>) -> bool {
        let text = text.trim();
        let after = self.texts.partition_point(|s| s.as_str() <= text);
        if after > 0 && text.starts_with(self.texts[after - 1].as_str()) {
            return true;
        }
        let (Some(session), Some(turn_id)) = (session, turn_id) else {
            return false;
        };
        self.desktop
            .binary_search_by(|(thread, turn, submitted)| {
                (thread.as_str(), turn.as_str(), submitted.as_str()).cmp(&(session, turn_id, text))
            })
            .is_ok()
    }
}

/// Concatenate visible human/routed text blocks. Routed `agent_message` handoffs have
/// appeared as both `input_text` and `output_text`; dropping the latter prevents the child
/// boundary from ever opening and suppresses the entire forked transcript.
fn extract_text(blocks: &[Block<'_>]) -> Option<String> {
    let mut out = String::new();
    for b in blocks {
        match b.ty.as_deref() {
            Some("input_text") | Some("output_text") | Some("text") => {
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
fn extract_assistant(blocks: &[Block<'_>]) -> Option<String> {
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

#[derive(serde::Serialize, serde::Deserialize)]
struct SubagentContext {
    agent_path: Option<String>,
    start_turn_id: Option<String>,
    replayed_parent: bool,
    parent_session: String,
    started: bool,
}

fn is_guardian_source(raw: Option<&RawValue>) -> bool {
    raw.and_then(|value| serde_json::from_str::<SessionSource<'_>>(value.get()).ok())
        .and_then(|source| source.subagent)
        .and_then(|subagent| subagent.other)
        .is_some_and(|kind| kind == "guardian")
}

fn is_uuid_v7_after(candidate: &str, earlier: &str) -> bool {
    fn canonical_v7(s: &str) -> bool {
        let b = s.as_bytes();
        b.len() == 36
            && b[8] == b'-'
            && b[13] == b'-'
            && b[18] == b'-'
            && b[23] == b'-'
            && b[14] == b'7'
            && b.iter()
                .enumerate()
                .all(|(i, c)| matches!(i, 8 | 13 | 18 | 23) || c.is_ascii_hexdigit())
    }

    canonical_v7(candidate) && canonical_v7(earlier) && candidate > earlier
}

fn header_value<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    text.lines()
        .find_map(|line| line.strip_prefix(key).map(str::trim))
        .filter(|value| !value.is_empty())
}

fn delegated_input(text: &str) -> Option<String> {
    let text = text.trim_start();
    if !text.starts_with("<codex_delegation") && !text.starts_with("<realtime_delegation") {
        return None;
    }
    let (_, rest) = text.split_once("<input>")?;
    let (body, _) = rest.split_once("</input>")?;
    let body = body.trim();
    if body.is_empty() {
        return None;
    }
    // Delegation wrappers XML-escape the task body in older Codex journals.
    Some(
        body.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", "\"")
            .replace("&apos;", "'")
            .replace("&amp;", "&"),
    )
}

/// Turn an inbound Codex team message into a searchable child-turn anchor. Newer
/// Codex builds encrypt the task body, so retain plaintext payloads when present and
/// otherwise describe only the routing metadata that the rollout actually exposes.
fn subagent_anchor(payload: &Payload<'_>, expected_recipient: &str) -> Option<String> {
    if payload.ty.as_deref() != Some("agent_message")
        || payload.recipient.as_deref() != Some(expected_recipient)
    {
        return None;
    }
    let visible = extract_text(payload.content.as_deref()?)?;
    let message_type = header_value(&visible, "Message Type:")?;
    let label = match message_type {
        "NEW_TASK" => "task",
        "MESSAGE" | "FOLLOWUP_TASK" => "message",
        _ => return None,
    };

    if let Some((_, body)) = visible.split_once("Payload:") {
        let body = body.trim();
        if !body.is_empty() {
            return Some(format!("[subagent {label}] {body}"));
        }
    }

    let task = header_value(&visible, "Task name:").unwrap_or(expected_recipient);
    let sender = payload
        .author
        .as_deref()
        .or_else(|| header_value(&visible, "Sender:"));
    Some(match sender {
        Some(sender) => format!("[subagent {label}] {task} (from {sender})"),
        None => format!("[subagent {label}] {task}"),
    })
}

/// Codex-specific preambles and system-injected `role:user` notifications that are NOT
/// something the user typed. Checked in addition to the shared `is_wrapper`.
pub(crate) fn is_codex_injected(text: &str) -> bool {
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
        // Image-paste marker; the typed text is entangled with it, so skip rather than mislabel.
        || t.starts_with("<image")
}

/// Derive a session id from a `rollout-<ISO>-<uuid>.jsonl` filename (fallback when the
/// `session_meta` line is missing its id).
fn session_from_filename(path: &Path) -> String {
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or_default();
    // The id is the trailing UUID (5 dash-separated groups).
    let parts: Vec<&str> = stem.split('-').collect();
    if parts.len() >= 5 {
        parts[parts.len() - 5..].join("-")
    } else {
        stem.to_string()
    }
}

/// The codex shell wrapper prints `Process exited with code N` into otherwise
/// unstructured output text -- the only outcome record many codex tool calls have.
fn sniff_exit_code(s: &str) -> Option<bool> {
    const MARK: &str = "Process exited with code ";
    let i = s.find(MARK)?;
    let digits: String = s[i + MARK.len()..]
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    if digits.is_empty() {
        None
    } else {
        Some(digits == "0")
    }
}

fn ciphertext_token(value: &str) -> bool {
    const FERNET_PREFIX: &[u8] = b"gAAAAAB";
    const ENTROPY_MIN_LEN: usize = 512;
    const ENTROPY_MIN_BITS: f64 = 5.75;

    let token = value.trim();
    let padding = token.bytes().rev().take_while(|byte| *byte == b'=').count();
    if padding > 2 || token.len() < 96 {
        return false;
    }
    let body = &token.as_bytes()[..token.len() - padding];
    if !body
        .iter()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'+' | b'/'))
    {
        return false;
    }
    if body.starts_with(FERNET_PREFIX) {
        return true;
    }
    if body.len() < ENTROPY_MIN_LEN {
        return false;
    }

    let mut counts = [0u32; 256];
    for byte in body {
        counts[*byte as usize] += 1;
    }
    let len = body.len() as f64;
    let entropy = counts
        .iter()
        .filter(|count| **count != 0)
        .map(|count| {
            let probability = f64::from(*count) / len;
            -probability * probability.log2()
        })
        .sum::<f64>();
    entropy >= ENTROPY_MIN_BITS
}

fn ciphertext_send_message(name: &str, input: &serde_json::Value) -> bool {
    if name != "send_message" {
        return false;
    }
    match input {
        serde_json::Value::Object(fields) => fields
            .get("message")
            .and_then(serde_json::Value::as_str)
            .is_some_and(ciphertext_token),
        serde_json::Value::String(inner) => serde_json::from_str(inner)
            .ok()
            .is_some_and(|decoded| ciphertext_send_message(name, &decoded)),
        _ => false,
    }
}

/// A function_call_output's `output`: usually a plain string, which is sometimes itself
/// JSON `{"output": "...", "metadata": {"exit_code": 0}}`. Returns (text, ok-if-known).
fn parse_call_output(v: &serde_json::Value) -> (String, usize, usize, Option<bool>) {
    let from_obj = |o: &serde_json::Map<String, serde_json::Value>| {
        let text = o
            .get("output")
            .and_then(|x| x.as_str())
            .map(|s| s.to_string())
            .unwrap_or_else(|| serde_json::Value::Object(o.clone()).to_string());
        let ok = o
            .get("metadata")
            .and_then(|m| m.get("exit_code"))
            .and_then(|c| c.as_i64())
            .map(|c| c == 0)
            .or_else(|| sniff_exit_code(&text));
        (text, ok)
    };
    match v {
        serde_json::Value::String(s) => {
            let t = s.trim_start();
            if t.starts_with('{') {
                if let Ok(serde_json::Value::Object(o)) = serde_json::from_str(t) {
                    let (text, ok) = from_obj(&o);
                    let (text, chars, bytes) = cap_event_output(&text);
                    return (text, chars, bytes, ok);
                }
            }
            let (text, chars, bytes) = cap_event_output(s);
            (text, chars, bytes, sniff_exit_code(s))
        }
        serde_json::Value::Object(o) => {
            let (text, ok) = from_obj(o);
            let (text, chars, bytes) = cap_event_output(&text);
            (text, chars, bytes, ok)
        }
        serde_json::Value::Null => (String::new(), 0, 0, None),
        other => {
            let (text, chars, bytes) = cap_event_output(&other.to_string());
            (text, chars, bytes, None)
        }
    }
}

/// Raw-byte prefilter invariant (sound for the quoted key needles - see the claude.rs
/// raw_str_value doc): every line kind this parser consumes is admitted by one of its
/// memmem needles - messages carry `"role":`, tool calls/outputs carry `"call_id":`,
/// plus the two meta line types (and web_search_call, whose call_id is not guaranteed).
/// The reasoning items that dominate rollout bytes match none and are skipped without
/// touching serde; an unquoted-needle false positive just costs one wasted parse.
fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    let tally = crate::intake::tally();
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            crate::intake::diagnostic(format_args!(
                "  ! codex: cannot read {}: {}",
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
struct CodexState {
    out: Vec<crate::model::RawMessage>,
    recap_turns: Vec<u32>,
    events: Vec<Event>,
    order: crate::ingest::ReplyOrder,
    pending: HashMap<String, usize>,
    turn: u32,
    project: Option<String>,
    session: Option<String>,
    first_meta_seen: bool,
    subagent: Option<SubagentContext>,
    internal_guardian: bool,
    current_model: String,
    submissions: Submissions,
    ordinal: usize,
    reset: bool,
}
pub(crate) fn resume_parse(
    path: &Path,
    data: &(impl crate::ingest::jsonl::Text + ?Sized),
    previous: Option<&str>,
) -> anyhow::Result<(
    Vec<Message>,
    Vec<Event>,
    crate::intake_batch::ReadOutcome,
    String,
)> {
    let state = previous.map(serde_json::from_str).transpose()?;
    let ((m, e, o), next) = parse_data(path, crate::intake::tally(), data, state);
    Ok((m, e, o, serde_json::to_string(&next)?))
}
fn parse_data(
    path: &Path,
    tally: std::sync::Arc<crate::intake::Tally>,
    data: &(impl crate::ingest::jsonl::Text + ?Sized),
    previous: Option<CodexState>,
) -> (
    (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome),
    CodexState,
) {
    let state = previous.unwrap_or_default();
    let mut submissions = state.submissions;
    let fresh = Submissions::collect(data);
    submissions.texts.extend(fresh.texts);
    submissions.texts.sort_unstable();
    submissions.texts.dedup();
    submissions.desktop.extend(fresh.desktop);
    submissions.desktop.sort_unstable();
    submissions.desktop.dedup();
    let mut out = state.out;
    let mut carried_rows = out.len();
    let mut recap_turns = state.recap_turns;
    let mut events = state.events;
    let mut carried_events = events.len();
    let mut order = state.order;
    // call_id -> index into `events`, so the *_output line can pair up.
    let mut pending = state.pending;
    let mut turn = state.turn;
    let ordinal = state.ordinal;
    let mut reset = false;
    let mut project = state.project;
    let mut session = state.session;
    let mut first_meta_seen = state.first_meta_seen;
    let mut subagent = state.subagent;
    let mut internal_guardian = state.internal_guardian;
    // The active model, updated by each turn_context line and stamped onto turns.
    let mut current_model = state.current_model;

    // Prefilter needles - admission invariant documented on this fn.
    let f_role = memmem::Finder::new(b"\"role\":");
    let f_callid = memmem::Finder::new(b"\"call_id\":");
    let f_meta = memmem::Finder::new(b"session_meta");
    let f_turnctx = memmem::Finder::new(b"turn_context");
    let f_websearch = memmem::Finder::new(b"web_search_call");
    let f_agentmsg = memmem::Finder::new(b"agent_message");
    let f_taskstarted = memmem::Finder::new(b"task_started");
    let f_compacted = memmem::Finder::new(b"compacted");
    let mut fast_meta = 0u64;
    let mut fast_non_message = 0u64;
    // The record ordinal is the stable fallback identity for store records that carry no
    // call_id. Include the rollout stem because one logical session may have more than one
    // source file after resume/import; an ordinal alone would conflate unrelated calls.
    let source_stem = path
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or("rollout");
    for (offset, line) in data.lines().enumerate() {
        let line = line.as_ref();
        let record_ordinal = ordinal + offset;
        if line.is_empty() {
            continue;
        }
        // Guardian rollouts are automatic approval-review control traffic. Once
        // session_meta proves that subtype, account every remaining record without
        // parsing or indexing its large echoed parent-history wrapper.
        if internal_guardian {
            tally.seen();
            tally.skip(crate::intake::Skip::Sidechain);
            continue;
        }
        let bytes = line.as_bytes();
        match fast_class(bytes) {
            FastClass::Parse => tally.seen(),
            FastClass::CallOutput => {
                tally.seen();
                if subagent.as_ref().is_some_and(|ctx| !ctx.started) {
                    tally.skip(crate::intake::Skip::Replay);
                    continue;
                }
                if let Some((id, raw_output)) = raw_call_output(line) {
                    if let Some(&i) = pending.get(id) {
                        let (text, chars, bytes, ok) =
                            serde_json::from_str::<serde_json::Value>(raw_output)
                                .map(|value| parse_call_output(&value))
                                .unwrap_or_default();
                        events[i].output = text;
                        events[i].output_chars = chars;
                        events[i].output_bytes = bytes;
                        if ok.is_some() {
                            events[i].ok = ok;
                        }
                        pending.remove(id);
                    }
                    tally.agent_row();
                    continue;
                }
                // A future reordered/pretty output record falls through to serde.
            }
            FastClass::Meta => {
                fast_meta += 1;
                continue;
            }
            FastClass::Compact => {
                tally.seen();
                if subagent.as_ref().is_some_and(|ctx| !ctx.started) {
                    tally.skip(crate::intake::Skip::Replay);
                    continue;
                }
                tally.row();
                recap_turns.push(turn);
                out.push(compact_boundary_message(
                    path,
                    project.as_deref(),
                    session.as_deref(),
                    canonical_timestamp(bytes),
                    turn,
                    subagent.as_ref(),
                ));
                tally.event();
                events.push(compaction_marker(
                    path,
                    session.as_deref(),
                    canonical_timestamp(bytes),
                    source_stem,
                    record_ordinal,
                ));
                turn += 1;
                continue;
            }
            FastClass::NonMessage => {
                fast_non_message += 1;
                continue;
            }
            FastClass::Fallback => {
                if f_role.find(bytes).is_none()
                    && f_callid.find(bytes).is_none()
                    && f_meta.find(bytes).is_none()
                    && f_turnctx.find(bytes).is_none()
                    && f_websearch.find(bytes).is_none()
                    && f_agentmsg.find(bytes).is_none()
                    && f_taskstarted.find(bytes).is_none()
                    && f_compacted.find(bytes).is_none()
                {
                    // Non-canonical reasoning/delta records: structural, never message
                    // payloads. The fallback retains compatibility with pretty/reordered JSON.
                    fast_non_message += 1;
                    continue;
                }
                tally.seen();
            }
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(line, 80)));
                continue;
            }
        };

        if l.ty.as_deref() == Some("compacted") {
            if subagent.as_ref().is_some_and(|ctx| !ctx.started) {
                tally.skip(crate::intake::Skip::Replay);
                continue;
            }
            tally.row();
            recap_turns.push(turn);
            out.push(compact_boundary_message(
                path,
                project.as_deref(),
                session.as_deref(),
                l.timestamp.as_deref(),
                turn,
                subagent.as_ref(),
            ));
            tally.event();
            events.push(compaction_marker(
                path,
                session.as_deref(),
                l.timestamp.as_deref(),
                source_stem,
                record_ordinal,
            ));
            turn += 1;
            continue;
        }

        // The first metadata record names the rollout file. Forked Codex rollouts then
        // replay their parent's history, including parent session_meta records; allowing
        // those to overwrite this identity collapses every child back into its parent.
        if l.ty.as_deref() == Some("session_meta") {
            if !first_meta_seen {
                first_meta_seen = true;
                if let Some(p) = &l.payload {
                    if let Some(cwd) = p.cwd.as_deref() {
                        project = Some(project_name(cwd));
                    }
                    if let Some(id) = p.id.as_deref() {
                        if !id.is_empty() {
                            session = Some(id.to_string());
                        }
                    }
                    if p.thread_source.as_deref() == Some("subagent") {
                        if is_guardian_source(p.source) {
                            internal_guardian = true;
                            tally.skip(crate::intake::Skip::Meta);
                            continue;
                        }
                        // Legacy journals store source as a scalar ("cli"/"vscode");
                        // newer subagents use an object with thread_spawn metadata.
                        let nested_agent_path = p
                            .source
                            .and_then(|raw| {
                                serde_json::from_str::<SessionSource<'_>>(raw.get()).ok()
                            })
                            .and_then(|source| source.subagent)
                            .and_then(|source| source.thread_spawn)
                            .and_then(|spawn| spawn.agent_path.map(Cow::into_owned));
                        let agent_path = p
                            .agent_path
                            .as_deref()
                            .filter(|s| !s.is_empty())
                            .map(str::to_string)
                            .or(nested_agent_path.filter(|s| !s.is_empty()));
                        // Newer journals name the parent outright; rollouts without the
                        // field still learn it from the replayed parent's session_meta.
                        let parent_session = p
                            .parent_thread_id
                            .as_deref()
                            .filter(|s| !s.is_empty())
                            .map(str::to_string)
                            .unwrap_or_default();
                        subagent = Some(SubagentContext {
                            agent_path,
                            start_turn_id: None,
                            replayed_parent: false,
                            parent_session,
                            started: false,
                        });
                    }
                }
            } else if let (Some(ctx), Some(p), Some(session_id)) =
                (subagent.as_mut(), l.payload.as_ref(), session.as_deref())
            {
                if let Some(parent_id) =
                    p.id.as_deref()
                        .filter(|id| !id.is_empty() && *id != session_id)
                {
                    ctx.replayed_parent = true;
                    ctx.parent_session = parent_id.to_string();
                }
            }
            tally.skip(crate::intake::Skip::Meta);
            continue;
        }
        // Path-less subagents get their task as a plain role:user message; UUIDv7 turn ids
        // survive fork re-timestamping, so the first v7 id newer than the session id marks
        // the child-owned task boundary (older v7 replay turns and v4 ids are excluded).
        if l.ty.as_deref() == Some("event_msg") {
            if let (Some(ctx), Some(p), Some(session_id)) =
                (subagent.as_mut(), l.payload.as_ref(), session.as_deref())
            {
                if !ctx.started && p.ty.as_deref() == Some("task_started") {
                    if let Some(turn_id) = p
                        .turn_id
                        .as_deref()
                        .filter(|turn_id| is_uuid_v7_after(turn_id, session_id))
                    {
                        ctx.start_turn_id = Some(turn_id.to_string());
                    }
                }
            }
            tally.skip(crate::intake::Skip::Meta);
            continue;
        }
        // turn_context announces the model in force for the turns that follow.
        if l.ty.as_deref() == Some("turn_context") {
            if let Some(md) = l.payload.as_ref().and_then(|p| p.model.as_deref()) {
                if !md.is_empty() {
                    current_model = md.to_string();
                }
            }
            tally.skip(crate::intake::Skip::Meta);
            continue;
        }

        if l.ty.as_deref() != Some("response_item") {
            tally.skip(crate::intake::Skip::NonMessage);
            continue;
        }
        let payload = match &l.payload {
            Some(p) => p,
            None => {
                tally.skip(crate::intake::Skip::NonMessage);
                continue;
            }
        };

        // A child rollout contains a verbatim parent replay before this inbound team
        // message. Start the child transcript here, then accept later MESSAGE handoffs as
        // additional turns. Outbound child messages have a different recipient.
        if payload.ty.as_deref() == Some("agent_message") {
            if let Some(ctx) = subagent.as_mut() {
                if let Some(agent_path) = ctx.agent_path.as_deref() {
                    if let Some(text) = subagent_anchor(payload, agent_path) {
                        if !ctx.started {
                            ctx.started = true;
                            pending.clear();
                            tally.discard_rows_as_replay(
                                out.len().saturating_sub(carried_rows) as u64
                            );
                            tally
                                .discard_events(events.len().saturating_sub(carried_events) as u64);
                            events.clear();
                            order.reset(&events);
                            out.clear();
                            turn = 0;
                            carried_rows = 0;
                            carried_events = 0;
                            reset = true;
                        }
                        tally.row();
                        order.prompt(&mut events);
                        out.push(crate::model::RawMessage {
                            agent: "codex",
                            project: project.clone().unwrap_or_else(|| "unknown".to_string()),
                            session: session
                                .clone()
                                .unwrap_or_else(|| session_from_filename(path)),
                            ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
                            turn,
                            text,
                            model: current_model.clone(),
                            reply: String::new(),
                            reply_chars: 0,
                            side: true,
                            parent: ctx.parent_session.clone(),
                            reply_shape: Default::default(),
                        });
                        turn += 1;
                        continue;
                    }
                }
            }
            // inter-agent chatter routed elsewhere (outbound child replies etc.)
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }

        let message_turn_id = payload
            .internal_chat_message_metadata_passthrough
            .as_ref()
            .and_then(|m| m.turn_id.as_deref());

        // Legacy/native subagents with no routing path carry a real plaintext task.
        // Require its passthrough turn id to match the child-owned task_started marker;
        // without that proof, keep suppressing the inherited parent transcript.
        if payload.ty.as_deref() == Some("message") && payload.role.as_deref() == Some("user") {
            if let Some(ctx) = subagent.as_mut().filter(|ctx| ctx.agent_path.is_none()) {
                let text = payload
                    .content
                    .as_deref()
                    .and_then(extract_text)
                    .and_then(|text| {
                        // A delegated task reaches a child without a keyboard, so an
                        // empty log says nothing here. A log that exists and does not
                        // attest does: the text is the replayed parent prologue.
                        delegated_input(&text).or_else(|| {
                            (!is_wrapper(&text)
                                && !is_codex_injected(&text)
                                && (submissions.is_empty()
                                    || submissions.attests(
                                        &text,
                                        session.as_deref(),
                                        message_turn_id,
                                    )))
                            .then_some(text)
                        })
                    });
                let turn_matches = ctx.started
                    // both ids present and equal - None == None is absence matching
                    // absence, not proof, and must never open the boundary
                    || (message_turn_id.is_some()
                        && message_turn_id == ctx.start_turn_id.as_deref())
                    || (message_turn_id.is_none()
                        && !ctx.replayed_parent
                        && ctx.start_turn_id.is_some());
                if let (Some(text), true) = (text, turn_matches) {
                    let label = if ctx.started { "message" } else { "task" };
                    if !ctx.started {
                        ctx.started = true;
                        pending.clear();
                        tally.discard_rows_as_replay(out.len().saturating_sub(carried_rows) as u64);
                        tally.discard_events(events.len().saturating_sub(carried_events) as u64);
                        events.clear();
                        order.reset(&events);
                        out.clear();
                        turn = 0;
                        carried_rows = 0;
                        carried_events = 0;
                        reset = true;
                    }
                    tally.row();
                    order.prompt(&mut events);
                    out.push(crate::model::RawMessage {
                        agent: "codex",
                        project: project.clone().unwrap_or_else(|| "unknown".to_string()),
                        session: session
                            .clone()
                            .unwrap_or_else(|| session_from_filename(path)),
                        ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
                        turn,
                        text: format!("[subagent {label}] {text}"),
                        model: current_model.clone(),
                        reply: String::new(),
                        reply_chars: 0,
                        side: true,
                        parent: ctx.parent_session.clone(),
                        reply_shape: Default::default(),
                    });
                    turn += 1;
                    continue;
                }
                // suppressed inherited-parent transcript before the proven boundary
                tally.skip(crate::intake::Skip::Replay);
                continue;
            }
        }
        if subagent.as_ref().is_some_and(|ctx| !ctx.started) {
            tally.skip(crate::intake::Skip::Replay);
            continue;
        }

        // Tool stream -> events. (Reasoning stays skipped.)
        let sess = || {
            session
                .clone()
                .unwrap_or_else(|| session_from_filename(path))
        };
        let event_id = || {
            payload
                .call_id
                .as_deref()
                .or(payload.id.as_deref())
                .filter(|id| !id.trim().is_empty())
                .map(str::to_string)
                .unwrap_or_else(|| format!("codex:{source_stem}:{record_ordinal}"))
        };
        match payload.ty.as_deref() {
            Some("function_call") | Some("custom_tool_call") | Some("tool_search_call") => {
                // tool_search_call records carry no `name`; label them for the mix stats
                let fallback = if payload.ty.as_deref() == Some("tool_search_call") {
                    "tool_search"
                } else {
                    "?"
                };
                let name = payload.name.as_deref().unwrap_or(fallback).to_string();
                // function_call carries `arguments` as a JSON string; custom_tool_call
                // carries `input` as a raw string (e.g. an apply_patch body).
                let mut control = false;
                let mut state = String::new();
                let (input, input_chars) = if let Some(raw) = payload.arguments {
                    let args_val: serde_json::Value =
                        serde_json::from_str(raw.get()).unwrap_or_default();
                    control = ciphertext_send_message(&name, &args_val);
                    match args_val {
                        serde_json::Value::String(inner) => {
                            serde_json::from_str::<serde_json::Value>(&inner)
                                .map(|v| {
                                    state = crate::ingest::task_state(&name, Some(&v));
                                    summarize_tool_input_with_chars(&v)
                                })
                                .unwrap_or_else(|_| cap_str_with_chars(&inner, EVENT_CAP))
                        }
                        v => {
                            state = crate::ingest::task_state(&name, Some(&v));
                            summarize_tool_input_with_chars(&v)
                        }
                    }
                } else {
                    payload.input.as_deref().map_or_else(
                        || (String::new(), 0),
                        |raw| {
                            control = serde_json::from_str(raw)
                                .ok()
                                .is_some_and(|value| ciphertext_send_message(&name, &value));
                            cap_str_with_chars(raw, EVENT_CAP)
                        },
                    )
                };
                let ok = match payload.status.as_deref() {
                    Some("completed") => Some(true),
                    Some("failed") | Some("error") => Some(false),
                    _ => None,
                };
                let call_id = event_id();
                // Native ids pair with output records. A synthesized source-record id still
                // gives the call durable identity, but cannot invent a correlation the store
                // did not record.
                if payload.call_id.as_deref().is_some_and(|id| !id.is_empty()) {
                    pending.insert(call_id.clone(), events.len());
                }
                tally.agent_row();
                tally.event();
                events.push(Event {
                    turn: out.last().map(|m| m.turn),
                    agent: "codex",
                    session: sess(),
                    ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
                    kind: if control { "control" } else { "tool" },
                    name,
                    input,
                    output: String::new(),
                    input_chars,
                    output_chars: 0,
                    output_bytes: 0,
                    ok,
                    call_id,
                    child_session: String::new(),
                    meta: String::new(),
                    state,
                });
                continue;
            }
            Some("function_call_output") | Some("custom_tool_call_output") => {
                if let Some(id) = payload.call_id.as_deref() {
                    if let Some(&i) = pending.get(id) {
                        let (text, chars, bytes, ok) = payload
                            .output
                            .and_then(|r| serde_json::from_str::<serde_json::Value>(r.get()).ok())
                            .map(|v| parse_call_output(&v))
                            .unwrap_or_default();
                        events[i].output = text;
                        events[i].output_chars = chars;
                        events[i].output_bytes = bytes;
                        if ok.is_some() {
                            events[i].ok = ok;
                        }
                        // No fabricated Some(true): codex output carries no exit codes and
                        // "arrived" != "worked", so ok stays None (reported as not-recorded)
                        pending.remove(id);
                    }
                }
                tally.agent_row();
                continue;
            }
            Some("web_search_call") => {
                tally.agent_row();
                tally.event();
                let action_val = payload
                    .action
                    .and_then(|r| serde_json::from_str::<serde_json::Value>(r.get()).ok());
                let query = action_val
                    .as_ref()
                    .and_then(|a| a.get("query"))
                    .and_then(|q| q.as_str())
                    .map(str::to_string)
                    .or_else(|| {
                        action_val
                            .as_ref()
                            .and_then(|a| a.get("queries"))
                            .and_then(|q| q.as_array())
                            .map(|queries| {
                                queries
                                    .iter()
                                    .filter_map(|query| query.as_str())
                                    .collect::<Vec<_>>()
                                    .join(" · ")
                            })
                    })
                    .unwrap_or_default();
                let (input, input_chars) = cap_str_with_chars(&query, EVENT_CAP);
                events.push(Event {
                    turn: out.last().map(|m| m.turn),
                    agent: "codex",
                    session: sess(),
                    ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
                    kind: "tool",
                    name: "web_search".to_string(),
                    input,
                    output: String::new(),
                    input_chars,
                    output_chars: 0,
                    output_bytes: 0,
                    ok: None,
                    call_id: event_id(),
                    child_session: String::new(),
                    meta: String::new(),
                    state: String::new(),
                });
                continue;
            }
            Some("message") => {}
            _ => {
                tally.skip(crate::intake::Skip::NonMessage);
                continue;
            }
        }
        let blocks = match payload.content.as_deref() {
            Some(b) => b,
            None => {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
        };

        // Assistant prose -> attach to the user message it answers.
        if payload.role.as_deref() == Some("assistant") {
            if let Some(txt) = extract_assistant(blocks) {
                if let Some(last) = out.last_mut() {
                    let chars = crate::ingest::append_reply(last, &txt);
                    if chars > 0 {
                        order.joined(&mut last.reply_shape, &events, events.len());
                        order.reply(events.len());
                    }
                    if last.model.is_empty() && !current_model.is_empty() {
                        last.model = current_model.clone();
                    }
                }
            }
            tally.agent_row();
            continue;
        }
        // Only the human past here; skip developer / system.
        if payload.role.as_deref() != Some("user") {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }
        let text = match extract_text(blocks) {
            Some(t) => t,
            None => {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
        };
        if is_wrapper(&text) || is_codex_injected(&text) {
            tally.skip(crate::intake::Skip::Wrapper);
            continue;
        }
        // An absent submission is not evidence of a human turn: a subagent thread
        // logs none because nobody is at the keyboard, and its real inbound work
        // arrives on the handoff paths above.
        if !submissions.attests(&text, session.as_deref(), message_turn_id) {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }

        tally.row();
        order.prompt(&mut events);
        out.push(crate::model::RawMessage {
            agent: "codex",
            project: project.clone().unwrap_or_else(|| "unknown".to_string()),
            session: session
                .clone()
                .unwrap_or_else(|| session_from_filename(path)),
            ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
            turn,
            text,
            model: current_model.clone(),
            reply: String::new(),
            reply_chars: 0,
            side: subagent.is_some(),
            parent: subagent
                .as_ref()
                .map(|ctx| ctx.parent_session.clone())
                .unwrap_or_default(),
            reply_shape: Default::default(),
        });
        turn += 1;
    }
    order.finish(&mut events);

    // Canonical prefilter skips dominate the line count. Fold them into the per-file tally
    // once, keeping the same seen identity while avoiding millions of atomic increments.
    let fast_seen = fast_meta + fast_non_message;
    tally.seen_n(fast_seen);
    tally.skip_n(crate::intake::Skip::Meta, fast_meta);
    tally.skip_n(crate::intake::Skip::NonMessage, fast_non_message);

    let last = out.last().map(|m| m.text.as_str()).unwrap_or("");
    let consumed: std::collections::HashSet<_> = out.iter().map(|m| m.text.as_str()).collect();
    submissions
        .texts
        .retain(|t| !consumed.contains(t.as_str()) || last.starts_with(t.as_str()));
    submissions
        .desktop
        .retain(|(_, _, t)| !consumed.contains(t.as_str()) || last == t);
    let mut keep: Vec<_> = pending.values().copied().collect();
    keep.sort_unstable();
    keep.dedup();
    let next = CodexState {
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
        project,
        session,
        first_meta_seen,
        subagent,
        internal_guardian,
        current_model,
        submissions,
        ordinal: ordinal + data.lines().count(),
        reset,
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
    (
        (messages, events, crate::intake_batch::ReadOutcome::Complete),
        next,
    )
}

/// Recursively collect rollout files under `~/.codex/sessions/` (YYYY/MM/DD nesting).
fn is_rollout_file(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("rollout-") && name.ends_with(".jsonl"))
}

fn gather(dir: &Path, files: &mut Vec<std::path::PathBuf>, absent_ok: bool) {
    match fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("codex", dir, "source is not a plain directory");
            return;
        }
        Err(error) if absent_ok && error.kind() == std::io::ErrorKind::NotFound => return,
        Err(error) => {
            crate::ingest::warn_source_skip("codex", dir, &error);
            return;
        }
    }
    let rd = match fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(error) => {
            crate::ingest::warn_source_skip("codex", dir, &error);
            return;
        }
    };
    for entry in rd {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("codex", dir, &error);
                continue;
            }
        };
        let p = entry.path();
        let metadata = match plain_entry_metadata(&entry) {
            Ok(Some(metadata)) => metadata,
            Ok(None) => {
                crate::ingest::warn_source_skip("codex", &p, "symlink sources are not followed");
                continue;
            }
            Err(error) => {
                crate::ingest::warn_source_skip("codex", &p, &error);
                continue;
            }
        };
        if metadata.is_dir() {
            gather(&p, files, false);
        } else if metadata.is_file() && is_rollout_file(&p) {
            files.push(p);
        }
    }
}

/// Walk all session rollouts and collect the user's Codex messages + tool events.
pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".codex").join("sessions");
    let files = cache.preflight_source_paths(&root).unwrap_or_else(|| {
        let mut files = Vec::new();
        gather(&root, &mut files, true);
        files.sort_unstable();
        files
    });

    let pass = crate::intake_batch::collect_cached_for(cache, "codex", &files, parse_file);
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). Byte-identical wrapper over `collect`.
pub struct Codex;
impl crate::ingest::registry::Adapter for Codex {
    fn name(&self) -> &'static str {
        "codex"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".codex").join("sessions")]
    }
    fn archive_roots(&self) -> Vec<std::path::PathBuf> {
        vec![
            crate::ingest::home().join(".codex/sessions"),
            crate::ingest::home().join(".codex/archived_sessions"),
        ]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        is_rollout_file(path)
    }
}
