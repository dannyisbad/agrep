//! The session graph: which records form the active branch, and the tool
//! results on it.

use std::collections::{HashMap, HashSet};

use super::record::{is_role, precapped, text_of};
use crate::ingest::cap_event_output;

/// One decoded JSONL line plus its file ordinal, which names tool calls and
/// bash runs that carry no id of their own.
pub(super) struct Record<'a> {
    pub(super) value: &'a serde_json::Value,
    pub(super) ordinal: usize,
}

/// A toolResult's capped output, as its call's event carries it.
#[derive(Clone, Default)]
pub(super) struct ToolResult {
    pub(super) output: String,
    pub(super) ok: Option<bool>,
    pub(super) chars: usize,
    pub(super) bytes: usize,
}

/// The active branch, oldest first. The last record in the file is the current
/// leaf; walking parentId back to the root drops branches the user abandoned.
/// A record whose parent is missing ends the walk rather than failing the file.
pub(super) fn active_branch(records: &[Record<'_>]) -> Vec<usize> {
    let mut by_id: HashMap<&str, usize> = HashMap::new();
    for (index, record) in records.iter().enumerate() {
        if let Some(id) = record.value.get("id").and_then(|i| i.as_str()) {
            by_id.insert(id, index);
        }
    }
    let leaf = records.iter().rposition(|record| {
        record.value.get("type").and_then(|t| t.as_str()) != Some("session")
            && record.value.get("id").and_then(|i| i.as_str()).is_some()
    });
    let Some(leaf) = leaf else {
        // v1: no ids at all, so the file order IS the conversation
        return (0..records.len()).collect();
    };
    let mut chain = Vec::new();
    let mut seen = HashSet::new();
    let mut cursor = Some(leaf);
    while let Some(index) = cursor {
        if !seen.insert(index) {
            break; // a parentId cycle is corrupt input, not a reason to panic
        }
        chain.push(index);
        cursor = records[index]
            .value
            .get("parentId")
            .and_then(|p| p.as_str())
            .and_then(|parent| by_id.get(parent).copied());
    }
    chain.reverse();
    chain
}

/// Outputs of every toolResult on the branch, keyed by the call they answer, so
/// a toolCall can be emitted with its result even though they are separate rows.
pub(super) fn tool_results(
    records: &[Record<'_>],
    branch: &[usize],
) -> HashMap<String, ToolResult> {
    let mut results = HashMap::new();
    for &index in branch {
        let Some(m) = records[index]
            .value
            .get("message")
            .filter(|m| is_role(m, "toolResult"))
        else {
            continue;
        };
        let Some(id) = m.get("toolCallId").and_then(|v| v.as_str()) else {
            continue;
        };
        let raw = text_of(m.get("content"));
        let (output, chars, bytes) = match precapped(m) {
            Some((chars, bytes)) => (raw, chars, bytes),
            None => cap_event_output(&raw),
        };
        results.insert(
            id.to_string(),
            ToolResult {
                output,
                ok: m.get("isError").and_then(|v| v.as_bool()).map(|v| !v),
                chars,
                bytes,
            },
        );
    }
    results
}
