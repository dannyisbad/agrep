//! Removals from the context map: `$rewindTo`, and re-syncs split into tail rollbacks, which
//! undo, and context rewrites, whose removed messages stay as dropped (see `Fold`).

use std::collections::HashSet;

use serde_json::Value;

use super::pair::{is_split, key_hash, split_key, CopyKey};
use super::{Fold, State};
use crate::ingest::gemini::content::{conversion_skips, is_notice};

impl Fold {
    /// `$rewindTo`: the message and every later one in the map; an unknown id clears the map.
    /// True when it was the person's `/rewind`, not the conversion's or a decline's rollback.
    pub(super) fn rewind(&mut self, target: &str, converting: bool) -> bool {
        let from = self
            .live_index(target)
            .and_then(|index| self.slot_of(index))
            .unwrap_or(0);
        let undone: Vec<usize> = self
            .order
            .get(from..)
            .into_iter()
            .flatten()
            .flatten()
            .copied()
            .collect();
        let kept = (converting && self.only_conversion_skips(&undone)) || self.declines(&undone);
        if undone.iter().any(|index| self.is_turn(*index)) {
            self.removal_epoch = Some(self.epoch);
        }
        if kept {
            for index in &undone {
                self.leave(*index);
            }
            let keys: Vec<Option<CopyKey>> =
                undone.iter().map(|index| self.copy_key(*index)).collect();
            self.mark_dropped(&undone, &keys);
        } else {
            let deleted = self.undo(undone);
            self.undo_answers(&deleted);
        }
        self.tidy();
        self.set_mark();
        !kept
    }

    /// Messages, at least one, that upstream's conversion leaves out of the rebuilt history: an
    /// env-led coalesced copy is one, and following its links would undo the prompt in it.
    fn only_conversion_skips(&self, indices: &[usize]) -> bool {
        !indices.is_empty()
            && indices.iter().all(|index| {
                self.entries
                    .get(*index)
                    .is_some_and(|entry| conversion_skips(&entry.message))
            })
    }

    /// A pure rollback that is a declined tool's, not a person's `/rewind` or an abort: with every
    /// declinable call cancelled, useGeminiStream records 'Request cancelled.' and then sets the
    /// history back to its length before the prompt (2105-2147 at fb972b2). Only an empty context
    /// makes that a rollback rather than a rewrite. So past notices it ends on the declined turn,
    /// removes only messages that entered the map since the previous removal, and leaves nothing
    /// but notices. When compression shortened the history in the same turn upstream skips it
    /// (2135), and the declined turn can later end a person's `/rewind`: the last two conditions
    /// tell that apart.
    fn declines(&self, removed: &[usize]) -> bool {
        let turns: Vec<usize> = removed
            .iter()
            .copied()
            .filter(|index| self.is_turn(*index))
            .collect();
        let since = |index: &usize| {
            let entered = self.entries.get(*index).map(|entry| entry.entered);
            self.removal_epoch
                .is_none_or(|epoch| entered.is_some_and(|entered| entered > epoch))
        };
        turns.last().is_some_and(|index| self.declined(*index))
            && turns.iter().all(since)
            && self.turns_in_map == turns.len()
    }

    /// Any message but a notice.
    fn is_turn(&self, index: usize) -> bool {
        self.entries
            .get(index)
            .is_some_and(|entry| !is_notice(&entry.message))
    }

    /// A model turn whose tool calls the person declined: every declinable one cancelled, or
    /// every one. `isTopicTool` (125-126): the person never gets to decline that one.
    fn declined(&self, index: usize) -> bool {
        let Some(message) = self.entries.get(index).map(|entry| &entry.message) else {
            return false;
        };
        let calls = match (message.get("type"), message.get("toolCalls")) {
            (Some(Value::String(kind)), Some(Value::Array(calls))) if kind == "gemini" => calls,
            _ => return false,
        };
        let cancelled =
            |call: &&Value| call.get("status").and_then(Value::as_str) == Some("cancelled");
        let topic = |call: &&Value| {
            let name = call.get("name").and_then(Value::as_str);
            matches!(name, Some("update_topic" | "Update Topic Context"))
        };
        let mut declinable = calls.iter().filter(|call| !topic(call)).peekable();
        let all_declinable = declinable.peek().is_some() && declinable.all(|call| cancelled(&call));
        all_declinable || (!calls.is_empty() && calls.iter().all(|call| cancelled(&call)))
    }

    /// Delete entries and, through each copy, the originals it re-recorded; returns those deleted.
    fn undo(&mut self, mut pending: Vec<usize>) -> Vec<usize> {
        let mut deleted = Vec::new();
        while let Some(index) = pending.pop() {
            let Some(entry) = self.entries.get_mut(index) else {
                continue;
            };
            if entry.state == State::Deleted {
                continue;
            }
            entry.state = State::Deleted;
            pending.extend_from_slice(&entry.originals);
            self.leave(index);
            deleted.push(index);
        }
        deleted
    }

    /// A person's `/rewind` undoes a prompt's whole exchange: also the model turns recorded after
    /// its row, before the next row, that a rewrite left out of the map, copied or not. A declined
    /// later round's (2105-2147 at fb972b2: the rollback ends at the last tool result sent) and an
    /// Esc'd tool's calls record (chatRecordingService.ts 1133-1157) are such turns. Copies there
    /// repeat other exchanges, so they stay; a copy of one of these turns is out of the map too, as
    /// the map keeps a prompt's copy before its answer's. The work stays linear: a row is deleted
    /// once, its scan stops at the next row, and as rows never appear later (see `Entry::row`) and
    /// pairing never clears a deleted one, no two scans cross the same entry.
    fn undo_answers(&mut self, deleted: &[usize]) {
        let mut answers = Vec::new();
        for row in deleted {
            if !self.entries.get(*row).is_some_and(|entry| entry.row) {
                continue;
            }
            for (index, entry) in self.entries.iter().enumerate().skip(row + 1) {
                if entry.row {
                    break;
                }
                let gemini = entry.message.get("type").and_then(Value::as_str) == Some("gemini");
                if gemini && entry.originals.is_empty() && entry.state == State::Dropped {
                    answers.push(index);
                }
            }
        }
        self.undo(answers);
    }

    /// Remove `removed` from the map and move `order` to its end, as a tail rollback or a
    /// context rewrite (see `Fold`).
    pub(super) fn resync(&mut self, removed: Vec<usize>, order: Vec<usize>, converting: bool) {
        let removed = self.in_map_order(removed);
        let gone: HashSet<usize> = removed.iter().copied().collect();
        let converted = converting && self.only_conversion_skips(&removed);
        let tail = if !removed.is_empty() && !converted {
            self.rolled_back_tail(&removed, &gone)
        } else {
            HashSet::new()
        };
        let copies = self.copies_after(&removed, &gone);
        for index in &removed {
            self.leave(*index);
        }
        self.move_to_end(order);
        self.tidy();
        if !removed.is_empty() {
            self.settle(removed, tail, copies);
        }
        self.set_mark();
    }

    /// The removed messages still in the map, in its order, once each.
    fn in_map_order(&self, removed: Vec<usize>) -> Vec<usize> {
        let mut removed: Vec<(usize, usize)> = removed
            .into_iter()
            .filter_map(|index| self.slot_of(index).map(|slot| (slot, index)))
            .collect();
        removed.sort_unstable();
        removed.dedup();
        removed.into_iter().map(|(_, index)| index).collect()
    }

    /// The removed messages the map ends with, walked back past what this re-sync recorded: its
    /// rolled-back tail, unless the removal is a declined tool's.
    fn rolled_back_tail(&mut self, removed: &[usize], gone: &HashSet<usize>) -> HashSet<usize> {
        let mut tail: HashSet<usize> = HashSet::new();
        let mut from = self.order.len();
        let mut gaps = false;
        while tail.len() < gone.len() {
            let Some(slot) = from.checked_sub(1) else {
                break;
            };
            match self.order.get(slot).copied().flatten() {
                None => gaps = true,
                Some(index) if self.entered_now(index) => {}
                Some(index) if gone.contains(&index) => {
                    tail.insert(index);
                }
                Some(_) => break,
            }
            from = slot;
        }
        // each gap is crossed once: one-in-one-out re-syncs would otherwise walk them all again
        if gaps {
            self.compact_from(from);
        }
        if self.declines(removed) {
            tail.clear();
        }
        tail
    }

    /// The copies a rewrite recorded since the previous re-sync after the last removed message,
    /// in map order.
    fn copies_after(&self, removed: &[usize], gone: &HashSet<usize>) -> Vec<usize> {
        let Some(last) = removed.last().and_then(|index| self.slot_of(*index)) else {
            return Vec::new();
        };
        let mut copies: Vec<(usize, usize)> = Vec::new();
        for index in self.mark..self.entries.len() {
            let Some(entry) = self.entries.get(index) else {
                continue;
            };
            // a notice is never a copy, also when an older recorder wrote it after this one
            // took the context over (auto-compression's, see `is_notice`)
            let copy =
                entry.originals.is_empty() && !gone.contains(&index) && !is_notice(&entry.message);
            if let Some(slot) = entry.slot.filter(|slot| *slot > last).filter(|_| copy) {
                copies.push((slot, index));
            }
        }
        copies.sort_unstable();
        copies.into_iter().map(|(_, index)| index).collect()
    }

    /// Move the listed messages still in the map to its end, in list order.
    fn move_to_end(&mut self, order: Vec<usize>) {
        let mut moved = HashSet::new();
        for index in order {
            if self.slot_of(index).is_some() && moved.insert(index) {
                self.leave(index);
                self.enter(index);
            }
        }
    }

    /// The removed messages, out of the map: the rolled-back tail is undone unless the map now
    /// ends on what this re-sync recorded; the rest are dropped, turns paired with the copies.
    fn settle(&mut self, removed: Vec<usize>, tail: HashSet<usize>, copies: Vec<usize>) {
        if removed.iter().any(|index| self.is_turn(*index)) {
            self.removal_epoch = Some(self.epoch);
        }
        let last = self.order.iter().rev().flatten().next().copied();
        let mut dropped = removed;
        if !tail.is_empty() && !last.is_some_and(|index| self.entered_now(index)) {
            dropped.retain(|index| !tail.contains(index));
            self.undo(tail.into_iter().collect());
        }
        // a copy never repeats a notice, and one between two user turns would split their run
        let (turns, notices): (Vec<usize>, Vec<usize>) =
            dropped.into_iter().partition(|index| self.is_turn(*index));
        let keys: Vec<Option<CopyKey>> = turns.iter().map(|index| self.copy_key(*index)).collect();
        let whole = self.before_mark == 0;
        self.pair(&turns, &keys, &copies, whole);
        self.mark_dropped(&turns, &keys);
        self.mark_dropped(&notices, &[]);
        self.tidy();
    }

    /// Mark messages already out of the map dropped, and pool them, by `keys` where given, for
    /// later whole rewrites: an Esc'd turn's two records pool as one under the copy's key too.
    fn mark_dropped(&mut self, dropped: &[usize], keys: &[Option<CopyKey>]) {
        for (position, index) in dropped.iter().enumerate() {
            if let Some(entry) = self.entries.get_mut(*index) {
                entry.state = State::Dropped;
                entry.partner = None;
            }
            let Some(Some(key)) = keys.get(position) else {
                continue;
            };
            self.dropped
                .entry(key_hash(key))
                .or_default()
                .insert(*index);
            let next = position + 1;
            if let (Some(Some(calls)), Some(partner)) = (keys.get(next), dropped.get(next)) {
                if is_split(key, calls) {
                    if let Some(entry) = self.entries.get_mut(*index) {
                        entry.partner = Some(*partner);
                    }
                    self.dropped
                        .entry(key_hash(&split_key(key, calls)))
                        .or_default()
                        .insert(*index);
                }
            }
        }
    }
}
