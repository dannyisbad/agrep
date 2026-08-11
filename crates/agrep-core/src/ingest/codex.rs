//! Codex CLI adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
//! One JSONL file per session. The first line is `session_meta` (gives cwd -> project,
//! and the session id). Real human turns are `response_item` lines with
//! payload.type=="message" and payload.role=="user", text in `input_text`/`text` blocks.
//!
//! Much of what arrives under `role:user` is codex's own composition: AGENTS.md and the
//! environment block, plugin and skill catalogs, delegation payloads, system notifications.
//! Their shape is indistinguishable from typed prose, so eligibility comes from the rollout's
//! `event_msg`/`user_message` log (`Submissions`), which records keyboard submissions and
//! nothing else. `is_wrapper`/`is_codex_injected` still veto client echoes the log does carry,
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

/// `message` is a string only on `user_message`; other payloads type it as an
/// object, and those lines fail this borrow and are skipped rather than coerced.
#[derive(Deserialize)]
struct SubmissionPayload<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Cow<'a, str>>,
}

/// What the rollout records the human as having submitted.
///
/// Codex composes the `role:user` response_items it sends to the model: AGENTS.md,
/// the environment block, plugin and skill catalogs, delegation payloads and typed
/// prose all arrive under the same role, and nothing in their shape separates a
/// config file from a sentence. The rollout's own `event_msg`/`user_message` log is
/// the seam: it exists once per keyboard submission and never for injected input.
struct Submissions {
    /// Sorted and deduped, so `attests` binary-searches instead of scanning.
    texts: Vec<String>,
}

impl Submissions {
    fn collect(data: &str) -> Self {
        let finder = memmem::Finder::new(b"\"user_message\"");
        let mut texts: Vec<String> = Vec::new();
        for line in data.lines() {
            if finder.find(line.as_bytes()).is_none() {
                continue;
            }
            let parsed: SubmissionLine = match serde_json::from_str(line) {
                Ok(parsed) => parsed,
                Err(_) => continue,
            };
            if parsed.ty.as_deref() != Some("event_msg") {
                continue;
            }
            let text = parsed
                .payload
                .as_ref()
                .filter(|p| p.ty.as_deref() == Some("user_message"))
                .and_then(|p| p.message.as_deref())
                .map(str::trim)
                .filter(|t| !t.is_empty());
            if let Some(text) = text {
                texts.push(text.to_string());
            }
        }
        texts.sort_unstable();
        texts.dedup();
        Self { texts }
    }

    fn is_empty(&self) -> bool {
        self.texts.is_empty()
    }

    /// A response_item is the human's turn when the log carries its text. Codex
    /// appends paste markers after the typed prose, so a logged submission that
    /// starts the item attests it; in sort order the only candidate prefix is the
    /// item's immediate predecessor.
    fn attests(&self, text: &str) -> bool {
        let text = text.trim();
        let after = self.texts.partition_point(|s| s.as_str() <= text);
        after > 0 && text.starts_with(self.texts[after - 1].as_str())
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
#[cfg(test)]
fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    parse_file_with_tally(path, crate::intake::file("codex", path))
}

fn parse_file_stamped(
    path: &Path,
    mtime_ns: i64,
    size: u64,
) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    parse_file_with_tally(
        path,
        crate::intake::file_stamped("codex", path, mtime_ns, size),
    )
}

fn parse_file_with_tally(
    path: &Path,
    tally: std::sync::Arc<crate::intake::Tally>,
) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!(
                "  ! codex: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            );
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Skipped,
            );
        }
    };

    let submissions = Submissions::collect(&data);
    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut recap_turns = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    // call_id -> index into `events`, so the *_output line can pair up.
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;
    let mut project: Option<String> = None;
    let mut session: Option<String> = None;
    let mut first_meta_seen = false;
    let mut subagent: Option<SubagentContext> = None;
    let mut internal_guardian = false;
    // The active model, updated by each turn_context line and stamped onto turns.
    let mut current_model = String::new();

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
    for (record_ordinal, line) in data.lines().enumerate() {
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
                            tally.discard_rows_as_replay(out.len() as u64);
                            tally.discard_events(events.len() as u64);
                            events.clear();
                            out.clear();
                            turn = 0;
                        }
                        tally.row();
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
                                && (submissions.is_empty() || submissions.attests(&text)))
                            .then_some(text)
                        })
                    });
                let message_turn_id = payload
                    .internal_chat_message_metadata_passthrough
                    .as_ref()
                    .and_then(|m| m.turn_id.as_deref());
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
                        tally.discard_rows_as_replay(out.len() as u64);
                        tally.discard_events(events.len() as u64);
                        events.clear();
                        out.clear();
                        turn = 0;
                    }
                    tally.row();
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
                let (input, input_chars) = if let Some(raw) = payload.arguments {
                    let args_val: serde_json::Value =
                        serde_json::from_str(raw.get()).unwrap_or_default();
                    control = ciphertext_send_message(&name, &args_val);
                    match args_val {
                        serde_json::Value::String(inner) => {
                            serde_json::from_str::<serde_json::Value>(&inner)
                                .map(|v| summarize_tool_input_with_chars(&v))
                                .unwrap_or_else(|_| cap_str_with_chars(&inner, EVENT_CAP))
                        }
                        v => summarize_tool_input_with_chars(&v),
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
                    let chars = crate::ingest::append_capped(
                        &mut last.reply,
                        &txt,
                        crate::ingest::REPLY_CAP,
                    );
                    last.reply_chars += chars;
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
        if !submissions.attests(&text) {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }

        tally.row();
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
        });
        turn += 1;
    }

    // Canonical prefilter skips dominate the line count. Fold them into the per-file tally
    // once, keeping the same seen identity while avoiding millions of atomic increments.
    let fast_seen = fast_meta + fast_non_message;
    tally.seen_n(fast_seen);
    tally.skip_n(crate::intake::Skip::Meta, fast_meta);
    tally.skip_n(crate::intake::Skip::NonMessage, fast_non_message);

    (
        out.into_iter()
            .map(|raw| {
                let turn = raw.turn;
                let mut message = raw.freeze();
                if recap_turns.binary_search(&turn).is_ok() {
                    message.who = "recap".into();
                    message.model_source = "recap".into();
                }
                message
            })
            .collect(),
        events,
        crate::ingest_cache::ReadOutcome::Complete,
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
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".codex").join("sessions");
    let files = cache.preflight_source_paths(&root).unwrap_or_else(|| {
        let mut files = Vec::new();
        gather(&root, &mut files, true);
        files.sort_unstable();
        files
    });

    let pass = crate::ingest_cache::collect_cached_stamped_for(
        cache,
        "codex",
        &root,
        &files,
        parse_file_stamped,
    );
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
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".codex").join("sessions")]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        is_rollout_file(path)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        ciphertext_send_message, extract_text, fast_class, is_guardian_source, parse_file,
        raw_call_output, Block, FastClass, Line, Submissions,
    };

    /// The teach block agrep installs into AGENTS.md. Codex hands it back inside a
    /// composed role:user turn, so it is the exact shape that must never index as a
    /// human turn no matter what prose it carries.
    const INSTALLED_BLOCK: &str = concat!(
        "# AGENTS.md instructions\n\n<INSTRUCTIONS>\n<!-- agrep:recall v19 -->\n",
        "    $ agrep \"no kernel image\"\n    $ agrep recall deadlock --probe\n</INSTRUCTIONS>",
    );

    fn rollout(
        dir: &std::path::Path,
        name: &str,
        records: &[serde_json::Value],
    ) -> std::path::PathBuf {
        std::fs::create_dir_all(dir).unwrap();
        let path = dir.join(name);
        let body: Vec<String> = records.iter().map(|r| r.to_string()).collect();
        std::fs::write(&path, body.join("\n") + "\n").unwrap();
        path
    }

    fn scratch(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "agrep-codex-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    fn user_item(text: &str) -> serde_json::Value {
        serde_json::json!({
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}
        })
    }

    fn submitted(text: &str) -> serde_json::Value {
        serde_json::json!({
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": text}
        })
    }

    fn meta(id: &str, extra: serde_json::Value) -> serde_json::Value {
        let mut payload =
            serde_json::json!({"type": "session_meta", "id": id, "cwd": "/work/agrep"});
        for (k, v) in extra.as_object().unwrap() {
            payload[k] = v.clone();
        }
        serde_json::json!({"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": payload})
    }

    #[test]
    fn composed_role_user_input_is_not_a_human_turn_however_it_reads() {
        let root = scratch("composed");
        // The composed turn carries the installed block; the typed turn carries the
        // same words, so only provenance separates them.
        let echo = "why does agrep recall deadlock miss?";
        let path = rollout(
            &root,
            "rollout-composed.jsonl",
            &[
                meta("session-composed", serde_json::json!({})),
                user_item(INSTALLED_BLOCK),
                user_item(echo),
                submitted(echo),
            ],
        );
        let (msgs, _, _) = parse_file(&path);
        let texts: Vec<String> = msgs.iter().map(|m| m.text.to_string()).collect();
        assert_eq!(texts, vec![echo.to_string()]);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn a_paste_marker_after_the_typed_prose_keeps_the_turn() {
        let root = scratch("paste");
        let path = rollout(
            &root,
            "rollout-paste.jsonl",
            &[
                meta("session-paste", serde_json::json!({})),
                user_item("look at this\n\n<image>\n</image>"),
                submitted("look at this"),
            ],
        );
        let (msgs, _, _) = parse_file(&path);
        assert_eq!(msgs.len(), 1);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn a_short_submission_does_not_attest_the_block_that_merely_contains_it() {
        let submissions = Submissions::collect(
            &[
                submitted("hi").to_string(),
                submitted("continue").to_string(),
            ]
            .join("\n"),
        );
        assert!(submissions.attests("hi"));
        assert!(!submissions.attests(&format!("{INSTALLED_BLOCK}\ncontinue")));
    }

    #[test]
    fn a_subagent_rollout_without_a_submission_log_yields_no_human_turns() {
        let root = scratch("subthread");
        let path = rollout(
            &root,
            "rollout-subthread.jsonl",
            &[
                meta(
                    "session-subthread",
                    serde_json::json!({"thread_source": "subagent",
                                       "parent_thread_id": "session-parent",
                                       "agent_path": "/root/child"}),
                ),
                user_item(INSTALLED_BLOCK),
                user_item("replayed parent prose"),
            ],
        );
        let (msgs, _, _) = parse_file(&path);
        assert!(
            msgs.is_empty(),
            "{:?}",
            msgs.iter().map(|m| &m.text).collect::<Vec<_>>()
        );
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn a_rollout_with_no_submission_log_yields_no_human_turns() {
        let root = scratch("unlogged");
        let path = rollout(
            &root,
            "rollout-unlogged.jsonl",
            &[
                meta("session-unlogged", serde_json::json!({})),
                user_item(INSTALLED_BLOCK),
            ],
        );
        let (msgs, _, _) = parse_file(&path);
        assert!(msgs.is_empty());
        std::fs::remove_dir_all(&root).ok();
    }

    fn high_entropy_token(len: usize) -> String {
        const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut state = 0x9e37_79b9_7f4a_7c15u64;
        (0..len)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                ALPHABET[(state as usize) & 63] as char
            })
            .collect()
    }

    #[test]
    fn ciphertext_detection_is_scoped_and_conservative() {
        let fernet = format!("gAAAAAB{}==", "A".repeat(96));
        let entropy = high_entropy_token(1024);
        for message in [&fernet, &entropy] {
            assert!(ciphertext_send_message(
                "send_message",
                &serde_json::json!({"target": "/root", "message": message})
            ));
        }

        let prose_base64 = "VGhlIHF1aWNrIGJyb3duIGZveCBqdW1wcyBvdmVyIHRoZSBsYXp5IGRvZw".repeat(12);
        let long_identifier = format!("parse_cache_generation_{}", "deadbeef".repeat(80));
        for message in [
            "review the allocator before release",
            prose_base64.as_str(),
            long_identifier.as_str(),
            "INFO relay payload gAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ] {
            assert!(!ciphertext_send_message(
                "send_message",
                &serde_json::json!({"message": message})
            ));
        }
        assert!(!ciphertext_send_message(
            "exec_command",
            &serde_json::json!({"message": fernet})
        ));
        assert!(!ciphertext_send_message(
            "send_message",
            &serde_json::json!({"message": high_entropy_token(256)})
        ));
    }

    #[test]
    fn ciphertext_send_messages_remain_diagnostic_control_events() {
        let root = std::env::temp_dir().join(format!(
            "agrep-codex-ciphertext-events-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("rollout-ciphertext.jsonl");
        let calls = [
            ("gAAAAAB".to_string() + &"A".repeat(128), "fernet"),
            (high_entropy_token(1024), "entropy"),
            ("please inspect the cache key".to_string(), "prose"),
        ];
        let mut records = vec![serde_json::json!({
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"type": "session_meta", "id": "session-ciphertext", "cwd": "/work/agrep"}
        })];
        for (ordinal, (message, call_id)) in calls.into_iter().enumerate() {
            records.push(serde_json::json!({
                "timestamp": format!("2026-01-01T00:00:0{}Z", ordinal + 1),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "send_message",
                    "arguments": serde_json::json!({"target": "/root", "message": message}).to_string(),
                    "call_id": call_id
                }
            }));
        }
        let body = records
            .into_iter()
            .map(|record| record.to_string())
            .collect::<Vec<_>>()
            .join("\n");
        std::fs::write(&path, body + "\n").unwrap();

        let (_, events, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(events.len(), 3);
        assert_eq!(events[0].kind, "control");
        assert_eq!(events[1].kind, "control");
        assert_eq!(events[2].kind, "tool");
        assert!(events[2].input.contains("please inspect the cache key"));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn routed_output_text_opens_the_subagent_boundary() {
        let blocks: Vec<Block<'_>> = serde_json::from_str(
            r#"[{"type":"output_text","text":"Message Type: NEW_TASK\nTask name: /root/a\nPayload:\ninspect the allocator"}]"#,
        )
        .unwrap();
        assert_eq!(
            extract_text(&blocks).as_deref(),
            Some("Message Type: NEW_TASK\nTask name: /root/a\nPayload:\ninspect the allocator")
        );
    }

    #[test]
    fn anonymous_web_searches_keep_every_record_with_actionable_ids() {
        let root = std::env::temp_dir().join(format!(
            "agrep-codex-web-events-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("rollout-web-identity.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"timestamp\":\"2026-01-01T00:00:00Z\",\"type\":\"session_meta\",\"payload\":{\"type\":\"session_meta\",\"id\":\"session-web\",\"cwd\":\"/work/agrep\"}}\n",
                "{\"timestamp\":\"2026-01-01T00:00:01Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"web_search_call\",\"action\":{\"query\":\"alpha\"}}}\n",
                "{\"timestamp\":\"2026-01-01T00:00:02Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"web_search_call\",\"action\":{\"queries\":[\"beta\",\"gamma\"]}}}\n",
                "{\"timestamp\":\"2026-01-01T00:00:03Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"web_search_call\",\"id\":\"ws-native\",\"action\":{\"query\":\"delta\"}}}\n"
            ),
        )
        .unwrap();
        let (_, events, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(events.len(), 3);
        assert_eq!(events[0].call_id, "codex:rollout-web-identity:1");
        assert_eq!(events[1].call_id, "codex:rollout-web-identity:2");
        assert_eq!(events[1].input, "beta · gamma");
        assert_eq!(events[2].call_id, "ws-native");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn guardian_subagent_source_is_internal_control_traffic() {
        let guardian: Box<serde_json::value::RawValue> =
            serde_json::from_str(r#"{"subagent":{"other":"guardian"}}"#).unwrap();
        let ordinary: Box<serde_json::value::RawValue> =
            serde_json::from_str(r#"{"subagent":{"thread_spawn":{"agent_path":"/root/a"}}}"#)
                .unwrap();
        let other_review: Box<serde_json::value::RawValue> =
            serde_json::from_str(r#"{"subagent":{"other":"review"}}"#).unwrap();
        assert!(is_guardian_source(Some(&guardian)));
        assert!(!is_guardian_source(Some(&ordinary)));
        assert!(!is_guardian_source(Some(&other_review)));
        assert!(!is_guardian_source(None));
    }

    #[test]
    fn canonical_prefix_classifies_outer_record_before_nested_keys() {
        assert_eq!(
            fast_class(
                br#"{"timestamp":"2026-01-01T00:00:00Z","type":"event_msg","payload":{"type":"mcp_tool_call_end","call_id":"c","result":{"role":"assistant"}}}"#
            ),
            FastClass::Meta
        );
        assert_eq!(
            fast_class(
                br#"{"timestamp":"2026-01-01T00:00:00Z","type":"compacted","payload":{"message":{"role":"user"}}}"#
            ),
            FastClass::Compact
        );
    }

    #[test]
    fn native_compaction_publishes_only_an_empty_structural_recap() {
        let root = scratch("compact-boundary");
        let path = rollout(
            &root,
            "rollout-compact-boundary.jsonl",
            &[
                meta("session-compact-boundary", serde_json::json!({})),
                serde_json::json!({
                    "timestamp": "2026-01-01T00:00:02Z",
                    "type": "compacted",
                    "payload": {
                        "message": "",
                        "replacement_history": [{
                            "type": "message",
                            "role": "user",
                            "content": "secret replacement prose"
                        }],
                        "window_id": "window-2",
                        "window_number": 2
                    }
                }),
            ],
        );
        let (msgs, events, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert!(events.is_empty());
        assert_eq!(msgs.len(), 1);
        assert_eq!(&*msgs[0].session, "session-compact-boundary");
        assert_eq!(msgs[0].turn, 0);
        assert_eq!(&*msgs[0].who, "recap");
        assert_eq!(&*msgs[0].model_source, "recap");
        assert!(msgs[0].text.is_empty());
        assert!(msgs[0].reply.is_empty());
        assert_eq!(msgs[0].ts, 1_767_225_602_000);
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn canonical_prefix_keeps_every_consumed_record_kind() {
        for line in [
            br#"{"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"s"}}"#.as_slice(),
            br#"{"timestamp":"2026-01-01T00:00:00Z","type":"turn_context","payload":{"model":"m"}}"#.as_slice(),
            br#"{"timestamp":"2026-01-01T00:00:00Z","type":"event_msg","payload":{"type":"task_started"}}"#.as_slice(),
            br#"{"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"message","role":"user"}}"#.as_slice(),
            br#"{"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"web_search_call"}}"#.as_slice(),
        ] {
            assert_eq!(fast_class(line), FastClass::Parse);
        }
        assert_eq!(
            fast_class(
                br#"{"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":"ok"}}"#
            ),
            FastClass::CallOutput
        );
        assert_eq!(
            fast_class(
                br#"{"timestamp":"2026-01-01T00:00:00Z","type":"response_item","payload":{"type":"reasoning","call_id":"decoy"}}"#
            ),
            FastClass::NonMessage
        );
    }

    #[test]
    fn noncanonical_json_uses_conservative_fallback() {
        assert_eq!(
            fast_class(
                br#"{ "type": "response_item", "timestamp": "2026-01-01", "payload": {"type":"message"}}"#
            ),
            FastClass::Fallback
        );
    }

    #[test]
    fn canonical_call_output_slices_only_the_structural_value() {
        let line = r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","call_id":"call-7","output":{"output":"brace } and escaped \" quote","metadata":{"exit_code":0}},"internal_chat_message_metadata_passthrough":{"turn_id":"7"}}}"#;
        let (call_id, output) = raw_call_output(line).unwrap();
        assert_eq!(call_id, "call-7");
        assert_eq!(
            output,
            r#"{"output":"brace } and escaped \" quote","metadata":{"exit_code":0}}"#
        );
        let string_line = r#"{"timestamp":"t","type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"c","output":"line 1\nline 2"}}"#;
        assert_eq!(
            raw_call_output(string_line).unwrap().1,
            r#""line 1\nline 2""#
        );
    }

    #[test]
    fn canonical_call_output_rejects_partial_or_invalid_records() {
        for line in [
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":"closed"}"#,
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":truX}}"#,
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":"ok",}}"#,
            "{\"timestamp\":\"t\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"c\",\"output\":\u{000b}\"ok\"}}",
            "{\"timestamp\":\"t\",\"type\":\"response_item\",\"payload\":{\"type\":\"function_call_output\",\"call_id\":\"c\",\"output\":\"ok\"}}\u{000c}",
        ] {
            // These still have the canonical prefix, so accepting them here would
            // bypass parse-error intake accounting in parse_file.
            assert_eq!(fast_class(line.as_bytes()), FastClass::CallOutput);
            assert!(raw_call_output(line).is_none());
            assert!(serde_json::from_str::<serde_json::Value>(line).is_err());
        }
    }

    #[test]
    fn canonical_call_output_preserves_ignored_field_schema_errors() {
        for line in [
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","id":7,"call_id":"c","output":"ok"}}"#,
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":"ok","internal_chat_message_metadata_passthrough":7}}"#,
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":"ok","internal_chat_message_metadata_passthrough":{"turn_id":7}}}"#,
        ] {
            assert_eq!(fast_class(line.as_bytes()), FastClass::CallOutput);
            assert!(raw_call_output(line).is_none());
            assert!(serde_json::from_str::<Line<'_>>(line).is_err());
        }

        for line in [
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","id":"row-1","call_id":"c","output":"ok"}}"#,
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","id":null,"call_id":"c","output":"ok"}}"#,
            r#"{"timestamp":"t","type":"response_item","payload":{"type":"function_call_output","call_id":"c","output":"ok","internal_chat_message_metadata_passthrough":null}}"#,
        ] {
            assert!(raw_call_output(line).is_some());
            assert!(serde_json::from_str::<Line<'_>>(line).is_ok());
        }
    }
}
