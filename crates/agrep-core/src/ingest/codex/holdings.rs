//! What a rollout holds, read without the parser's rules: says why a rollout that made no
//! rows has none.

use std::io::{BufRead, Read};
use std::path::Path;

use serde_json::Value;

/// The records of a rollout that rows come from.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Holdings {
    /// It entered Codex's review mode.
    pub review: bool,
    /// Its assistant messages.
    pub replies: usize,
    /// Its tool calls.
    pub calls: usize,
}

/// The holdings of the rollout at `path`, or `None` when it cannot be read to its end.
pub fn holdings(path: &Path) -> Option<Holdings> {
    crate::ingest::registry::read_regular_snapshot(path, |file| read(path, file))
        .ok()
        .flatten()
        .flatten()
}

fn read(path: &Path, file: &std::fs::File) -> Option<Holdings> {
    let reader: Box<dyn Read + '_> = if super::discover::is_compressed(path) {
        Box::new(zstd::stream::read::Decoder::new(file).ok()?)
    } else {
        Box::new(file)
    };
    let mut held = Holdings::default();
    for line in std::io::BufReader::new(reader).lines() {
        let Ok(record) = serde_json::from_str::<Value>(&line.ok()?) else {
            continue;
        };
        let payload = &record["payload"];
        match (record["type"].as_str(), payload["type"].as_str()) {
            (Some("response_item"), Some("message")) if payload["role"] == "assistant" => {
                held.replies += 1;
            }
            (
                Some("response_item"),
                Some("function_call" | "custom_tool_call" | "tool_search_call" | "web_search_call"),
            ) => held.calls += 1,
            (Some("event_msg"), Some("item_completed"))
                if payload["item"]["type"] == "EnteredReviewMode" =>
            {
                held.review = true;
            }
            _ => {}
        }
    }
    Some(held)
}
