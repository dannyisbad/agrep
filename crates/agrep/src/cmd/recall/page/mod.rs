//! Recall and pack pages fitted to a byte budget the way agrep 0.4.9 fitted them
//! (py/recall.py): text pages split the budget across blocks and cap messages, JSON pages
//! cap by share and then shrink, drop and omit until they fit.
use super::{head, hit_floors, weak, Block, JUDGEABLE_BLOCK};
use agrep_core::{
    evidence::{self, Anchor, Event, Row, ToolMatch},
    handles, json_output, surface,
};
use anyhow::{bail, Result};
use regex::{Captures, Regex};
use serde_json::{json, Map, Value};
use std::{
    cmp::Reverse,
    collections::{BTreeMap, BTreeSet, BinaryHeap, HashSet},
    sync::LazyLock,
};
/// What each collapsed weak line holds back from the page.
const WEAK_LINE_COST: usize = 240;
/// The shortest trailer that still counts every omitted hit, reserved before floors are set.
const TRAILER_RESERVE: usize = 64;
const MORE_HINT: &str = "see --json omitted_hits";
/// Visible match bytes below which a tiny page leaves its match line out.
const TINY_MATCH_MIN: usize = 8;
/// Splits a weak line's head from its snippet. Sanitized content cannot hold U+001F, and
/// it renders as a space of the same width.
const SNIP_SEP: &str = "\x1f- ";
/// Every truncation marker recall writes; a cut never lands inside one.
static TRUNC_MARKER: LazyLock<Regex> = LazyLock::new(|| {
    let nouns = "API errors?|compactions?|task notifications?";
    Regex::new(&format!(
        r"\[(?:\+[\d,]+ (?:chars|hit\(s\) over budget|(?:tool calls|{nouns})(?:, [\d,]+ (?:{nouns}))*) - [^\]]*|[\d,]+ chars|snippet - open: [^\]]*)\]"
    ))
    .expect("truncation marker pattern")
});
static BUDGET_MARKER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\[output truncated to --budget[^\]]*\]").expect("budget marker pattern")
});
static MSG_ROW: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^( +)(\d+) (\S+): (.*)$").expect("message row pattern"));
static TRAILER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?s)^\[\+(\d+) hit\(s\) over budget(?: - (.*))?\]\z").expect("trailer pattern")
});
static MORE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"[\s\x1C-\x1F]*(?:\+(\d+) more \(see --json omitted_hits\)|see --json omitted_hits)\z",
    )
    .expect("trailer pointer pattern")
});
/// `cap`'s tail marker, removed before a JSON row shrinks again.
static CAP_MARKER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r" \[\+[\d,]+ chars - agrep around .+ \d+ -C 0(?: --max-chars 0| --full)?(?: --who \w+)?\]\z",
    )
    .expect("cap marker pattern")
});
/// `match_windows`' closing gap and snippet action.
static SNIPPET_TAIL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?: \[(\d[\d,]*) chars\])? \[snippet - open: [^\]]*\]\z")
        .expect("snippet tail pattern")
});
/// The head marker of a slice slid to its match.
static SLID_HEAD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\[\+[\d,]+ chars - [^\]]*\] ").expect("slid head pattern"));

/// A recall page's blocks with each query's anchors.
pub(super) struct Page<'a> {
    pub blocks: &'a [Block],
    pub queries: &'a [String],
    /// Each query's anchors; a handle is an address, not a match, and has none.
    pub anchors: Vec<Vec<Anchor>>,
    pub budget: usize,
}
impl<'a> Page<'a> {
    pub fn new(blocks: &'a [Block], queries: &'a [String], budget: usize) -> Self {
        let anchors = queries
            .iter()
            .map(|q| {
                if handles::is_handle(q) {
                    Vec::new()
                } else {
                    evidence::anchor_patterns(q)
                }
            })
            .collect();
        Self {
            blocks,
            queries,
            anchors,
            budget,
        }
    }
    /// The anchors of every query a block's windows came from (0.4.9's `_hit_anchors`).
    fn block_anchors(&self, b: &Block) -> Vec<&Anchor> {
        let mut seen = Vec::new();
        for q in &b.queries {
            if !seen.contains(q) {
                seen.push(*q);
            }
        }
        seen.iter()
            .flat_map(|q| self.anchors[(*q).min(self.anchors.len() - 1)].iter())
            .collect()
    }
}
/// A turn as 0.4.9's windows held it: the first prompt-side row with text and the first
/// reply with text.
pub(super) struct Turn<'a> {
    pub turn: i64,
    pub ts: i64,
    pub who: &'a str,
    pub text: &'a str,
    pub reply: Option<&'a Row>,
}
/// `rows` (in turn order, replies after prompts) as turns.
pub(super) fn turns(rows: &[Row]) -> Vec<Turn<'_>> {
    let mut out: Vec<Turn> = Vec::new();
    for row in rows.iter().filter(|row| row.who != "tool") {
        if out.last().is_none_or(|t| t.turn != row.turn) {
            out.push(Turn {
                turn: row.turn,
                ts: row.ts,
                who: "user",
                text: "",
                reply: None,
            });
        }
        let t = out.last_mut().expect("a turn was just pushed");
        if row.who == "agent" {
            if t.reply.is_none_or(|r| r.text.is_empty()) {
                t.reply = Some(row);
            }
        } else if t.text.is_empty() {
            (t.text, t.who, t.ts) = (&row.text, &row.who, row.ts);
        }
    }
    out
}
fn reply_text<'a>(t: &Turn<'a>) -> &'a str {
    t.reply.map_or("", |r| r.text.as_str())
}
/// The rows of a block's hit turns: turn, speaker and text (0.4.9's `_hit_rows`).
fn hit_rows<'a>(turns: &'a [Turn], hit_turns: &BTreeSet<i64>) -> Vec<(i64, &'a str, &'a str)> {
    turns
        .iter()
        .filter(|t| hit_turns.contains(&t.turn))
        .flat_map(|t| [(t.turn, t.who, t.text), (t.turn, "agent", reply_text(t))])
        .filter(|(_, _, text)| !text.is_empty())
        .collect()
}
/// Bytes a block's hit turns take as rendered rows, at most one judgeable block.
pub(super) fn hit_need(turns: &[Turn], hit_turns: &BTreeSet<i64>) -> usize {
    hit_rows(turns, hit_turns)
        .iter()
        .map(|(_, who, text)| text.len() + 10 + who.chars().count())
        .sum::<usize>()
        .min(JUDGEABLE_BLOCK)
}
/// The smallest hit rows the fitter can render: a capped row keeps its pointer whole.
fn hit_min(turns: &[Turn], hit_turns: &BTreeSet<i64>, target: &str) -> usize {
    hit_rows(turns, hit_turns)
        .iter()
        .map(|(turn, who, text)| {
            let marker = format!(
                " [+{} chars - {}]",
                surface::number(text.chars().count()),
                evidence::expand_command(target, *turn, who)
            );
            (text.len() + 10 + who.chars().count())
                .min(marker.chars().count() + 12 + who.chars().count())
        })
        .sum()
}
/// A message cap that spreads `share` over a window's messages, less a marker reserve.
fn msg_cap(share: usize, turns: &[Turn], reserve: usize) -> usize {
    if share == 0 {
        return 0;
    }
    let n = turns
        .iter()
        .map(|t| usize::from(!t.text.is_empty()) + usize::from(!reply_text(t).is_empty()))
        .sum::<usize>();
    (share / n.max(1)).saturating_sub(reserve).max(80)
}
/// A tool hit's event selector and span.
fn tool_claim(b: &Block) -> (Option<String>, Option<(usize, usize)>) {
    if b.hit.who != "tool" {
        return (None, None);
    }
    handles::parse(&b.hit.handle).map_or((None, None), |c| (c.event, c.span))
}
fn matches<'e>(
    b: &'e Block,
    claim: &(Option<String>, Option<(usize, usize)>),
) -> Vec<(&'e Event, ToolMatch)> {
    b.window
        .events
        .iter()
        .map(|e| {
            (
                e,
                evidence::selected_tool_match(e, claim.0.as_deref(), claim.1),
            )
        })
        .collect()
}
/// What a selected tool event costs a page.
fn event_cost(e: &Event) -> usize {
    360.min(80 + e.input.len() + 200.min(e.kept_output_bytes()))
}
/// The `around` call that opens every turn a block shows.
fn window_command(b: &Block, turns: &[Turn]) -> String {
    let center = b.window.center;
    let radius = turns
        .iter()
        .map(|t| t.turn)
        .chain(b.window.events.iter().map(|e| e.turn))
        .map(|turn| (turn - center).abs())
        .max()
        .unwrap_or(0);
    surface::shell(&[
        "agrep".into(),
        "around".into(),
        b.window.target.clone(),
        center.to_string(),
        "-C".into(),
        radius.to_string(),
        "--tool-output".into(),
        "200".into(),
    ])
}
fn gap_line(previous: i64, turn: i64) -> String {
    if turn - previous == 2 {
        format!("       [turn {} not shown]", previous + 1)
    } else {
        format!("       [turns {}-{} not shown]", previous + 1, turn - 1)
    }
}
fn marker_counts(events: &[&Event]) -> [usize; 3] {
    evidence::MARKERS.map(|(name, _)| {
        events
            .iter()
            .filter(|e| e.is_marker() && e.name == name)
            .count()
    })
}
fn events_tail(tools: usize, markers: &[usize; 3], expand: &str) -> String {
    let mut parts = Vec::new();
    if tools > 0 {
        parts.push(format!("{tools} tool calls"));
    }
    for (n, (_, noun)) in markers.iter().zip(evidence::MARKERS) {
        if *n > 0 {
            parts.push(format!(
                "{} {noun}{}",
                surface::number(*n),
                if *n == 1 { "" } else { "s" }
            ));
        }
    }
    if parts.is_empty() {
        String::new()
    } else {
        format!("       [+{} - {expand}]", parts.join(", "))
    }
}
fn trim_end(text: &str) -> &str {
    text.trim_end_matches(evidence::py_space)
}
fn trim_start(text: &str) -> &str {
    text.trim_start_matches(evidence::py_space)
}
mod json;
/// A block's line once fitted.
mod text;
pub(super) use json::json;
pub(super) use text::text;

fn written(payload: String, budget: usize) -> String {
    let mut payload = payload;
    if budget > 0 && payload.len() > budget - 1 {
        payload = surface::prefix(&payload, budget - 1).to_owned();
    }
    payload + "\n"
}
