//! Resume: the serialized parser state an appended transcript continues from.

use std::collections::BTreeMap;
use std::path::Path;

use super::parse::parse_data;
use crate::model::{Event, Message};

/// A session's accumulating parse state; what survives a checkpoint is serialized between
/// appends, so the field set and order are the persisted format.
#[derive(Default, serde::Serialize, serde::Deserialize)]
pub(super) struct ClaudeState {
    pub(super) out: Vec<crate::model::RawMessage>,
    pub(super) recap_turns: Vec<u32>,
    pub(super) events: Vec<Event>,
    pub(super) order: crate::ingest::ReplyOrder,
    /// tool_use id -> index into `events`, so the later tool_result line can pair up.
    pub(super) pending: BTreeMap<String, usize>,
    pub(super) turn: u32,
    pub(super) first_cwd: String,
    pub(super) project_votes: BTreeMap<String, usize>,
    pub(super) side_parent: Option<String>,
    pub(super) ordinal: usize,
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

impl ClaudeState {
    /// The state the next append resumes from, after `lines` more lines: the last message and
    /// the calls still awaiting a result. Takes the pending map and session-wide fields.
    pub(super) fn checkpoint(&mut self, lines: usize) -> ClaudeState {
        let mut keep: Vec<_> = self.pending.values().copied().collect();
        keep.sort_unstable();
        keep.dedup();
        ClaudeState {
            out: self.out.last().cloned().into_iter().collect(),
            recap_turns: self
                .out
                .last()
                .filter(|m| self.recap_turns.contains(&m.turn))
                .map(|m| vec![m.turn])
                .unwrap_or_default(),
            events: keep.iter().map(|i| self.events[*i].clone()).collect(),
            order: self.order.checkpoint(&self.events, &keep),
            pending: std::mem::take(&mut self.pending)
                .into_iter()
                .map(|(id, i)| (id, keep.binary_search(&i).unwrap()))
                .collect(),
            turn: self.turn,
            first_cwd: std::mem::take(&mut self.first_cwd),
            project_votes: std::mem::take(&mut self.project_votes),
            side_parent: self.side_parent.take(),
            ordinal: self.ordinal + lines,
        }
    }

    /// The parsed messages, recap turns marked, and the events.
    pub(super) fn into_output(self) -> (Vec<Message>, Vec<Event>) {
        let recap_turns = self.recap_turns;
        let messages = self
            .out
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
        (messages, self.events)
    }
}
