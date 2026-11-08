//! A message's reply text: blocks appended under the 0.4.2 cap, and which of a turn's calls no
//! reply followed.

use super::is_work_event;

/// Cap on indexed reply text; original lengths remain available for truncation diagnostics.
pub const REPLY_CAP: usize = 64_000;

/// Characters of a capped reply's latest block kept beside the text, so its ending survives.
pub const REPLY_TAIL_CAP: usize = 8_000;

/// The `meta` token the calls recorded after a turn's last reply block carry.
pub const AFTER_REPLY_META: &str = "after_reply=1";

/// Append reply block `t` to the message's reply exactly as 0.4.2 did (blocks joined with one
/// space, `REPLY_CAP` characters kept, `…` when a block was cut), recording where the block
/// joined and, past the cap, the block's tail in `reply_shape`. Adds the source characters
/// this block represents (its length plus one join) to `reply_chars` and returns them. The
/// held length is tracked, so a block past the cap costs its own length, not a rescan.
pub fn append_reply(raw: &mut crate::model::RawMessage, t: &str) -> usize {
    let t = t.trim();
    if t.is_empty() {
        return 0;
    }
    let block_chars = t.chars().count();
    let shape = &mut raw.reply_shape;
    let held = shape.head_chars as usize;
    let source_chars = block_chars + usize::from(held > 0);
    raw.reply_chars += source_chars;
    if held >= REPLY_CAP {
        // the tail block replaces the previous one past the cap; its join entry accumulates
        shape.joins = (shape.breaks.len() + 1) as u32;
        keep_end(shape, t, block_chars);
        return source_chars;
    }
    if held > 0 {
        raw.reply.push(' ');
        shape.breaks.push(held as u32);
        shape.joins = shape.breaks.len() as u32;
    }
    raw.reply.push_str(t);
    let joined = held + usize::from(held > 0) + block_chars;
    if joined <= REPLY_CAP {
        shape.head_chars = joined as u32;
        return source_chars;
    }
    let end = raw
        .reply
        .char_indices()
        .nth(REPLY_CAP)
        .map_or(raw.reply.len(), |(index, _)| index);
    raw.reply.truncate(end);
    raw.reply.push('…');
    shape.head_chars = (REPLY_CAP + 1) as u32;
    keep_end(shape, t, block_chars);
    source_chars
}

fn keep_end(shape: &mut crate::model::ReplyShape, t: &str, block_chars: usize) {
    shape.end_cut = block_chars > REPLY_TAIL_CAP;
    let start = if shape.end_cut {
        t.char_indices()
            .nth(block_chars - REPLY_TAIL_CAP)
            .map_or(0, |(index, _)| index)
    } else {
        0
    };
    shape.end.clear();
    shape.end.push_str(&t[start..]);
}

/// Which of a turn's calls no reply text followed. Adapters mark each human prompt, each reply
/// block they append, and the end of the file; the calls recorded after the turn's last reply
/// block (every call of a turn that wrote none) gain `after_reply=1` in `meta`, so a turn cut
/// off mid-work can be told from one that answered after its tools. Compaction recaps are not
/// prompts: the reply they carry continues the turn before them.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct ReplyOrder {
    turn_start: usize,
    prior_work: bool,
    text_at: usize,
    /// Calls the user ran (pi's `!cmd`), never the agent's work: left out of the fact.
    excluded: Vec<usize>,
}

impl Default for ReplyOrder {
    fn default() -> Self {
        Self::new()
    }
}

impl ReplyOrder {
    pub fn new() -> Self {
        Self {
            text_at: 0,
            turn_start: 0,
            prior_work: false,
            excluded: Vec::new(),
        }
    }

    /// A human prompt opens a turn; the previous turn's trailing calls are settled.
    pub fn prompt(&mut self, events: &mut [crate::model::Event]) {
        self.settle(events);
        self.text_at = events.len();
        self.turn_start = events.len();
        self.prior_work = false;
    }

    /// A reply block was appended after `recorded` events: every call so far was answered, and
    /// the block's join (when the block made one) records whether work came between it and
    /// the block before it.
    pub fn reply(&mut self, recorded: usize) {
        self.text_at = recorded;
        self.prior_work = false;
    }

    /// Whether the agent made a work call (a tool or a delegation) between its previous reply
    /// block and the block `append_reply` just added (the events before index `recorded`), kept
    /// for that join: the shape's last break, the tail block past the cap (whose entry covers
    /// every block the tail replaced), or for a first block the lead join.
    pub fn joined(
        &self,
        shape: &mut crate::model::ReplyShape,
        events: &[crate::model::Event],
        recorded: usize,
    ) {
        let worked = self.prior_work
            || events[..recorded.min(events.len())]
                .iter()
                .enumerate()
                .skip(self.text_at)
                .any(|(index, event)| is_work_event(event) && !self.excluded.contains(&index));
        Self::joined_known(shape, worked);
    }

    /// The same fact from an adapter whose store orders prose and calls itself: `worked` is
    /// whether a work call came between the previous block and the one just appended.
    pub fn joined_known(shape: &mut crate::model::ReplyShape, worked: bool) {
        let flag = if worked {
            crate::model::WORKED_YES
        } else {
            crate::model::WORKED_NO
        };
        let joins = shape.joins as usize;
        if joins == 0 {
            shape.lead = flag;
        } else if shape.worked.len() < joins {
            shape.worked.push(flag);
        } else {
            let tail = shape.worked.len() - 1;
            shape.worked[tail] = shape.worked[tail].max(flag);
        }
    }

    /// The events so far were discarded (a replayed parent transcript): nothing to settle.
    pub fn reset(&mut self, events: &[crate::model::Event]) {
        self.text_at = events.len();
        self.turn_start = events.len();
        self.prior_work = false;
        self.excluded.clear();
    }

    /// The event at `index` was run by the user, not the agent: it never trails a turn.
    pub fn exclude(&mut self, index: usize) {
        self.excluded.push(index);
    }

    /// Keep only unresolved calls while carrying whether completed work preceded the next reply.
    pub fn checkpoint(&self, events: &[crate::model::Event], keep: &[usize]) -> Self {
        Self {
            turn_start: keep.iter().filter(|i| **i < self.turn_start).count(),
            text_at: keep.iter().filter(|i| **i < self.text_at).count(),
            prior_work: self.prior_work
                || events
                    .iter()
                    .enumerate()
                    .skip(self.text_at)
                    .any(|(i, e)| is_work_event(e) && !self.excluded.contains(&i)),
            excluded: keep
                .iter()
                .enumerate()
                .filter_map(|(n, i)| self.excluded.contains(i).then_some(n))
                .collect(),
        }
    }

    /// The file ended: the last turn's trailing calls are settled.
    pub fn finish(&self, events: &mut [crate::model::Event]) {
        self.settle(events);
    }

    fn settle(&self, events: &mut [crate::model::Event]) {
        for (index, event) in events.iter_mut().enumerate().skip(self.turn_start) {
            event.meta = event
                .meta
                .split_whitespace()
                .filter(|s| *s != AFTER_REPLY_META)
                .collect::<Vec<_>>()
                .join(" ");
            if index < self.text_at {
                continue;
            }
            if self.excluded.contains(&index) {
                continue;
            }
            if !event.meta.is_empty() {
                event.meta.push(' ');
            }
            event.meta.push_str(AFTER_REPLY_META);
        }
    }
}
