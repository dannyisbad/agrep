//! One pass over a session's records: decode the input, walk the active branch,
//! and turn each record on it into rows and tool events.

mod message;
mod resume;

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use std::sync::Arc;

use serde_json::Value;

use super::branch::{active_branch, tool_results, Record, ToolResult};
use super::discovery::slug_project;
use super::projection::projected_record;
use super::record::{compact_record, PreparedRecord};
use super::Parsed;
use crate::ingest::parse_timestamp;
use crate::intake::{Skip, Tally};
use crate::model::{Event, RawMessage};

pub(crate) use resume::resume_prepared;
use resume::PiState;

/// Which file is being read, for whom, and the sidechat root it hangs off.
pub(super) struct Origin<'a> {
    pub(super) agent: &'static str,
    pub(super) path: &'a Path,
    pub(super) parent: Option<String>,
}

enum Input<'a> {
    Text(&'a str),
    /// Records an incremental read prepared, and the file line the next read
    /// starts at: only the reader saw the blank lines between records.
    Prepared {
        records: &'a [PreparedRecord],
        next_ordinal: usize,
    },
}

/// A full read: one pass over a whole file's text.
pub(super) fn parse_text(origin: Origin<'_>, tally: Arc<Tally>, data: &str) -> Parsed {
    parse_data(origin, tally, Input::Text(data), None).0
}

fn parse_data(
    origin: Origin<'_>,
    tally: Arc<Tally>,
    data: Input<'_>,
    previous: Option<PiState>,
) -> (Parsed, PiState) {
    let resumed = previous.is_some();
    let mut state = previous.unwrap_or_default();
    if !resumed {
        state.side = origin.parent.is_some();
        state.parent = origin.parent.unwrap_or_default();
    }
    let base = state.ordinal;
    let owned;
    let mut offsets = Vec::new();
    let (values, next_ordinal) = match data {
        Input::Prepared {
            records,
            next_ordinal,
        } => (prepared_values(records, &tally), next_ordinal),
        Input::Text(data) => {
            owned = text_values(data, base, &tally, &mut offsets);
            (owned.iter().collect(), base + data.lines().count())
        }
    };
    let records = number_records(&values, &offsets, base);
    identify(&mut state, &records, origin.path, resumed);
    let branch = active_branch(&records);
    let results = tool_results(&records, &branch);
    count_abandoned(records.len(), &branch, &tally);
    let mut parser = Parser::new(origin.agent, &tally, &results, state);
    for &index in &branch {
        parser.record(&records[index]);
    }
    parser.finish(next_ordinal)
}

/// Readable prepared records; malformed ones are counted and sampled here.
fn prepared_values<'v>(input: &'v [PreparedRecord], tally: &Tally) -> Vec<&'v Value> {
    let mut values = Vec::new();
    for record in input {
        match record {
            PreparedRecord::Value(value) => values.push(value),
            PreparedRecord::Malformed { error, raw } => {
                // The adapter counts malformed records and retains readable ones.
                tally.seen();
                match raw.sample() {
                    Ok(sample) => tally.error(&format!("{error}: {sample}")),
                    Err(error) => tally.error(&format!("cannot read malformed record: {error}")),
                }
            }
        }
    }
    values
}

/// Compact records decoded from each non-blank line, with each one's ordinal
/// pushed to `offsets`; unparsable lines are counted and sampled.
fn text_values(data: &str, base: usize, tally: &Tally, offsets: &mut Vec<usize>) -> Vec<Value> {
    let mut values = Vec::new();
    for (offset, line) in data.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        match projected_record(line) {
            Ok(value) => {
                let value = compact_record(value);
                offsets.push(base + offset);
                values.push(value);
            }
            Err(error) => {
                tally.seen();
                tally.error(&format!("{error}: {}", crate::intake::clip(line, 80)));
            }
        }
    }
    values
}

/// Each record with its file ordinal: the one an incremental read stamped, else
/// its line's, else its position after `base`.
fn number_records<'v>(values: &[&'v Value], offsets: &[usize], base: usize) -> Vec<Record<'v>> {
    values
        .iter()
        .copied()
        .enumerate()
        .map(|(offset, value)| Record {
            value,
            ordinal: value
                .get("__agrep_ordinal")
                .and_then(|v| v.as_u64())
                .map_or_else(
                    || offsets.get(offset).copied().unwrap_or(base + offset),
                    |v| v as usize,
                ),
        })
        .collect()
}

/// Session id and project from the header; without one, a resumed pass keeps
/// what it had and a fresh one falls back to the file stem and cwd slug.
fn identify(state: &mut PiState, records: &[Record<'_>], path: &Path, resumed: bool) {
    let header = records
        .iter()
        .find(|record| record.value.get("type").and_then(|t| t.as_str()) == Some("session"))
        .map(|record| record.value);
    if let Some(id) = header.and_then(|header| header.get("id").and_then(|i| i.as_str())) {
        state.session = id.to_string();
    } else if !resumed {
        state.session = path
            .file_stem()
            .map(|stem| stem.to_string_lossy().to_string())
            .unwrap_or_default();
    }
    if let Some(cwd) = header
        .and_then(|header| header.get("cwd").and_then(|c| c.as_str()))
        .filter(|cwd| !cwd.trim().is_empty())
    {
        state.project = cwd.to_string();
    } else if !resumed {
        state.project = slug_project(path);
    }
}

/// Abandoned branches are still records in the file, so they are counted and
/// skipped rather than ignored: audit's raw census is a non-blank line count,
/// and it only certifies this store while `seen` accounts for every line.
fn count_abandoned(records: usize, branch: &[usize], tally: &Tally) {
    let on_branch: HashSet<usize> = branch.iter().copied().collect();
    for index in 0..records {
        if !on_branch.contains(&index) {
            tally.seen();
            tally.skip(Skip::Unreferenced);
        }
    }
}

/// The pass over the active branch: the checkpoint it continues from plus the
/// tool results found on the branch.
struct Parser<'p> {
    agent: &'static str,
    tally: &'p Tally,
    results: &'p HashMap<String, ToolResult>,
    out: Vec<RawMessage>,
    events: Vec<Event>,
    order: crate::ingest::ReplyOrder,
    pending: BTreeMap<String, usize>,
    recap_turns: Vec<u32>,
    turn: u32,
    model: String,
    mirrored_anchor: bool,
    session: String,
    project: String,
    parent: String,
    side: bool,
}

impl<'p> Parser<'p> {
    fn new(
        agent: &'static str,
        tally: &'p Tally,
        results: &'p HashMap<String, ToolResult>,
        state: PiState,
    ) -> Self {
        let PiState {
            out,
            events,
            order,
            pending,
            recap_turns,
            turn,
            model,
            mirrored_anchor,
            session,
            project,
            parent,
            side,
            ordinal: _,
        } = state;
        let mut parser = Self {
            agent,
            tally,
            results,
            out,
            events,
            order,
            pending,
            recap_turns,
            turn,
            model,
            mirrored_anchor,
            session,
            project,
            parent,
            side,
        };
        parser.answer_pending();
        parser
    }

    /// Calls the previous pass left waiting get the results this pass found.
    fn answer_pending(&mut self) {
        for (id, result) in self.results {
            if let Some(i) = self.pending.remove(id) {
                let e = &mut self.events[i];
                e.output = result.output.clone();
                e.ok = result.ok;
                e.output_chars = result.chars;
                e.output_bytes = result.bytes;
            }
        }
    }

    fn record(&mut self, record: &Record<'_>) {
        self.tally.seen();
        match record.value.get("type").and_then(|t| t.as_str()) {
            Some("session") => self.tally.skip(Skip::Meta),
            Some("model_change") => self.model_change(record.value),
            Some("compaction") => self.compaction(record.value),
            Some("message") => self.message(record),
            _ => self.tally.skip(Skip::Meta),
        }
    }

    /// Applies forward until the next switch, like the store replays it.
    /// pi writes modelId; the oh-my-pi fork writes model.
    fn model_change(&mut self, record: &Value) {
        if let Some(named) = record
            .get("modelId")
            .or_else(|| record.get("model"))
            .and_then(|m| m.as_str())
        {
            self.model = named.to_string();
        }
        self.tally.skip(Skip::Meta);
    }

    fn compaction(&mut self, record: &Value) {
        let ts = parse_timestamp::rfc3339(record.get("timestamp").and_then(|t| t.as_str()));
        let summary = record
            .get("summary")
            .and_then(|s| s.as_str())
            .unwrap_or_default();
        if summary.trim().is_empty() {
            self.tally.skip(Skip::EmptyText);
            return;
        }
        self.tally.row();
        self.recap_turns.push(self.turn);
        self.push_row(ts, summary.to_string());
    }

    fn push_row(&mut self, ts: i64, text: String) {
        self.out.push(RawMessage {
            agent: self.agent,
            project: self.project.clone(),
            session: self.session.clone(),
            ts,
            turn: self.turn,
            text,
            model: self.model.clone(),
            reply: String::new(),
            reply_chars: 0,
            side: self.side,
            parent: self.parent.clone(),
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }
}
