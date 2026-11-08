//! Borrowed record schemas, the raw-byte key probes that pick which one to parse, and the
//! readers of a record's `message.content`.

use std::borrow::Cow;

use memchr::memmem;
use serde::Deserialize;
use serde_json::value::RawValue;

// Borrowed deserialization: scalar fields are Cow (borrow from the line when escape-free,
// allocate only when JSON-escaped) and `content` stays a RawValue slice, parsed into a
// DOM only by the lines that actually need one - most fail a filter first.
#[derive(Deserialize)]
pub(super) struct Line<'a> {
    #[serde(rename = "type", borrow)]
    pub(super) ty: Option<Cow<'a, str>>,
    #[serde(rename = "isMeta")]
    pub(super) is_meta: Option<bool>,
    #[serde(rename = "isSidechain")]
    pub(super) is_sidechain: Option<bool>,
    #[serde(rename = "isCompactSummary")]
    pub(super) is_compact_summary: Option<bool>,
    #[serde(rename = "userType", borrow)]
    pub(super) user_type: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) message: Option<Msg<'a>>,
    #[serde(borrow)]
    pub(super) timestamp: Option<Cow<'a, str>>,
    #[serde(rename = "sessionId", borrow)]
    pub(super) session_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) cwd: Option<&'a RawValue>,
    // Raw so an unexpected shape degrades to "not a marker" instead of failing the row.
    #[serde(rename = "isApiErrorMessage", borrow)]
    pub(super) is_api_error: Option<&'a RawValue>,
    #[serde(borrow)]
    pub(super) origin: Option<&'a RawValue>,
    #[serde(borrow)]
    pub(super) attachment: Option<&'a RawValue>,
    // the tool's structured result beside its display text; raw, parsed only for agent calls
    #[serde(rename = "toolUseResult", borrow)]
    pub(super) tool_use_result: Option<&'a RawValue>,
}

impl Line<'_> {
    /// The row's timestamp in epoch millis; 0 when absent or unparseable.
    pub(super) fn ts(&self) -> i64 {
        crate::ingest::parse_timestamp::rfc3339(self.timestamp.as_deref())
    }
}

#[derive(Deserialize)]
pub(super) struct ProjectLine<'a> {
    #[serde(borrow)]
    pub(super) cwd: Option<&'a RawValue>,
}

#[derive(Deserialize)]
pub(super) struct Msg<'a> {
    #[serde(borrow)]
    pub(super) role: Option<Cow<'a, str>>,
    #[serde(borrow)]
    pub(super) content: Option<&'a RawValue>,
    #[serde(borrow)]
    pub(super) model: Option<Cow<'a, str>>,
}

/// Extract the raw (still-escaped) bytes of the JSON string value that follows `finder`'s
/// `"key":"` needle. The escaped form is fine for project-histogram keys: segmentation
/// splits on both slash kinds and drops empty segments, so `C:\\Users` (escaped) and
/// `C:\Users` bucket to the same project root.
///
/// SOUNDNESS of raw-byte key needles, here and in the prefilters: inside any JSON string
/// value a quote is escaped to `\"`, so the byte sequence `"key":` can only occur as a
/// real object key - string contents can never produce a false match on it.
pub(super) fn raw_str_value<'a>(finder: &memmem::Finder, bytes: &'a [u8]) -> Option<&'a str> {
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

pub(super) fn has_json_key(finder: &memmem::Finder, bytes: &[u8]) -> bool {
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

pub(super) fn optional_cwd(value: Option<&RawValue>) -> Option<String> {
    value
        .and_then(|raw| serde_json::from_str::<String>(raw.get()).ok())
        .filter(|cwd| !cwd.is_empty())
}

pub(super) fn is_api_error_row(line: &Line) -> bool {
    line.is_api_error.is_some_and(|raw| raw.get() == "true")
}

/// A raw `content` parsed into a DOM; None when absent or malformed.
pub(super) fn parse_content(raw: Option<&RawValue>) -> Option<serde_json::Value> {
    raw.and_then(|r| serde_json::from_str(r.get()).ok())
}

/// The text blocks of a block array, newline-joined; empty texts add no separator.
pub(super) fn joined_text_blocks(blocks: &[serde_json::Value]) -> String {
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

/// Pull human text out of a `message.content` that may be a string or a block array.
pub(super) fn extract_text(content: &serde_json::Value) -> Option<String> {
    match content {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Array(blocks) => {
            Some(joined_text_blocks(blocks)).filter(|out| !out.is_empty())
        }
        _ => None,
    }
}
