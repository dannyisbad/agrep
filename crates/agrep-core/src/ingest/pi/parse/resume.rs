//! The checkpoint an appended session resumes from, and the incremental entry
//! point that continues a parse from it.

use std::collections::BTreeMap;
use std::path::Path;

use super::super::discovery::side_parent;
use super::super::record::PreparedRecord;
use super::super::Parsed;
use super::{parse_data, Input, Origin, Parser};
use crate::model::{Event, Message, RawMessage};

#[derive(Default, serde::Serialize, serde::Deserialize)]
pub(super) struct PiState {
    pub(super) out: Vec<RawMessage>,
    pub(super) events: Vec<Event>,
    pub(super) order: crate::ingest::ReplyOrder,
    pub(super) pending: BTreeMap<String, usize>,
    pub(super) recap_turns: Vec<u32>,
    pub(super) turn: u32,
    pub(super) model: String,
    pub(super) mirrored_anchor: bool,
    pub(super) session: String,
    pub(super) project: String,
    pub(super) parent: String,
    pub(super) side: bool,
    pub(super) ordinal: usize,
}

/// Continue `previous` over `values`, the records an incremental read prepared
/// from a file now read through line `next_ordinal`.
pub(crate) fn resume_prepared(
    path: &Path,
    values: &[PreparedRecord],
    next_ordinal: usize,
    previous: Option<&str>,
) -> anyhow::Result<(
    Vec<Message>,
    Vec<Event>,
    crate::intake_batch::ReadOutcome,
    String,
)> {
    let state = previous.map(serde_json::from_str).transpose()?;
    let origin = Origin {
        agent: "pi",
        path,
        parent: side_parent(path),
    };
    let input = Input::Prepared {
        records: values,
        next_ordinal,
    };
    let ((m, e, o), next) = parse_data(origin, crate::intake::tally(), input, state);
    Ok((m, e, o, serde_json::to_string(&next)?))
}

impl Parser<'_> {
    /// Close the pass: settle reply order, checkpoint what the next append
    /// needs (the last row, still-pending calls) and freeze the rows.
    pub(super) fn finish(mut self, next_ordinal: usize) -> (Parsed, PiState) {
        self.order.finish(&mut self.events);
        let mut keep: Vec<_> = self.pending.values().copied().collect();
        keep.sort_unstable();
        keep.dedup();
        let next = PiState {
            out: self.out.last().cloned().into_iter().collect(),
            events: keep.iter().map(|i| self.events[*i].clone()).collect(),
            order: self.order.checkpoint(&self.events, &keep),
            pending: self
                .pending
                .into_iter()
                .map(|(id, i)| (id, keep.binary_search(&i).unwrap()))
                .collect(),
            recap_turns: self
                .out
                .last()
                .filter(|m| self.recap_turns.contains(&m.turn))
                .map(|m| vec![m.turn])
                .unwrap_or_default(),
            turn: self.turn,
            model: self.model,
            mirrored_anchor: self.mirrored_anchor,
            session: self.session,
            project: self.project,
            parent: self.parent,
            side: self.side,
            ordinal: next_ordinal,
        };
        let messages = freeze_rows(self.out, &self.recap_turns);
        (
            (
                messages,
                self.events,
                crate::intake_batch::ReadOutcome::Complete,
            ),
            next,
        )
    }
}

/// Compaction summaries freeze as recap rows.
fn freeze_rows(out: Vec<RawMessage>, recap_turns: &[u32]) -> Vec<Message> {
    out.into_iter()
        .map(|raw| {
            let turn = raw.turn;
            let mut m = raw.freeze();
            if recap_turns.contains(&turn) {
                m.who = "recap".into();
                m.model_source = "recap".into();
            }
            m
        })
        .collect()
}
