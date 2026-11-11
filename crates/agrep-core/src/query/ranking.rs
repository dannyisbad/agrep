use super::*;
/// The speaker/source prior: 0.4.9's speaker weight times its source scale.
pub fn weight(who: &str) -> f64 {
    match who {
        "user" => 1.,
        "agent" => 0.8,
        "subagent" => 0.7225,
        "recap" => 0.325,
        "tool" => 0.22,
        "control" => 0.325,
        "synthetic" | "harness" => 0.3,
        _ => 0.5,
    }
}
/// 0.4.9's `_META_SCALE` (py/search.py:1222): every ranked tier scales a row whose
/// provenance is tooling about history rather than history by this factor.
pub(super) const META_SCALE: f64 = 0.45;
/// 0.4.9's `_meta_row` from stored provenance (py/search.py:1227-1238): machinery roles
/// and a subagent's first turn, which is its spawned task prompt.
pub fn meta_role(who: &str, turn: i64) -> bool {
    matches!(who, "harness" | "synthetic") || (who == "subagent" && turn == 0)
}
pub(super) fn meta_scale(meta: bool) -> f64 {
    if meta {
        META_SCALE
    } else {
        1.
    }
}
/// 0.4.9's speaker weight and source scale, which its score multiplies separately.
pub(super) fn speaker(who: &str) -> (f64, f64) {
    match who {
        "user" => (1., 1.),
        "agent" => (0.8, 1.),
        "subagent" => (0.85, 0.85),
        "tool" => (0.4, 0.55),
        "recap" | "control" => (0.5, 0.65),
        "synthetic" | "harness" => (0.5, 0.6),
        _ => (0.5, 1.),
    }
}
/// A row's score from its match strength, recency, boundary factor and meta scale,
/// multiplied and rounded to four places in 0.4.9's order (py/search.py:1874-1876).
pub(super) fn score(strength: f64, rec: f64, who: &str, factor: f64, meta: bool) -> f64 {
    let (w, s) = speaker(who);
    crate::window::round4(strength * rec * w * factor * s * meta_scale(meta))
}
pub fn recency(ts: i64, now: i64) -> f64 {
    let days = (now - ts).max(0) as f64 / 86_400_000.;
    0.5f64.powf(days / 14.)
}
/// The score 0.4.9 reported for a meaning row: its keyword formula with a full match
/// (py/search.py:1817-1876, 1938-1942), while the cosine ranks the row.
pub fn meaning_score(h: &Hit, now: i64) -> f64 {
    let rec = recency(h.ts, now).max(if h.who == "user" { 0.35 } else { 0. });
    let (w, s) = speaker(&h.who);
    window::round4(rec * w * s * meta_scale(meta_role(&h.who, h.turn)))
}
/// The ranked (rounded) score a row can reach when its raw score is at most `ceiling`.
/// The margin covers products formed in another order than the score's.
pub(super) fn ranked(ceiling: f64) -> f64 {
    window::round4(ceiling * (1. + 1e-12))
}
/// Session-head views fold lane confidence into the score.
pub(super) fn lane(tier: u8) -> f64 {
    match tier {
        1 => 0.7,
        2 => 0.5,
        _ => 1.,
    }
}
/// A row as `Matcher::evaluate` found it.
pub(super) struct Scored {
    pub(super) tier: u8,
    pub(super) score: f64,
    /// The boundary class; `explicit`, `counted`, or `unscored` when the row cannot
    /// enter the page.
    pub(super) class: &'static str,
    pub(super) snippet: String,
    /// 0.4.9's match span (`_match_span`), in bytes of the row.
    pub(super) span: Option<std::ops::Range<usize>>,
}
impl Scored {
    pub(super) fn bare(tier: u8, class: &'static str) -> Self {
        Self {
            tier,
            score: 0.,
            class,
            snippet: String::new(),
            span: None,
        }
    }
}
/// The page's last row, which another row must outrank to enter a full page.
pub(super) struct Cutoff {
    pub(super) chats: bool,
    pub(super) tier: u8,
    /// Its score, lane-folded for session heads.
    pub(super) score: f64,
    /// Whether the candidate loses equal scores on time, session and turn.
    pub(super) later: bool,
}
impl Cutoff {
    /// `last` as the cutoff for a candidate (or a block's best tie key) at `ts`,
    /// `session` and `turn`.
    pub(super) fn of(last: &Hit, chats: bool, ts: i64, session: &str, turn: i64) -> Self {
        Self {
            chats,
            tier: last.tier,
            score: last.score * if chats { lane(last.tier) } else { 1. },
            later: ts < last.ts
                || (ts == last.ts
                    && (session > last.session.as_str()
                        || (session == last.session && turn > last.turn))),
        }
    }
    /// Whether a row of `tier` whose raw score is at most `ceiling` cannot enter. Rows
    /// rank by rounded score, then (session heads) lane, then time and position.
    pub(super) fn excludes(&self, tier: u8, ceiling: f64) -> bool {
        self.below(tier, ceiling)
            || (self.later && tier == self.tier && self.best(tier, ceiling) == self.score)
    }
    /// Whether such a row ranks below the cutoff whatever its time and position, as does
    /// every row of a lower ceiling.
    pub(super) fn below(&self, tier: u8, ceiling: f64) -> bool {
        if !self.chats && tier != self.tier {
            return tier > self.tier;
        }
        let best = self.best(tier, ceiling);
        best < self.score || (best == self.score && tier > self.tier)
    }
    pub(super) fn best(&self, tier: u8, ceiling: f64) -> f64 {
        ranked(ceiling) * if self.chats { lane(tier) } else { 1. }
    }
}
pub(super) type CandidateGroups = std::sync::Arc<HashMap<i64, Vec<i64>>>;
/// A selective scope's blocks with their row counts, in block order.
pub(super) type ScopeBlocks = std::sync::Arc<Vec<(i64, usize)>>;
/// 0.4.9's `_rank_key` (rows) and `_session_view_key` (session heads) over rounded
/// scores, then the row id.
pub(super) fn compare(a: &Hit, b: &Hit, sort: &str, chats: bool) -> std::cmp::Ordering {
    let score = |h: &Hit| h.score * lane(h.tier);
    let order = match sort {
        "time" => b.ts.cmp(&a.ts),
        "position" => a.session.cmp(&b.session).then(a.turn.cmp(&b.turn)),
        _ => {
            if chats {
                score(b).total_cmp(&score(a)).then(a.tier.cmp(&b.tier))
            } else {
                a.tier.cmp(&b.tier).then(b.score.total_cmp(&a.score))
            }
        }
    };
    order
        .then(b.ts.cmp(&a.ts))
        .then(a.session.cmp(&b.session))
        .then(a.turn.cmp(&b.turn))
        .then(a.who.cmp(&b.who))
        .then(a.rowid.cmp(&b.rowid))
}
/// Whether another chat's copy of `row` (the same tier, score and time) can rank below
/// `last` on a row page: rows equal so far rank by their chat, which a position page
/// orders first.
pub fn copy_may_rank_below(row: &Hit, last: &Hit, sort: &str) -> bool {
    match sort {
        "time" => row.ts == last.ts,
        "position" => true,
        _ => row.tier == last.tier && row.score.total_cmp(&last.score).is_eq() && row.ts == last.ts,
    }
}
pub(super) type BlockBound = (i64, f64, i64, String, i64, i64);
