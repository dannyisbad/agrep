//! Canonical transcript windows, read together with events in one SQLite snapshot.
use crate::handles::{self, Claim};
use serde::{Deserialize, Serialize};
use serde_json::Value;

mod load;
mod render;
mod text;
mod tools;

pub use load::{load, load_events, load_rows, windows};
pub use render::{render, Display, Rendered};
pub use text::{
    anchor_patterns, anchored_cap, cap, content_terms, coverage, expand_command, one_line,
    py_space, weak_snippet, Anchor, Coverage,
};
pub use tools::{selected_tool_match, tool_line, ToolMatch, MARKERS};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Row {
    pub session: String,
    pub turn: i64,
    pub who: String,
    pub ts: i64,
    pub text: String,
    pub model: String,
    pub model_source: String,
    pub source: i64,
    pub source_chars: usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub turn: i64,
    pub ts: i64,
    pub name: String,
    pub kind: String,
    pub input: String,
    pub output: String,
    pub input_chars: usize,
    pub output_chars: usize,
    pub output_bytes: Option<usize>,
    pub ok: Option<bool>,
    pub identity: String,
    pub text: String,
    pub raw: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Window {
    #[serde(skip)]
    pub target: String,
    pub session: String,
    pub agent: String,
    pub project: String,
    pub title: String,
    pub family: String,
    pub side: bool,
    pub center: i64,
    pub radius: Option<i64>,
    pub latest: bool,
    pub first: i64,
    pub last: i64,
    pub rows: Vec<Row>,
    pub events: Vec<Event>,
    pub notes: Vec<Value>,
    pub claim: Option<Claim>,
}
#[derive(Clone, Debug)]
pub struct Request {
    pub target: String,
    pub turn: Option<i64>,
    pub radius: Option<i64>,
    pub exact: bool,
}
impl Request {
    pub fn new(target: &str, turn: Option<i64>, radius: Option<i64>) -> Self {
        Self {
            target: target.into(),
            turn,
            radius,
            exact: handles::is_handle(target) || (target.starts_with('@') && target.contains(':')),
        }
    }
}
/// A result handle that names no turn to serve, with 0.4.9's machine code for why
/// (py/recall.py:1870-1937): `result-handle-unresolved`, `stale-result-handle` or
/// `content-ambiguous`.
#[derive(Debug)]
pub struct HandleError {
    pub code: &'static str,
    message: String,
}
impl std::fmt::Display for HandleError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for HandleError {}
pub fn handle_error(code: &'static str, message: String) -> anyhow::Error {
    HandleError { code, message }.into()
}

#[cfg(test)]
#[path = "tests.rs"]
mod checks;
