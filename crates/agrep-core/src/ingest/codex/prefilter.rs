//! Byte-level prefilter: settles most rollout records from their canonical compact layout
//! without running serde, and admits everything else conservatively.

use std::borrow::Cow;

use memchr::memmem;
use serde_json::value::RawValue;

use super::records::ChatMessageMeta;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum FastClass {
    /// The canonical prefix proves this is a record the parser consumes.
    Parse,
    /// Canonical tool output: extract call_id/output directly and avoid walking the
    /// often multi-megabyte record once just to capture output as RawValue.
    CallOutput,
    /// Canonical bookkeeping which the adapter deliberately does not materialize.
    Meta,
    /// A canonical `item_completed` event: bookkeeping unless its item is one the parser
    /// reads (`item_type`).
    Item,
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
pub(super) fn fast_class(bytes: &[u8]) -> FastClass {
    let Some((_, after_timestamp)) = split_timestamp(bytes) else {
        return FastClass::Fallback;
    };
    let Some(after_type) = after_timestamp.strip_prefix(b"\",\"type\":\"") else {
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
                event_msg_class(payload)
            } else {
                response_item_class(payload)
            }
        }
        b"compacted" => FastClass::Compact,
        // any other outer record kind deserializes to the non-message arm anyway; nested
        // message-shaped data must not make the prefilter parse it
        _ => FastClass::NonMessage,
    }
}

fn event_msg_class(payload: &[u8]) -> FastClass {
    // task_started supplies the child-owned turn boundary and item_completed can open a
    // review. Every other event_msg is bookkeeping (including huge duplicated MCP tool
    // outputs).
    match payload {
        b"task_started" => FastClass::Parse,
        b"item_completed" => FastClass::Item,
        _ => FastClass::Meta,
    }
}

/// The type of a canonical `item_completed` event's item: the first `"item":{"type":` holds
/// it, since the payload's own fields before it are short ids.
pub(super) fn item_type(bytes: &[u8]) -> Option<&[u8]> {
    const ITEM: &[u8] = b"\"item\":{\"type\":\"";
    let start = memmem::find(bytes, ITEM)? + ITEM.len();
    let len = memchr::memchr(b'"', &bytes[start..])?;
    Some(&bytes[start..start + len])
}

fn response_item_class(payload: &[u8]) -> FastClass {
    match payload {
        b"message" | b"agent_message" | b"function_call" | b"custom_tool_call"
        | b"tool_search_call" | b"web_search_call" => FastClass::Parse,
        b"function_call_output" | b"custom_tool_call_output" => FastClass::CallOutput,
        _ => FastClass::NonMessage,
    }
}

/// Split a canonical record at the closing quote of its leading timestamp:
/// `(timestamp bytes, rest starting at that quote)`.
fn split_timestamp(bytes: &[u8]) -> Option<(&[u8], &[u8])> {
    let after = bytes.strip_prefix(b"{\"timestamp\":\"")?;
    let end = memchr::memchr(b'"', after)?;
    Some(after.split_at(end))
}

/// A timestamp whose raw bytes need no JSON unescaping.
fn is_plain_timestamp(raw: &[u8]) -> bool {
    !raw.iter().any(|byte| *byte < 0x20 || *byte == b'\\')
}

pub(super) fn canonical_timestamp(bytes: &[u8]) -> Option<&str> {
    let (raw, _) = split_timestamp(bytes)?;
    if !is_plain_timestamp(raw) {
        return None;
    }
    std::str::from_utf8(raw).ok()
}

#[inline]
fn is_json_whitespace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r')
}

/// Parse one JSON value at ``start`` without materializing it. ``RawValue`` validates
/// strings, escapes, primitives, and balanced containers; the stream offset gives the
/// exact end even when a compact object field follows immediately.
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
pub(super) fn raw_call_output(line: &str) -> Option<(&str, &str)> {
    let bytes = line.as_bytes();
    let mut i = call_output_fields_start(bytes)?;
    let mut fields = CallOutputFields::default();
    while bytes.get(i) == Some(&b',') {
        let (key, raw, end) = compact_field(bytes, i + 1)?;
        fields.accept(key, raw)?;
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
    Some((fields.call_id?, fields.output?))
}

/// Offset just past the payload type's closing quote of a canonical
/// `function_call_output`/`custom_tool_call_output` record.
fn call_output_fields_start(bytes: &[u8]) -> Option<usize> {
    let (timestamp, after_timestamp) = split_timestamp(bytes)?;
    if !is_plain_timestamp(timestamp) {
        return None;
    }
    let after_type =
        after_timestamp.strip_prefix(b"\",\"type\":\"response_item\",\"payload\":{\"type\":\"")?;
    let payload_type_end = memchr::memchr(b'"', after_type)?;
    if !matches!(
        &after_type[..payload_type_end],
        b"function_call_output" | b"custom_tool_call_output"
    ) {
        return None;
    }
    Some(bytes.len() - after_type.len() + payload_type_end + 1)
}

/// One compact `"key":value` member starting at its opening quote: `(key, raw value, end)`.
fn compact_field(bytes: &[u8], mut i: usize) -> Option<(&[u8], &str, usize)> {
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
    Some((key, raw, end))
}

/// The payload members a canonical call output may carry, each accepted at most once.
#[derive(Default)]
struct CallOutputFields<'a> {
    call_id: Option<&'a str>,
    output: Option<&'a str>,
    seen_id: bool,
    seen_metadata: bool,
}

impl<'a> CallOutputFields<'a> {
    /// `None` rejects the record (unknown, repeated or mistyped member).
    fn accept(&mut self, key: &[u8], raw: &'a str) -> Option<()> {
        match key {
            b"id" if !self.seen_id => {
                // Match Payload::id exactly: null or a (possibly escaped) string.
                let _: Option<Cow<'_, str>> = serde_json::from_str(raw).ok()?;
                self.seen_id = true;
            }
            b"call_id" if self.call_id.is_none() => {
                let id = serde_json::from_str::<&str>(raw).ok()?;
                if !id.is_ascii() {
                    return None;
                }
                self.call_id = Some(id);
            }
            b"output" if self.output.is_none() => self.output = Some(raw),
            b"internal_chat_message_metadata_passthrough" if !self.seen_metadata => {
                // Do not let the raw shortcut hide a type error the ordinary Line
                // deserializer would count. Unknown object fields remain accepted.
                let _: Option<ChatMessageMeta<'_>> = serde_json::from_str(raw).ok()?;
                self.seen_metadata = true;
            }
            _ => return None,
        }
        Some(())
    }
}

/// Raw-byte prefilter invariant for records `fast_class` cannot prove (sound for the
/// quoted key needles - see the claude.rs raw_str_value doc): every line kind the parser
/// consumes is admitted by one of these needles - messages carry `"role":`, tool
/// calls/outputs carry `"call_id":`, plus the two meta line types (and web_search_call,
/// whose call_id is not guaranteed) and review mode's opening. The reasoning items that
/// dominate rollout bytes match none and are skipped without touching serde; an
/// unquoted-needle false positive just costs one wasted parse. A review's request is
/// admitted apart, only while a review awaits it (`holds_request`).
pub(super) struct FallbackNeedles([memmem::Finder<'static>; 9]);

/// Whether a record's bytes can hold the message Codex hands a review thread: its item type,
/// as a quoted JSON token.
pub(super) fn holds_request(bytes: &[u8]) -> bool {
    memmem::find(bytes, b"\"UserMessage\"").is_some()
}

impl FallbackNeedles {
    pub(super) fn new() -> Self {
        Self([
            memmem::Finder::new(b"\"role\":"),
            memmem::Finder::new(b"\"call_id\":"),
            memmem::Finder::new(b"session_meta"),
            memmem::Finder::new(b"turn_context"),
            memmem::Finder::new(b"web_search_call"),
            memmem::Finder::new(b"agent_message"),
            memmem::Finder::new(b"task_started"),
            memmem::Finder::new(b"compacted"),
            memmem::Finder::new(b"\"EnteredReviewMode\""),
        ])
    }

    pub(super) fn admits(&self, bytes: &[u8]) -> bool {
        self.0.iter().any(|needle| needle.find(bytes).is_some())
    }
}
