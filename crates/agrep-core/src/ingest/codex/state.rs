//! The resume cursor: parser state carried from one parse of a rollout to the next.

use std::collections::BTreeMap;

use super::review::Review;
use super::subagent::SubagentContext;
use super::submissions::Submissions;
use super::voice::Transcript;
use crate::model::{Event, RawMessage};

/// Serialized between incremental parses. While a parse runs it holds every row and event
/// that parse produced; `checkpoint` trims it to what resuming after the last line needs.
#[derive(Default, serde::Serialize, serde::Deserialize)]
pub(super) struct CodexState {
    pub(super) out: Vec<RawMessage>,
    pub(super) recap_turns: Vec<u32>,
    pub(super) events: Vec<Event>,
    pub(super) order: crate::ingest::ReplyOrder,
    /// call_id -> index into `events`, so the *_output line can pair up.
    pub(super) pending: BTreeMap<String, usize>,
    pub(super) turn: u32,
    pub(super) project: Option<String>,
    pub(super) session: Option<String>,
    pub(super) first_meta_seen: bool,
    pub(super) subagent: Option<SubagentContext>,
    /// For a guardian's review of another chat's actions, that chat's id ("" if unnamed).
    pub(super) guardian: Option<String>,
    /// The active model, updated by each turn_context line and stamped onto turns.
    pub(super) current_model: String,
    pub(super) submissions: Submissions,
    /// A review this chat started whose result has not arrived.
    #[serde(default)]
    pub(super) review: Option<Review>,
    /// The voice session under way, until its flush.
    #[serde(default)]
    pub(super) transcript: Transcript,
    /// Record ordinal of the parse's first line.
    pub(super) ordinal: usize,
    /// Whether this parse opened a subagent boundary, discarding every earlier row
    /// including the resumed ones.
    pub(super) reset: bool,
}

/// Everything one finished parse produced.
pub(super) struct Finished {
    pub(super) out: Vec<RawMessage>,
    pub(super) recap_turns: Vec<u32>,
    pub(super) events: Vec<Event>,
}

impl CodexState {
    /// Split a finished parse of `lines` lines into its output and the cursor that resumes
    /// after it: the last row and the still-unpaired calls.
    pub(super) fn checkpoint(self, lines: usize) -> (Self, Finished) {
        let Self {
            out,
            recap_turns,
            events,
            order,
            pending,
            turn,
            project,
            session,
            first_meta_seen,
            subagent,
            guardian,
            current_model,
            submissions,
            review,
            transcript,
            ordinal,
            reset,
        } = self;
        let mut keep: Vec<_> = pending.values().copied().collect();
        keep.sort_unstable();
        keep.dedup();
        let next = Self {
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
            guardian,
            current_model,
            submissions,
            review,
            transcript,
            ordinal: ordinal + lines,
            reset,
        };
        (
            next,
            Finished {
                out,
                recap_turns,
                events,
            },
        )
    }
}
