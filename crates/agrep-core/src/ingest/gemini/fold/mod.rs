//! Folding a JSONL record stream into the transcript a person read (see `Fold`).
#![deny(clippy::indexing_slicing)]

mod pair;
mod resync;

use std::collections::{BTreeSet, HashMap, HashSet};

use serde_json::Value;

use super::content::{absorb, is_history_copy, is_js_object, is_notice, js_truthy, opens_exchange};
use crate::intake::{Skip, Tally};

/// Where one recorded message stands.
#[derive(Clone, Copy, PartialEq, Eq)]
enum State {
    /// In upstream's context map.
    Live,
    /// Out of the context after a rewrite, still part of the conversation.
    Dropped,
    /// Undone: rewound, or rolled back after an aborted or failed request.
    Deleted,
}

struct Entry {
    message: Value,
    state: State,
    /// The entries this one re-records under a new id during a context rewrite: one, or the run
    /// of consecutive turns of one role upstream's `getHistory()` coalesced into it.
    originals: Vec<usize>,
    /// Position in `Fold::order` while in the context map.
    slot: Option<usize>,
    /// The `Fold::epoch` in which the entry entered the map.
    entered: u64,
    /// A model turn's visible reply, once `absorb` has read it: patches that write the same back
    /// can come by the hundred thousand.
    reply: Option<String>,
    /// Opens an exchange (see `opens_exchange`): fixed when recorded and cleared when paired as a
    /// copy, so the boundaries `undo_answers` scans between never appear later.
    row: bool,
    /// The calls record upstream split off this model turn's text on Esc (see `split_key`), once
    /// both left the map together: a copy of the turn repeats the two as one.
    partner: Option<usize>,
    /// The content is the history's parts, not a recorded reply (see `is_history_copy`); a patch
    /// that keeps the recorded reply among the parts leaves this false.
    copied: bool,
}

impl Entry {
    /// Take a record of this message under its id. A model turn recorded again with the history's
    /// raw parts (gemini-cli 0.45-0.52's `/chat resume` of a turn compression dropped) keeps the
    /// reply it saved when both read the same (see `absorb`).
    fn rerecord(&mut self, mut message: Value) {
        let raw = is_history_copy(&message)
            && self.message.get("type").and_then(Value::as_str) == Some("gemini");
        match (&mut self.message, &mut message) {
            (Value::Object(old), Value::Object(new)) if raw => {
                if let Some(content) = new.get("content").cloned() {
                    absorb(old, &mut self.reply, &mut self.copied, &content);
                }
                if let Some(kept) = old.get("content").cloned() {
                    new.insert("content".to_string(), kept);
                }
            }
            _ => {
                self.copied = is_history_copy(&message);
                self.reply = None;
            }
        }
        self.message = message;
    }
}

/// Upstream's `createJsonlRecordAccumulator` (full-load mode) mirrored as `order`, its context map
/// in insertion order with record kinds tested in its order, beside the transcript (`entries`, in
/// first-recorded order) that a person read. A re-sync (`$patch` `removeIds`/`orderIds`, or a
/// `$set.messages` checkpoint) that removes messages splits them:
/// - a tail rollback (aborted and failed requests): the removed messages that were the last ones
///   in the map before it, when nothing it records lands after the surviving ones once its order
///   applies. They are undone, with the originals of copies among them. `$rewindTo` is one too,
///   and only ever touches the map: dropped messages after its target stay.
/// - a context rewrite (compression, tool-output masking, truncation, `/chat resume`), the rest:
///   the removed messages stay, and the copies recorded since the previous re-sync after them pair
///   to them, or to messages dropped earlier, as replays. A re-sync never overwrites a recorded
///   tool result.
///
/// Two removals are neither, and their messages stay, as dropped: the re-sync right after a
/// `/rewind` or `--resume`, which removes only what upstream's conversion of the history skips
/// (see `converting`), and a declined tool's rollback (see `declines`).
///
/// Work stays linear in the file: each entry is keyed at most once as removed and once as a
/// copy, and the map is a slot list compacted as removals accumulate and wherever the rollback
/// walk crossed a gap, so no gap is walked twice.
/// Every non-blank line and every message inside a `messages` array is one seen unit, and each
/// leaves through exactly one counter below or as one transcript entry.
///
/// Limits, measured with the recorder fuzz driver (fixtures/gemini_flows/recorder: 29 of 1,642
/// flows over realistic, resume-heavy and Esc-heavy mixes differ from its oracle):
/// - a copy carries no id of what it repeats, only its content, so pairing is by content and
///   position: a rewrite repeating a turn byte-identical to another candidate in the same window
///   (two compressions whose snapshots read the same: 20; a prompt typed twice with the same
///   reply: 6) can pair with the other one or merge the two;
/// - coalesced, a compression's acknowledgement can join the reply before it in one model copy,
///   which then reads as that reply plus the canned text on the recap row (2);
/// - a decline after `/chat resume`, `--resume`, masking and `/rewind` can lose its prompt and
///   cancelled call (1).
#[derive(Default)]
pub(super) struct Fold {
    session_id: Option<Value>,
    project_hash: Option<Value>,
    entries: Vec<Entry>,
    /// Upstream's context map: entry indices in its insertion order, `None` where one left.
    order: Vec<Option<usize>>,
    in_map: usize,
    /// Map entries that are not notices (see `is_notice`).
    turns_in_map: usize,
    /// Each id's newest entry.
    by_id: HashMap<String, usize>,
    /// Bumped by every record that is not a message: entries entering the map in the current
    /// epoch were recorded by the re-sync that follows them.
    epoch: u64,
    /// `entries.len()` at the previous re-sync; copies are recorded after it.
    mark: usize,
    /// Map entries other than notices recorded before `mark`: none left after a re-sync means it
    /// re-recorded the whole context (`setHistory` with fresh ids: masking, truncation,
    /// `/chat resume`). A notice can outlive any re-sync (see `is_notice`).
    before_mark: usize,
    /// The epoch of the previous re-sync or `$rewindTo` that removed a message other than a notice
    /// (see `declines`). A re-sync that only adds, as the `$set.messages` recorder writes on
    /// compressing an empty context, leaves it, and the environment's stable id can re-enter after.
    removal_epoch: Option<u64>,
    /// Set by a `$rewindTo` or a `$set` of `sessionId` until the next record that is not a
    /// message. `/rewind` writes the first, then re-syncs to `convertSessionToClientHistory` of
    /// what is left (rewindCommand.tsx 47-60 at fb972b2); a recorder taking over the file for
    /// `--resume` or compression writes the second (chatRecordingService.ts 737), and `--resume`
    /// then re-syncs to that conversion of the file (useSessionResume.ts 92, 122). A re-sync then
    /// that removes only what the conversion skips is that one.
    converting: bool,
    /// Dropped entries by `CopyKey` hash, the pool a whole-context rewrite pairs from: a
    /// `/chat resume` of a save from before a compression copies messages already out of the
    /// context. Entries no longer dropped are pruned on use.
    dropped: HashMap<u64, BTreeSet<usize>>,
    seen: u64,
    meta: u64,
    non_message: u64,
    replay: u64,
    errors: u64,
    first_error: Option<String>,
}

/// Reading records.
impl Fold {
    pub(super) fn read(data: &str) -> Self {
        let mut fold = Self::default();
        for line in data.lines() {
            if line.trim().is_empty() {
                continue;
            }
            fold.seen += 1;
            match serde_json::from_str::<Value>(line) {
                Ok(record) => fold.apply(record),
                Err(error) => {
                    fold.errors += 1;
                    if fold.first_error.is_none() {
                        fold.first_error =
                            Some(format!("{error}: {}", crate::intake::clip(line, 80)));
                    }
                }
            }
        }
        fold
    }

    fn apply(&mut self, record: Value) {
        let converting = std::mem::take(&mut self.converting);
        if let Some(target) = record.get("$rewindTo").and_then(Value::as_str) {
            self.meta += 1;
            self.converting = self.rewind(target, converting);
        } else if let Some(patch) = record.get("$patch").filter(|patch| is_js_object(patch)) {
            self.meta += 1;
            self.patch(patch, converting);
        } else if let (Some(Value::String(id)), None) = (record.get("id"), record.get("$patch")) {
            let id = id.clone();
            self.put(id, record);
            // the conversion's re-sync first records the tool results it re-derives
            self.converting = converting;
            return;
        } else if let Some(set) = record.get("$set").filter(|set| is_js_object(set)) {
            self.meta += 1;
            if let Some(Value::Array(messages)) = set.get("messages") {
                self.checkpoint(messages, converting);
            }
            self.merge_metadata(set);
            self.converting = set.get("sessionId").is_some();
        } else if record.get("sessionId").is_some_and(Value::is_string)
            && record.get("projectHash").is_some_and(Value::is_string)
        {
            self.meta += 1;
            self.document(&record);
        } else {
            self.non_message += 1;
        }
        self.epoch += 1;
    }

    /// A metadata line, or a whole legacy document read as one: its messages are recorded in turn.
    fn document(&mut self, record: &Value) {
        self.merge_metadata(record);
        if let Some(Value::Array(messages)) = record.get("messages") {
            for message in messages {
                self.seen += 1;
                match (message.get("id"), message.get("$patch")) {
                    (Some(Value::String(id)), None) => self.put(id.clone(), message.clone()),
                    _ => self.non_message += 1,
                }
            }
        }
    }

    fn merge_metadata(&mut self, fields: &Value) {
        if let Some(session_id) = fields.get("sessionId") {
            self.session_id = Some(session_id.clone());
        }
        if let Some(project_hash) = fields.get("projectHash") {
            self.project_hash = Some(project_hash.clone());
        }
    }

    /// A message record: a rewrite of a message in the map replaces it in place; anything else
    /// enters the map at its end, a dropped message returning to it.
    fn put(&mut self, id: String, message: Value) {
        let known = self.by_id.get(&id).copied().filter(|index| {
            self.entries
                .get(*index)
                .is_some_and(|entry| entry.state != State::Deleted)
        });
        match known {
            Some(index) => self.rerecord(index, message),
            None => self.insert(id, message),
        }
    }

    /// A known message recorded again: it returns to the map when dropped.
    fn rerecord(&mut self, index: usize, message: Value) {
        let epoch = self.epoch;
        let Some(entry) = self.entries.get_mut(index) else {
            return;
        };
        let was_turn = !is_notice(&entry.message);
        entry.rerecord(message);
        let turn = !is_notice(&entry.message);
        let revived = entry.state == State::Dropped;
        if revived {
            entry.state = State::Live;
            entry.entered = epoch;
            self.enter(index);
        } else if was_turn != turn {
            let before = usize::from(index < self.mark);
            if turn {
                self.turns_in_map += 1;
                self.before_mark += before;
            } else {
                self.turns_in_map = self.turns_in_map.saturating_sub(1);
                self.before_mark = self.before_mark.saturating_sub(before);
            }
        }
        self.replay += 1;
    }

    /// A new message, entering the map at its end.
    fn insert(&mut self, id: String, message: Value) {
        let index = self.entries.len();
        self.by_id.insert(id, index);
        let row = opens_exchange(&message);
        let copied = is_history_copy(&message);
        self.entries.push(Entry {
            message,
            state: State::Live,
            originals: Vec::new(),
            slot: None,
            entered: self.epoch,
            reply: None,
            partner: None,
            copied,
            row,
        });
        self.enter(index);
    }

    fn patch(&mut self, patch: &Value, converting: bool) {
        if patch.get("id").is_some_and(Value::is_string) {
            self.update(patch);
        }
        if let Some(Value::Array(updates)) = patch.get("updates") {
            for update in updates {
                if update.get("id").is_some_and(Value::is_string) {
                    self.update(update);
                }
            }
        }
        let ids = |field: &str| -> Vec<usize> {
            let listed = patch.get(field).and_then(Value::as_array);
            listed
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .filter_map(|id| self.live_index(id))
                .collect()
        };
        let removed = ids("removeIds");
        // upstream moves listed messages to the end in list order; unlisted ones keep theirs
        let order = ids("orderIds");
        self.resync(removed, order, converting);
    }

    /// A `$set.messages` history checkpoint (gemini-cli up to 361b0bb; `$patch` replaced it in
    /// d1cc08a): upstream rebuilds its map from the list, so unlisted messages leave it.
    fn checkpoint(&mut self, messages: &[Value], converting: bool) {
        let first_new = self.entries.len();
        let mut listed: Vec<usize> = Vec::new();
        for message in messages {
            self.seen += 1;
            let (Some(Value::String(id)), None) = (message.get("id"), message.get("$patch")) else {
                self.non_message += 1;
                continue;
            };
            if self.live_index(id).is_some_and(|index| index < first_new) {
                self.update(message);
                self.replay += 1;
            } else {
                self.put(id.clone(), message.clone());
            }
            listed.extend(self.live_index(id));
        }
        let kept: HashSet<usize> = listed.iter().copied().collect();
        let removed: Vec<usize> = self
            .order
            .iter()
            .flatten()
            .copied()
            .filter(|index| !kept.contains(index))
            .collect();
        self.resync(removed, listed, converting);
    }

    /// Upstream `applySinglePatch` on a message in the map: replace `content` (see `absorb`);
    /// give gemini tool calls a result by id, only where none was recorded.
    fn update(&mut self, patch: &Value) {
        let Some(index) = patch
            .get("id")
            .and_then(Value::as_str)
            .and_then(|id| self.live_index(id))
        else {
            return;
        };
        let Some(Entry {
            message: Value::Object(message),
            reply,
            copied,
            ..
        }) = self.entries.get_mut(index)
        else {
            return;
        };
        if let Some(content) = patch.get("content") {
            absorb(message, reply, copied, content);
        }
        let Some(Value::Array(calls)) = patch.get("toolCalls") else {
            return;
        };
        if message.get("type").and_then(Value::as_str) != Some("gemini") {
            return;
        }
        let Some(Value::Array(existing)) = message.get_mut("toolCalls") else {
            return;
        };
        for call in calls.iter().filter(|call| call.is_object()) {
            let id = call.get("id");
            let target = existing.iter_mut().find(|tc| tc.get("id") == id);
            if let (Some(result), Some(Value::Object(target))) = (call.get("result"), target) {
                if target.get("result").is_none_or(Value::is_null) {
                    target.insert("result".to_string(), result.clone());
                }
            }
        }
    }
}

/// The context map.
impl Fold {
    fn enter(&mut self, index: usize) {
        if let Some(entry) = self.entries.get_mut(index) {
            entry.slot = Some(self.order.len());
            self.order.push(Some(index));
            self.in_map += 1;
            if !is_notice(&entry.message) {
                self.turns_in_map += 1;
                if index < self.mark {
                    self.before_mark += 1;
                }
            }
        }
    }

    fn leave(&mut self, index: usize) {
        let Some(entry) = self.entries.get_mut(index) else {
            return;
        };
        let slot = entry.slot.take();
        let turn = !is_notice(&entry.message);
        if let Some(cell) = slot.and_then(|slot| self.order.get_mut(slot)) {
            *cell = None;
            self.in_map = self.in_map.saturating_sub(1);
            if turn {
                self.turns_in_map = self.turns_in_map.saturating_sub(1);
                if index < self.mark {
                    self.before_mark = self.before_mark.saturating_sub(1);
                }
            }
        }
    }

    /// Copies recorded from here on are later than every message now in the map.
    fn set_mark(&mut self) {
        self.mark = self.entries.len();
        self.before_mark = self.turns_in_map;
    }

    /// Drop trailing gaps, and rebuild `order` once gaps outnumber the messages in it.
    fn tidy(&mut self) {
        while matches!(self.order.last(), Some(None)) {
            self.order.pop();
        }
        if self.order.len() > 2 * self.in_map + 64 {
            self.compact_from(0);
        }
    }

    /// Close the gaps in `order` from `from` on, keeping its order.
    fn compact_from(&mut self, from: usize) {
        let moved: Vec<usize> = self.order.drain(from..).flatten().collect();
        for index in moved {
            if let Some(entry) = self.entries.get_mut(index) {
                entry.slot = Some(self.order.len());
                self.order.push(Some(index));
            }
        }
    }

    fn slot_of(&self, index: usize) -> Option<usize> {
        self.entries.get(index).and_then(|entry| entry.slot)
    }

    fn live_index(&self, id: &str) -> Option<usize> {
        self.by_id
            .get(id)
            .copied()
            .filter(|index| self.slot_of(*index).is_some())
    }

    fn entered_now(&self, index: usize) -> bool {
        self.entries
            .get(index)
            .is_some_and(|entry| entry.entered == self.epoch)
    }
}

/// The folded session.
impl Fold {
    /// Upstream `finalize` refuses a history without both ids and falls back to legacy JSON.
    pub(super) fn complete(&self) -> bool {
        self.session().is_some() && self.project_hash.as_ref().is_some_and(js_truthy)
    }

    pub(super) fn session(&self) -> Option<String> {
        self.session_id
            .as_ref()
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string)
    }

    pub(super) fn error_free(&self) -> bool {
        self.errors == 0
    }

    /// Whether the file undid any message it recorded.
    pub(super) fn undid_any(&self) -> bool {
        self.entries
            .iter()
            .any(|entry| entry.state == State::Deleted)
    }

    pub(super) fn record(&self, tally: &Tally) {
        let undone = self
            .entries
            .iter()
            .filter(|entry| entry.state == State::Deleted)
            .count();
        tally.seen_n(self.seen);
        tally.skip_n(Skip::Meta, self.meta);
        tally.skip_n(Skip::NonMessage, self.non_message);
        tally.skip_n(Skip::Replay, self.replay);
        tally.skip_n(Skip::Unreferenced, undone as u64);
        let sample = self.first_error.as_deref().unwrap_or("");
        for _ in 0..self.errors {
            tally.error(sample);
        }
    }

    /// The transcript in order, each message flagged when it only repeats another and when its
    /// content is a history copy's parts.
    pub(super) fn messages(&self) -> impl Iterator<Item = (&Value, bool, bool)> {
        self.entries
            .iter()
            .filter(|entry| entry.state != State::Deleted)
            .map(|entry| (&entry.message, !entry.originals.is_empty(), entry.copied))
    }
}
