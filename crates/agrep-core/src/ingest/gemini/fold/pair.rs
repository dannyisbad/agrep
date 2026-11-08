//! Pairing the copies a context rewrite records with the messages they repeat.

use std::collections::HashMap;
use std::hash::{Hash, Hasher};

use serde_json::Value;

use super::{Fold, State};
use crate::ingest::gemini::content::{
    conversion_skips, history_kind, is_injected_context, parts_of, parts_text, past_injected,
    visible_text, INTERRUPTED_PLACEHOLDER,
};

/// What a re-recorded copy shares with the message it repeats: type, visible text and the
/// (name, call id) of every tool call or result it carries.
pub(super) type CopyKey = (String, String, Vec<(String, String)>);

/// One part of a turn as a coalesced copy repeats it: whether the turn is the model's, the part's
/// text and its tool call or result.
type PartKey = (bool, String, Option<(String, String)>);

/// Whether `text` and `calls`, consecutive model records, are the two an Esc'd turn leaves: the
/// UI records the cancellation notice between the turn's text and its cancelled call, so
/// `recordToolCalls` starts a record of its own for the call (chatRecordingService.ts 1133-1157
/// at fb972b2). A copy of the turn carries the text and the call as one message.
pub(super) fn is_split(text: &CopyKey, calls: &CopyKey) -> bool {
    text.0 == "gemini"
        && calls.0 == "gemini"
        && !text.1.is_empty()
        && text.2.is_empty()
        && calls.1.is_empty()
        && !calls.2.is_empty()
}

/// The key a copy of the Esc'd turn carries: the text record's text with the calls record's calls.
pub(super) fn split_key(text: &CopyKey, calls: &CopyKey) -> CopyKey {
    (text.0.clone(), text.1.clone(), calls.2.clone())
}

pub(super) fn key_hash(key: &CopyKey) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    key.hash(&mut hasher);
    hasher.finish()
}

/// A message's parts as a coalesced copy repeats them, in history order. A model turn's record
/// keeps its cleaned reply as text and its calls in `toolCalls` while a copy keeps the turn's parts,
/// so both read as visible text (see `visible_text`) and calls; coalescing strips thoughts first.
fn part_keys(message: &Value) -> Vec<PartKey> {
    let Some(content) = message.get("content") else {
        return Vec::new();
    };
    let text = |part: &Value| parts_text(std::slice::from_ref(part));
    match history_kind(message) {
        "user" => parts_of(content)
            .iter()
            .map(|part| (false, text(part), part_tool(part)))
            .collect(),
        "gemini" => {
            let mut keys = Vec::new();
            for part in parts_of(content) {
                if let Some(call) = part.get("functionCall") {
                    keys.push((true, String::new(), Some(name_and_id(call))));
                    continue;
                }
                let reply = visible_text(&text(part));
                if !reply.is_empty() {
                    keys.push((true, reply, None));
                }
            }
            if !keys.iter().any(|(_, _, call)| call.is_some()) {
                let calls = message.get("toolCalls").and_then(Value::as_array);
                for call in calls.into_iter().flatten() {
                    keys.push((true, String::new(), Some(name_and_id(call))));
                }
            }
            keys
        }
        _ => Vec::new(),
    }
}

/// The (name, call id) of a functionCall or functionResponse part.
fn part_tool(part: &Value) -> Option<(String, String)> {
    ["functionCall", "functionResponse"]
        .iter()
        .find_map(|kind| part.get(*kind))
        .map(name_and_id)
}

fn name_and_id(call: &Value) -> (String, String) {
    let field = |name: &str| {
        call.get(name)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    (field("name"), field("id"))
}

/// The copies of one `pair`: each one's key and parts.
struct Copies<'a> {
    keys: &'a [Option<CopyKey>],
    parts: &'a [Vec<PartKey>],
}

impl Copies<'_> {
    fn want(&self, slot: usize) -> &[PartKey] {
        self.parts.get(slot).map(Vec::as_slice).unwrap_or_default()
    }
}

/// The removed turns of one `pair`, keyed as a copy can repeat them: one whole, an Esc'd turn's
/// two records, or a coalesced run of them (by first and last part). Every part is keyed once
/// per `pair`, so a comparison never re-reads its text.
struct Removed<'a> {
    indices: &'a [usize],
    keys: &'a [Option<CopyKey>],
    parts: &'a [Vec<PartKey>],
    /// Messages upstream's conversion leaves out (see `conversion_skips`).
    stale: &'a [bool],
    at: HashMap<&'a CopyKey, Vec<usize>>,
    /// An Esc'd turn's text record followed by its calls record, keyed as the copy repeats them.
    splits: HashMap<CopyKey, Vec<usize>>,
    starts: HashMap<&'a PartKey, Vec<usize>>,
    ends: HashMap<&'a PartKey, Vec<usize>>,
}

impl<'a> Removed<'a> {
    fn new(
        indices: &'a [usize],
        keys: &'a [Option<CopyKey>],
        parts: &'a [Vec<PartKey>],
        stale: &'a [bool],
    ) -> Self {
        let mut at: HashMap<&CopyKey, Vec<usize>> = HashMap::new();
        for (position, key) in keys.iter().enumerate() {
            if let Some(key) = key {
                at.entry(key).or_default().push(position);
            }
        }
        let mut starts: HashMap<&PartKey, Vec<usize>> = HashMap::new();
        let mut ends: HashMap<&PartKey, Vec<usize>> = HashMap::new();
        for (position, parts) in parts.iter().enumerate() {
            if let (Some(first), Some(last)) = (parts.first(), parts.last()) {
                starts.entry(first).or_default().push(position);
                ends.entry(last).or_default().push(position);
            }
        }
        let mut splits: HashMap<CopyKey, Vec<usize>> = HashMap::new();
        for (position, pair) in keys.windows(2).enumerate() {
            if let [Some(text), Some(calls)] = pair {
                if is_split(text, calls) {
                    splits
                        .entry(split_key(text, calls))
                        .or_default()
                        .push(position);
                }
            }
        }
        Self {
            indices,
            keys,
            parts,
            stale,
            at,
            splits,
            starts,
            ends,
        }
    }

    /// The nearest of the candidates `key` and `want` repeat in pairing order: one removed turn,
    /// an Esc'd turn's two records, or a coalesced run; the longest of those starting (ending)
    /// together.
    fn nearest_span(
        &self,
        key: &CopyKey,
        want: &[PartKey],
        bound: usize,
        front: bool,
    ) -> Option<(usize, usize)> {
        let nearest = |positions: &[usize], len: usize| -> Option<(usize, usize)> {
            let start = if front {
                *positions.get(positions.partition_point(|p| *p < bound))?
            } else {
                let below = positions.partition_point(|p| p + len <= bound);
                *positions.get(below.checked_sub(1)?)?
            };
            Some((start, start + len))
        };
        let found = nearest(self.at.get(key).map(Vec::as_slice).unwrap_or_default(), 1);
        let split = nearest(
            self.splits.get(key).map(Vec::as_slice).unwrap_or_default(),
            2,
        );
        let run = self.run(want, bound, front);
        let rank = |(start, end): &(usize, usize)| {
            let away = if front {
                *start
            } else {
                self.indices.len() - *end
            };
            (away, std::cmp::Reverse(end - start))
        };
        [found, split, run].into_iter().flatten().min_by_key(rank)
    }

    /// The run of two or more consecutive removed turns whose parts, one after another, are the
    /// copy's (`want`): the one starting at or after `bound` from the front, else the one ending
    /// before it, found by each turn's first (front) or last part. A `stale` message and a turn
    /// without parts to key (thought-only, like a binary read's acknowledgement) can sit inside
    /// the run without being in the copy.
    fn run(&self, want: &[PartKey], bound: usize, front: bool) -> Option<(usize, usize)> {
        if want.len() < 2 {
            return None;
        }
        let edges = if front { &self.starts } else { &self.ends };
        let positions = edges.get(if front { want.first() } else { want.last() }?)?;
        let below = positions.partition_point(|p| *p < bound);
        let start = *positions.get(if front { below } else { below.checked_sub(1)? })?;
        let mut scan = RunScan {
            want,
            removed: self,
            front,
            // comparisons stop at a few per part of the copy, so a crafted run of repeats stays
            // linear
            budget: 4 * want.len(),
        };
        scan.walk(start)
    }
}

/// One walk of `Removed::run` from its first candidate turn.
struct RunScan<'s, 'a> {
    want: &'s [PartKey],
    removed: &'s Removed<'a>,
    front: bool,
    budget: usize,
}

impl RunScan<'_, '_> {
    fn walk(&mut self, start: usize) -> Option<(usize, usize)> {
        let (mut next, mut edge, mut taken, mut members) = (Some(start), start, 0, 0);
        let mut fitted = start;
        while taken < self.want.len() {
            if let Some(len) = next.and_then(|at| self.fits(at, taken)) {
                edge = next?;
                fitted = edge;
                next = self.step(edge);
                taken += len;
            } else if let Some(len) = (members > 0).then(|| self.fits(fitted, taken)).flatten() {
                // `--resume` re-derives a tool result the file also holds, under the same id:
                // the history carries that message twice and upstream's map once
                taken += len;
            } else if members > 0 && next.is_some_and(|at| self.skippable(at)) {
                // the map can keep a stale one after a conversion when the file never re-synced
                // (the `$set.messages` recorder writes none when the count comes out the same);
                // `stripThoughts` drops a turn left without parts (geminiChat.ts 1841-1880)
                self.budget = self.budget.checked_sub(1)?;
                edge = next?;
                next = self.step(edge);
                continue;
            } else {
                return None;
            }
            members += 1;
        }
        if members < 2 {
            return None;
        }
        Some(if self.front {
            (start, edge + 1)
        } else {
            (edge, start + 1)
        })
    }

    /// How many parts the removed turn at `at` covers next, if it fits there.
    fn fits(&mut self, at: usize, taken: usize) -> Option<usize> {
        let parts = self
            .removed
            .parts
            .get(at)
            .filter(|parts| !parts.is_empty())?;
        let range = if self.front {
            taken..taken + parts.len()
        } else {
            self.want.len().checked_sub(taken + parts.len())?..self.want.len() - taken
        };
        let wanted = self.want.get(range)?;
        self.budget = self.budget.checked_sub(wanted.len())?;
        (wanted == parts.as_slice()).then_some(parts.len())
    }

    fn skippable(&self, at: usize) -> bool {
        self.removed.stale.get(at) == Some(&true)
            || self.removed.parts.get(at).is_some_and(Vec::is_empty)
    }

    fn step(&self, at: usize) -> Option<usize> {
        if self.front {
            at.checked_add(1)
        } else {
            at.checked_sub(1)
        }
    }
}

impl Fold {
    /// Pair copies with the removed messages they repeat, order-preserving. Compression copies a
    /// suffix after its snapshot, paired from the back. A rewrite that re-records the context from
    /// its first message (masking, truncation, `/chat resume`) pairs from the front, and when it
    /// re-records the whole context a copy of nothing removed may repeat an earlier dropped message.
    /// `getHistory()` coalesces consecutive turns of a role for Gemini 2 and 3 models, so one copy
    /// can repeat a run of them: user turns (the environment and the first prompt, a cancelled
    /// tool's result and the next prompt, IDE context and its prompt), and model turns where
    /// upstream's conversion dropped the user turn between them (see `conversion_skips`).
    pub(super) fn pair(
        &mut self,
        removed: &[usize],
        removed_keys: &[Option<CopyKey>],
        copies: &[usize],
        whole: bool,
    ) {
        if copies.is_empty() {
            return;
        }
        let copy_keys: Vec<Option<CopyKey>> = copies.iter().map(|i| self.copy_key(*i)).collect();
        let removed_parts = self.part_keys_of(removed);
        let copy_parts = self.part_keys_of(copies);
        let stale: Vec<bool> = removed
            .iter()
            .map(|index| {
                let message = self.entries.get(*index).map(|entry| &entry.message);
                message.is_some_and(conversion_skips)
            })
            .collect();
        let pool = Removed::new(removed, removed_keys, &removed_parts, &stale);
        let wanted = Copies {
            keys: &copy_keys,
            parts: &copy_parts,
        };
        let front = self.pairs_from_front(&pool, &wanted, whole);
        let originals = self.match_copies(&pool, &wanted, front, whole);
        for (copy, found) in copies.iter().zip(originals) {
            if let Some(entry) = self.entries.get_mut(*copy) {
                entry.row &= found.is_empty();
                entry.originals = found;
            }
        }
    }

    fn part_keys_of(&self, indices: &[usize]) -> Vec<Vec<PartKey>> {
        indices
            .iter()
            .map(|index| {
                let message = self.entries.get(*index).map(|entry| &entry.message);
                message.map(part_keys).unwrap_or_default()
            })
            .collect()
    }

    /// Whether the copies re-record the context from its first removed message on.
    fn pairs_from_front(&mut self, pool: &Removed, copies: &Copies, whole: bool) -> bool {
        let opens = matches!(
            (copies.keys.first(), pool.keys.first()),
            (Some(Some(copy)), Some(Some(first))) if copy == first
        ) || pool
            .run(copies.want(0), 0, true)
            .is_some_and(|(start, _)| start == 0);
        // a whole rewrite re-records the context from its start, but a save made after `/rewind`
        // or `--resume` lacks the environment message, so judge by the first copy past it
        let lead = copies.keys.iter().enumerate().find_map(|(slot, key)| {
            let context = |(kind, text, _): &CopyKey| kind == "user" && is_injected_context(text);
            key.as_ref()
                .filter(|key| !context(key))
                .map(|key| (key, slot))
        });
        opens
            || (whole
                && lead.is_some_and(|(key, slot)| {
                    pool.at.contains_key(key)
                        || pool.splits.contains_key(key)
                        || pool.run(copies.want(slot), 0, true).is_some()
                        || self.dropped_original(key, 0).is_some()
                }))
    }

    /// Each copy's originals, matched in pairing order; turns typed since the previous re-sync
    /// may sit among the copies of a partial rewrite.
    fn match_copies(
        &mut self,
        pool: &Removed,
        copies: &Copies,
        front: bool,
        whole: bool,
    ) -> Vec<Vec<usize>> {
        let mut originals: Vec<Vec<usize>> = vec![Vec::new(); copies.keys.len()];
        let mut order: Vec<usize> = (0..copies.keys.len()).collect();
        if !front {
            order.reverse();
        }
        let mut bound = if front { 0 } else { pool.indices.len() };
        let mut dropped_bound = 0;
        for slot in order {
            let Some(Some(key)) = copies.keys.get(slot) else {
                continue;
            };
            if let Some((start, end)) = pool.nearest_span(key, copies.want(slot), bound, front) {
                bound = if front { end } else { start };
                if let Some(cell) = originals.get_mut(slot) {
                    *cell = pool.indices.get(start..end).unwrap_or_default().to_vec();
                }
                continue;
            }
            if !(front && whole) {
                continue;
            }
            if let Some((found, partner)) = self.dropped_original(key, dropped_bound) {
                dropped_bound = found + 1;
                if let Some(original) = originals.get_mut(slot) {
                    original.push(found);
                    original.extend(partner);
                }
            }
        }
        originals
    }

    /// The oldest dropped entry at or after `bound` that `key` repeats, with the calls record
    /// split off it when the copy repeats the two (see `partner`), pruning entries that are no
    /// longer dropped, repeat a turn since undone through another copy of it, or no longer key
    /// to this pool (revived with other content; a split whose partner left): a slot is walked
    /// past unpruned only on a hash collision, so a scan never crosses a stale slot twice.
    fn dropped_original(&mut self, key: &CopyKey, bound: usize) -> Option<(usize, Option<usize>)> {
        let hash = key_hash(key);
        let mut cursor = bound;
        loop {
            let index = *self.dropped.get(&hash)?.range(cursor..).next()?;
            let usable = self.entries.get(index).is_some_and(|entry| {
                let deleted = |original: &usize| {
                    self.entries
                        .get(*original)
                        .is_none_or(|entry| entry.state == State::Deleted)
                };
                entry.state == State::Dropped && !entry.originals.iter().any(deleted)
            });
            let own = usable.then(|| self.copy_key(index)).flatten();
            if own.as_ref() == Some(key) {
                return Some((index, None));
            }
            let dropped = |index: usize| {
                self.entries
                    .get(index)
                    .is_some_and(|entry| entry.state == State::Dropped)
            };
            let partner = self.entries.get(index).and_then(|entry| entry.partner);
            let split = own
                .as_ref()
                .zip(partner.filter(|p| dropped(*p)))
                .and_then(|(text, p)| {
                    let calls = self.copy_key(p)?;
                    is_split(text, &calls).then(|| (split_key(text, &calls), p))
                });
            if let Some((split, partner)) = split.as_ref().filter(|(split, _)| split == key) {
                debug_assert_eq!(key_hash(split), hash);
                return Some((index, Some(*partner)));
            }
            let belongs = own.as_ref().is_some_and(|own| key_hash(own) == hash)
                || split.is_some_and(|(split, _)| key_hash(&split) == hash);
            if belongs {
                cursor = index + 1;
            } else if let Some(set) = self.dropped.get_mut(&hash) {
                set.remove(&index);
            }
        }
    }

    /// None for the interrupted-turn closer: the CLI writes it the first time inside a rewrite,
    /// where it would take the place of an earlier closer and misalign every pair after it. A
    /// coalesced user copy keys past its leading injected context (see `past_injected`).
    pub(super) fn copy_key(&self, index: usize) -> Option<CopyKey> {
        let message = &self.entries.get(index)?.message;
        let kind = history_kind(message);
        let mut parts = parts_of(message.get("content").unwrap_or(&Value::Null));
        let calls = message.get("toolCalls").and_then(Value::as_array);
        let mut tools: Vec<(String, String)> =
            calls.into_iter().flatten().map(name_and_id).collect();
        tools.extend(parts.iter().filter_map(part_tool));
        tools.sort_unstable();
        tools.dedup();
        if kind == "user" {
            parts = past_injected(parts);
        }
        let text = visible_text(&parts_text(parts));
        if tools.is_empty() && text == INTERRUPTED_PLACEHOLDER {
            return None;
        }
        Some((kind.to_string(), text, tools))
    }
}
