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
/// A block's line once fitted.
struct Record {
    line: String,
    required: bool,
    tool: bool,
    drop_key: (u8, i64, usize),
}
/// 0.4.9's `_cap_line_bytes`: a line cut to `budget` bytes, keeping its recovery pointer.
fn cap_line_bytes(line: &str, budget: i64) -> String {
    if line.len() as i64 <= budget {
        return line.to_owned();
    }
    let markers: Vec<(usize, usize, bool)> = TRUNC_MARKER
        .find_iter(line)
        .map(|m| (m.start(), m.end(), m.as_str().contains("agrep around ")))
        .collect();
    let mut budget = budget;
    let (body, suffix) = match markers.iter().rev().find(|m| m.2) {
        Some(&(start, _, _)) => {
            let suffix = format!(" {}", &line[start..]);
            budget -= suffix.len() as i64;
            (trim_end(&line[..start]), suffix)
        }
        None => (line, String::new()),
    };
    if budget <= 0 {
        return trim_start(&suffix).to_owned();
    }
    let ellipsis = if budget > 3 { "…" } else { "" };
    let mut cut = surface::prefix(body, (budget - ellipsis.len() as i64) as usize).len();
    if let Some(&(start, _, _)) = markers
        .iter()
        .find(|(start, end, _)| *start < cut && cut < *end)
    {
        cut = start;
    }
    let prefix = trim_end(&body[..cut]);
    if prefix.is_empty() {
        trim_start(&suffix).to_owned()
    } else {
        format!("{prefix}{ellipsis}{suffix}")
    }
}
/// 0.4.9's `_cap_required_lines`: exact bytes shared so every required row keeps a prefix;
/// the head takes its whole size first when it fits.
fn cap_required_lines(
    head: &str,
    records: &mut [Record],
    required: &[usize],
    budget: usize,
) -> String {
    let values: Vec<String> = std::iter::once(head.to_owned())
        .chain(required.iter().map(|i| records[*i].line.clone()))
        .collect();
    let mut remaining = budget.saturating_sub(values.len().saturating_sub(1));
    let mut pending: Vec<usize> = (0..values.len()).collect();
    let mut allocations = vec![0; values.len()];
    if head.len() <= remaining {
        allocations[0] = head.len();
        remaining -= head.len();
        pending.remove(0);
    }
    while !pending.is_empty() {
        let share = remaining / pending.len();
        let small: Vec<usize> = pending
            .iter()
            .copied()
            .filter(|i| values[*i].len() <= share)
            .collect();
        if small.is_empty() {
            let extra = remaining % pending.len();
            for (offset, index) in pending.iter().enumerate() {
                allocations[*index] = share + usize::from(offset < extra);
            }
            break;
        }
        for index in small {
            allocations[index] = values[index].len();
            remaining -= values[index].len();
            pending.retain(|p| *p != index);
        }
    }
    let mut values = values
        .iter()
        .zip(&allocations)
        .map(|(value, budget)| cap_line_bytes(value, *budget as i64));
    let head = values.next().expect("the head is always present");
    for (index, value) in required.iter().zip(values) {
        records[*index].line = value;
    }
    head
}
/// 0.4.9's `_fit_recall_records`: a block within `limit` bytes (zero is unbounded) that
/// keeps its required rows, dropping context rows farthest from the hit first.
fn fit_records(
    head: &str,
    records: &mut [Record],
    limit: usize,
    expand: &str,
    hidden_tools: usize,
    markers: &[usize; 3],
) -> String {
    let join = |head: &str, lines: Vec<&str>, tail: &str| {
        let mut out = head.to_owned();
        for line in lines.into_iter().chain((!tail.is_empty()).then_some(tail)) {
            out.push('\n');
            out.push_str(line);
        }
        out
    };
    if limit == 0 {
        let tail = events_tail(hidden_tools, markers, expand);
        return join(
            head,
            records.iter().map(|r| r.line.as_str()).collect(),
            &tail,
        );
    }
    let required: Vec<usize> = (0..records.len())
        .filter(|i| records[*i].required)
        .collect();
    let mut keep = vec![true; records.len()];
    let (mut dropped_tools, mut dropped_context) = (hidden_tools, 0);
    let marker = |dropped_context: usize, dropped_tools: usize| {
        if dropped_context > 0 {
            format!("     [output truncated to --budget - rest at {expand}]")
        } else {
            events_tail(dropped_tools, markers, expand)
        }
    };
    let mut droppable: Vec<usize> = (0..records.len())
        .filter(|i| !records[*i].required)
        .collect();
    droppable.sort_by_key(|i| records[*i].drop_key);
    let mut kept_bytes: usize = records.iter().map(|r| r.line.len()).sum();
    let mut kept_rows = records.len();
    let size = |kept_bytes: usize, kept_rows: usize, tail: &str| {
        head.len() + kept_bytes + tail.len() + kept_rows + usize::from(!tail.is_empty())
    };
    for index in droppable {
        if size(
            kept_bytes,
            kept_rows,
            &marker(dropped_context, dropped_tools),
        ) <= limit
        {
            break;
        }
        keep[index] = false;
        kept_bytes -= records[index].line.len();
        kept_rows -= 1;
        if records[index].tool {
            dropped_tools += 1;
        } else {
            dropped_context += 1;
        }
    }
    let tail = marker(dropped_context, dropped_tools);
    if size(kept_bytes, kept_rows, &tail) <= limit {
        let lines = records
            .iter()
            .zip(&keep)
            .filter(|(_, keep)| **keep)
            .map(|(r, _)| r.line.as_str())
            .collect();
        return join(head, lines, &tail);
    }
    if tail.is_empty() {
        let head = cap_required_lines(head, records, &required, limit);
        return join(
            &head,
            required.iter().map(|i| records[*i].line.as_str()).collect(),
            "",
        );
    }
    let compact = if dropped_context > 0 {
        "     … surrounding context omitted"
    } else if dropped_tools > 0 {
        "       … surrounding tool calls omitted"
    } else {
        "       … surrounding events omitted"
    };
    let tail = if compact.len() < tail.len() {
        compact.to_owned()
    } else {
        tail
    };
    let head = cap_required_lines(
        head,
        records,
        &required,
        limit.saturating_sub(tail.len() + 1),
    );
    join(
        &head,
        required.iter().map(|i| records[*i].line.as_str()).collect(),
        &tail,
    )
}
/// The page a budget too small for its first block keeps: the head (its handle is the
/// recovery route) and the first message row cut plain, or the head alone; `None` when
/// even the head does not fit.
fn tiny_block(head: &str, rows: &[String], room: i64) -> Option<String> {
    if head.len() as i64 > room {
        return None;
    }
    let left = room - head.len() as i64 - 1;
    for line in rows {
        let plain = TRUNC_MARKER.replace_all(line, "");
        let plain = trim_end(&plain);
        let Some(found) = MSG_ROW.captures(plain) else {
            continue;
        };
        let prefix = found
            .get(4)
            .expect("the body group always participates")
            .start();
        if left >= (prefix + TINY_MATCH_MIN) as i64 {
            return Some(format!("{head}\n{}", cap_line_bytes(plain, left)));
        }
        break;
    }
    Some(head.to_owned())
}
fn bare_trailer(count: usize) -> String {
    format!("[+{count} hit(s) over budget - {MORE_HINT}]")
}
/// `[+N hit(s) over budget - h1 h2 +K more (see --json omitted_hits)]` within `limit`
/// bytes (zero is unbounded): the count always, then as many handles as fit.
fn over_budget_trailer(count: usize, cmds: &[String], limit: usize) -> String {
    let mut unique: Vec<&str> = Vec::new();
    for cmd in cmds {
        if !cmd.is_empty() && !unique.contains(&cmd.as_str()) {
            unique.push(cmd);
        }
    }
    let joiner = if !unique.is_empty()
        && unique
            .iter()
            .all(|c| c.starts_with('@') && !c.chars().any(evidence::py_space))
    {
        " "
    } else {
        " · "
    };
    let base = format!("[+{count} hit(s) over budget");
    let fits = |candidate: &str| limit == 0 || candidate.len() <= limit;
    for pointer in [true, false] {
        for shown in (0..=unique.len()).rev() {
            let named = unique[..shown].join(joiner);
            let more = if pointer && count > shown {
                format!("+{} more ({MORE_HINT})", count - shown)
            } else {
                String::new()
            };
            let body = [named, more]
                .into_iter()
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join(" ");
            let candidate = if body.is_empty() {
                format!("{base}]")
            } else {
                format!("{base} - {body}]")
            };
            if (shown > 0 || pointer) && fits(&candidate) {
                return candidate;
            }
        }
    }
    let bare = bare_trailer(count);
    if fits(&bare) {
        bare
    } else {
        format!("{base}]")
    }
}
/// The omitted count and named commands of an over-budget trailer.
fn parse_trailer(trailer: &str) -> Option<(usize, Vec<String>)> {
    let found = TRAILER.captures(trailer)?;
    let tail = MORE.replace(found.get(2).map_or("", |m| m.as_str()), "");
    let words: Vec<&str> = tail
        .split(evidence::py_space)
        .filter(|w| !w.is_empty())
        .collect();
    let cmds: Vec<String> = if !words.is_empty() && words.iter().all(|w| w.starts_with('@')) {
        words.into_iter().map(str::to_owned).collect()
    } else {
        tail.split(" · ")
            .filter(|c| !c.is_empty())
            .map(str::to_owned)
            .collect()
    };
    Some((found[1].parse().ok()?, cmds))
}
/// A page holding only the disclosure: every block folds into the count, lead first.
fn degrade_trailer(trailer: &str, ends: &[(usize, String)], budget: usize) -> String {
    let Some((count, cmds)) = parse_trailer(trailer) else {
        return surface::prefix(trailer, budget).to_owned();
    };
    let named: Vec<String> = ends
        .iter()
        .map(|(_, cmd)| cmd.clone())
        .filter(|cmd| !cmd.is_empty())
        .chain(cmds)
        .collect();
    over_budget_trailer(count + ends.len(), &named, budget)
}
/// Handles of the blocks a cut at byte `cut` drops whole.
fn evicted_handles(cut: usize, ends: &[(usize, String)]) -> Vec<String> {
    let mut start = 0;
    let mut out = Vec::new();
    for (end, cmd) in ends {
        if start >= cut && !cmd.is_empty() {
            out.push(cmd.clone());
        }
        start = end + 2;
    }
    out
}
fn with_evicted(trailer: &str, evicted: &[String], limit: usize) -> String {
    if evicted.is_empty() {
        return trailer.to_owned();
    }
    let (count, cmds) = parse_trailer(trailer).unwrap_or_default();
    let named: Vec<String> = evicted.iter().cloned().chain(cmds).collect();
    over_budget_trailer(count + evicted.len(), &named, limit)
}
fn joined(text: &str, trailer: &str) -> String {
    match (text.is_empty(), trailer.is_empty()) {
        (false, false) => format!("{text}\n\n{trailer}"),
        (true, _) => trailer.to_owned(),
        (false, true) => text.to_owned(),
    }
}
/// 0.4.9's `_fit_text_payload`: `text` and its trailer within `budget` bytes, never cutting
/// inside a marker or a later block's head line; blocks the cut evicts join the trailer.
fn fit_text(
    text: &str,
    budget: usize,
    ends: &[(usize, String)],
    trailer: &str,
    keep: Option<usize>,
) -> String {
    let whole = joined(text, trailer);
    if budget == 0 || whole.len() <= budget {
        return whole;
    }
    let first = ends.first().map_or(text.len(), |(end, _)| *end);
    let limit = match keep {
        Some(keep) => (budget as i64 - keep.min(first) as i64 - 2).max(1) as usize,
        None => (budget as i64 - first as i64 - 2).max(TRAILER_RESERVE as i64) as usize,
    };
    let mut evicted: Vec<String> = Vec::new();
    let mut body = String::new();
    for _ in 0..6 {
        let full = with_evicted(trailer, &evicted, limit);
        let (fitted, cut) = cut_text(text, budget, ends, &full);
        body = fitted;
        let now = cut.map_or_else(Vec::new, |cut| evicted_handles(cut, ends));
        if now == evicted {
            break;
        }
        evicted = now;
    }
    body
}
/// One cut: the fitted page and the byte offset the cut landed at (`None` when the
/// trailer alone fills the page).
fn cut_text(
    text: &str,
    budget: usize,
    ends: &[(usize, String)],
    trailer: &str,
) -> (String, Option<usize>) {
    if !trailer.is_empty() {
        let whole = joined(text, trailer);
        if budget == 0 || whole.len() <= budget {
            return (whole, Some(text.len()));
        }
        let room = budget as i64 - trailer.len() as i64 - 2;
        if room >= 80 {
            let (body, cut) = cut_text(text, room as usize, ends, "");
            return (body + "\n\n" + trailer, cut);
        }
        return (degrade_trailer(trailer, ends, budget), None);
    }
    if budget == 0 || text.len() <= budget {
        return (text.to_owned(), Some(text.len()));
    }
    let generic = "\n[output truncated to --budget; rerun with a larger budget]";
    let mut marker = generic.to_owned();
    for _ in 0..3 {
        let cut = budget as i64 - marker.len() as i64;
        let wanted = ends
            .iter()
            .find(|(end, cmd)| *end as i64 > cut && !cmd.is_empty())
            .map_or_else(
                || generic.to_owned(),
                |(_, cmd)| format!("\n[output truncated to --budget - rest at {cmd}]"),
            );
        if wanted == marker {
            break;
        }
        marker = wanted;
    }
    if marker.len() >= budget {
        marker = generic.to_owned();
    }
    if marker.len() >= budget {
        return (surface::prefix(generic, budget).to_owned(), Some(0));
    }
    let mut cut = surface::prefix(text, budget - marker.len()).len();
    let scan = text[..cut]
        .char_indices()
        .rev()
        .nth(399)
        .map_or(0, |(at, _)| at);
    for m in TRUNC_MARKER.find_iter(&text[scan..]) {
        let (start, end) = (scan + m.start(), scan + m.end());
        if start >= cut {
            break;
        }
        if end > cut {
            cut = start;
            break;
        }
    }
    // A cut inside a later block's head line would slice its handle: the block goes whole.
    let mut start = 0;
    for (end, _) in ends {
        if start < cut && cut < *end {
            let head_end = text[start..].find('\n').map(|at| start + at);
            if start > 0 && head_end.is_none_or(|head_end| cut <= head_end) {
                cut = start;
            }
            break;
        }
        start = end + 2;
    }
    (trim_end(&text[..cut]).to_owned() + &marker, Some(cut))
}
const HEADER: &str = "\x1b[1;36m";
const TURN: &str = "\x1b[33m";
const AGENT: &str = "\x1b[36m";
const DIM: &str = "\x1b[2m";
const FAILED: &str = "\x1b[1;31m";
const RESET: &str = "\x1b[0m";
fn dim_markers(text: &str) -> String {
    let dim = |c: &Captures| format!("{DIM}{}{RESET}", &c[0]);
    let once = TRUNC_MARKER.replace_all(text, dim);
    BUDGET_MARKER.replace_all(&once, dim).into_owned()
}
fn paint_line(line: &str) -> String {
    if line.starts_with("── ") {
        return match line.split_once(SNIP_SEP) {
            Some((head, snippet)) => format!("{HEADER}{head}{RESET} - {}", dim_markers(snippet)),
            None => format!("{HEADER}{line}{RESET}"),
        };
    }
    if let Some(row) = MSG_ROW.captures(line) {
        let speaker = if &row[3] == "agent" { AGENT } else { TURN };
        return format!(
            "{}{TURN}{}{RESET} {speaker}{}:{RESET} {}",
            &row[1],
            &row[2],
            &row[3],
            dim_markers(&row[4])
        );
    }
    if line.starts_with("     ") {
        let code = if line.contains("✗ FAILED") {
            FAILED
        } else {
            DIM
        };
        return format!("{code}{line}{RESET}");
    }
    dim_markers(line)
}
/// The fitted page painted: the budget never counts these bytes.
fn paint(payload: &str) -> String {
    payload
        .split('\n')
        .map(paint_line)
        .collect::<Vec<_>>()
        .join("\n")
        .replace('\x1f', " ")
}
/// 0.4.9's `_render_text_payload`: the fitted page, painted within the budget when color
/// is on, with the final newline counted.
fn render_payload(
    text: &str,
    budget: usize,
    ends: &[(usize, String)],
    trailer: &str,
    color: bool,
    keep: Option<usize>,
) -> String {
    let content = budget.saturating_sub(1);
    if !color {
        return fit_text(text, content, ends, trailer, keep).replace('\x1f', " ");
    }
    if budget == 0 {
        return paint(&fit_text(text, 0, ends, trailer, keep));
    }
    let mut plain_budget = content;
    for _ in 0..12 {
        let painted = paint(&fit_text(text, plain_budget, ends, trailer, keep));
        let excess = painted.len() as i64 - content as i64;
        if excess <= 0 {
            return painted;
        }
        if plain_budget == 0 {
            break;
        }
        let next = (plain_budget as i64 - excess).max(0) as usize;
        plain_budget = (plain_budget - 1).min(next);
    }
    fit_text(text, content, ends, trailer, keep).replace('\x1f', " ")
}
/// One page line ending in a newline, cut to the budget.
fn written(payload: String, budget: usize) -> String {
    let mut payload = payload;
    if budget > 0 && payload.len() > budget - 1 {
        payload = surface::prefix(&payload, budget - 1).to_owned();
    }
    payload + "\n"
}
/// The text of a block's hit turn, else its first message.
fn center_text<'a>(b: &Block, turns: &[Turn<'a>]) -> &'a str {
    let center = turns
        .iter()
        .find(|t| t.turn == b.window.center)
        .and_then(|t| {
            let reply = reply_text(t);
            let text = if b.hit.who == "agent" && !reply.is_empty() {
                reply
            } else if !t.text.is_empty() {
                t.text
            } else {
                reply
            };
            (!text.is_empty()).then_some(text)
        });
    center.unwrap_or_else(|| {
        turns
            .iter()
            .map(|t| {
                if t.text.is_empty() {
                    reply_text(t)
                } else {
                    t.text
                }
            })
            .find(|text| !text.is_empty())
            .unwrap_or("")
    })
}
/// The text page within `page.budget` bytes (zero is unbounded), 0.4.9's render: weak
/// blocks past the first collapse to one line, every hit turn gets a floor, the spare
/// widens blocks with a double share for the first, and when the page's text exceeds 85%
/// of what is left, messages are capped and each block is fitted to its share.
pub(super) fn text(page: &Page, color: bool) -> Result<String> {
    let budget = page.budget;
    let blocks = page.blocks;
    let n = blocks.len();
    let collapse: Vec<bool> = blocks
        .iter()
        .enumerate()
        .map(|(i, b)| i > 0 && weak(&b.hit))
        .collect();
    let full: Vec<usize> = (0..n).filter(|i| !collapse[*i]).collect();
    let full_count = full.len();
    let mut content_budget = if budget > 0 {
        budget.saturating_sub(WEAK_LINE_COST * (n - full_count))
    } else {
        0
    };
    let heads: Vec<String> = blocks.iter().map(|b| head(&b.hit)).collect();
    let turns: Vec<Vec<Turn>> = blocks.iter().map(|b| turns(&b.window.rows)).collect();
    let head_costs: Vec<usize> = full
        .iter()
        .map(|i| format!("── {}", heads[*i]).len() + 5)
        .collect();
    let needs: Vec<usize> = full
        .iter()
        .zip(&head_costs)
        .map(|(i, cost)| hit_need(&turns[*i], &blocks[*i].hit_turns) + cost)
        .collect();
    let mins: Vec<usize> = full
        .iter()
        .map(|i| hit_min(&turns[*i], &blocks[*i].hit_turns, &blocks[*i].window.target))
        .collect();
    let mut floors = hit_floors(&needs, &head_costs, &mins, content_budget);
    if budget > 0 && floors.contains(&0) {
        // omitted hits are named in a trailer the first block must leave room for
        content_budget = content_budget.saturating_sub(TRAILER_RESERVE);
        floors = hit_floors(&needs, &head_costs, &mins, content_budget);
    }
    let mut floor = vec![0; n];
    for (i, f) in full.iter().zip(&floors) {
        floor[*i] = *f;
    }
    let room = if budget > 0 {
        full.iter().filter(|i| floor[**i] > 0).count()
    } else {
        full_count
    }
    .max(1);
    let spare = if budget > 0 {
        content_budget.saturating_sub(floors.iter().sum())
    } else {
        0
    };
    let spare_share = spare / (full_count.min(room) + usize::from(full_count > 1)).max(1);
    let claims: Vec<_> = blocks.iter().map(tool_claim).collect();
    let selections: Vec<Vec<(&Event, ToolMatch)>> = blocks
        .iter()
        .zip(&claims)
        .map(|(b, claim)| matches(b, claim))
        .collect();
    let raw: usize = full
        .iter()
        .map(|i| {
            turns[*i]
                .iter()
                .map(|t| t.text.len() + reply_text(t).len())
                .sum::<usize>()
                + selections[*i]
                    .iter()
                    .filter(|(_, m)| m.selected)
                    .map(|(e, _)| event_cost(e))
                    .sum::<usize>()
        })
        .sum();
    let fits = budget == 0 || raw <= (content_budget as f64 * 0.85) as usize;
    let trail = |start: usize, used: usize| {
        let rest = &blocks[start..];
        let cmds: Vec<String> = rest.iter().map(|b| b.hit.handle.clone()).collect();
        let left = if budget > 0 {
            budget as i64 - 1 - used as i64 - 2
        } else {
            0
        };
        let pointer = over_budget_trailer(rest.len(), &[], 0);
        let limit = if budget > 0 && pointer.len() as i64 <= left {
            left as usize
        } else {
            0
        };
        over_budget_trailer(rest.len(), &cmds, limit)
    };
    let mut used = 0;
    let mut shown_full = 0;
    let mut out: Vec<String> = Vec::new();
    let mut cmds: Vec<String> = Vec::new();
    let mut trailer = String::new();
    let mut first_block: Option<(String, Vec<String>, String)> = None;
    for (i, b) in blocks.iter().enumerate() {
        if budget > 0 && (used >= budget || (!collapse[i] && floor[i] == 0)) {
            trailer = trail(i, used);
            break;
        }
        if collapse[i] {
            let anchors: Vec<&Anchor> = page.anchors[b.queries[0].min(page.anchors.len() - 1)]
                .iter()
                .collect();
            let snippet = surface::safe(
                &evidence::weak_snippet(center_text(b, &turns[i]), &b.hit.snippet, &anchors),
                false,
            );
            let line = if snippet.is_empty() {
                format!("── {}", heads[i])
            } else {
                format!("── {}{SNIP_SEP}{snippet}", heads[i])
            };
            used += line.len() + 1;
            out.push(line);
            cmds.push(b.hit.handle.clone());
            continue;
        }
        let share = if budget > 0 {
            floor[i]
                + spare_share
                    * if shown_full == 0 && full_count > 1 {
                        2
                    } else {
                        1
                    }
        } else {
            0
        };
        let cap = if fits {
            0
        } else {
            msg_cap(share, &turns[i], 120)
        };
        let anchors = if fits {
            Vec::new()
        } else {
            page.block_anchors(b)
        };
        let head_line = format!("── {}", heads[i]);
        let mut by_turn: BTreeMap<i64, Vec<&(&Event, ToolMatch)>> = BTreeMap::new();
        for selection in &selections[i] {
            by_turn.entry(selection.0.turn).or_default().push(selection);
        }
        let rows_in = |hit: bool| {
            turns[i]
                .iter()
                .filter(|t| b.hit_turns.contains(&t.turn) == hit)
                .map(|t| usize::from(!t.text.is_empty()) + usize::from(!reply_text(t).is_empty()))
                .sum::<usize>()
        };
        let (center_rows, context_rows) = (rows_in(true), rows_in(false));
        let mut center_cap = cap;
        if share > 0 && !fits && center_rows > 0 {
            let context_reserve = context_rows * (cap + 80);
            let tool_reserve = if selections[i].iter().any(|(_, m)| m.selected) {
                360
            } else {
                0
            };
            let center_room = (80 * center_rows as i64)
                .max(share as i64 - head_line.len() as i64 - context_reserve as i64 - tool_reserve);
            center_cap = (center_room as usize / center_rows)
                .saturating_sub(80)
                .clamp(80, 2400);
        }
        let target = &b.window.target;
        let center = b.window.center;
        let row_body = |text: &str, cap: usize, expand: &str| {
            let flat = evidence::one_line(text);
            let (body, _) = if anchors.is_empty() {
                evidence::cap(&flat, cap, expand)
            } else {
                evidence::anchored_cap(&flat, cap, expand, &anchors)
            };
            surface::safe(&body, false)
        };
        let mut records = Vec::new();
        let mut hidden: Vec<&Event> = Vec::new();
        let mut previous = None;
        for t in &turns[i] {
            if let Some(previous) = previous.filter(|p| t.turn > p + 1) {
                records.push(Record {
                    line: gap_line(previous, t.turn),
                    required: true,
                    tool: false,
                    drop_key: (3, 0, records.len()),
                });
            }
            previous = Some(t.turn);
            let hit_turn = b.hit_turns.contains(&t.turn);
            let tcap = if hit_turn { center_cap } else { cap };
            let distance = -(t.turn - center).abs();
            if !t.text.is_empty() {
                let body = row_body(
                    t.text,
                    tcap,
                    &evidence::expand_command(target, t.turn, t.who),
                );
                records.push(Record {
                    line: format!("  {:>4} {}: {body}", t.turn, surface::safe(t.who, false)),
                    required: hit_turn,
                    tool: false,
                    drop_key: (2, distance, records.len()),
                });
            }
            for (e, m) in by_turn.get(&t.turn).into_iter().flatten() {
                if !m.selected {
                    hidden.push(e);
                    continue;
                }
                let line = format!(
                    "     {}",
                    evidence::tool_line(e, m.output_span, m.preview.as_deref())
                );
                records.push(Record {
                    line: surface::safe(&line, false),
                    required: true,
                    tool: true,
                    drop_key: (u8::from(e.ok != Some(true)), distance, records.len()),
                });
            }
            if let Some(reply) = t.reply.filter(|r| !r.text.is_empty()) {
                let mut body = row_body(
                    &reply.text,
                    tcap,
                    &evidence::expand_command(target, t.turn, "agent"),
                );
                if reply.reply_truncated() {
                    body += &format!(
                        " [source truncated at ingest; original reply {} chars]",
                        surface::number(reply.source_chars)
                    );
                }
                records.push(Record {
                    line: format!("  {:>4} agent: {body}", t.turn),
                    required: hit_turn,
                    tool: false,
                    drop_key: (2, distance, records.len()),
                });
            }
        }
        let markers = marker_counts(&hidden);
        if shown_full == 0 {
            first_block = Some((
                head_line.clone(),
                records
                    .iter()
                    .filter(|r| r.required)
                    .map(|r| r.line.clone())
                    .collect(),
                b.hit.handle.clone(),
            ));
        }
        let limit = if share > 0 && !fits {
            share.saturating_sub(4).max(80)
        } else {
            0
        };
        let block = fit_records(
            &head_line,
            &mut records,
            limit,
            &window_command(b, &turns[i]),
            hidden.len() - markers.iter().sum::<usize>(),
            &markers,
        );
        if budget > 0 && shown_full >= room && used + block.len() + 1 > budget {
            // past the block reserve, a block still renders when it fits what is left
            trailer = trail(i, used);
            break;
        }
        used += block.len() + 1;
        out.push(block);
        cmds.push(b.hit.handle.clone());
        shown_full += 1;
    }
    if let Some((head, rows, handle)) = first_block.filter(|_| budget > 0) {
        let content = budget - 1;
        let (over, bare, reserve) = if trailer.is_empty() {
            let marker = format!("\n[output truncated to --budget - rest at {handle}]");
            let over = used > content && content as i64 - marker.len() as i64 <= head.len() as i64;
            (over, String::new(), 0)
        } else {
            let bare = bare_trailer(n - 1);
            let reserve = bare.len() + 2;
            (used + 2 + trailer.len() > content, bare, reserve)
        };
        if over {
            let Some(tiny) = tiny_block(&head, &rows, content as i64 - reserve as i64) else {
                let need = head.len() + reserve + 1;
                let what = if bare.is_empty() {
                    "its hit block's head line"
                } else {
                    "its first hit block and the over-budget line"
                };
                bail!("--budget {budget} is too small for this page: {what} need at least {need} bytes");
            };
            (out, cmds, trailer) = (vec![tiny], vec![handle], bare);
        }
    }
    let mut ends = Vec::new();
    let mut offset = 0;
    for (block, cmd) in out.iter().zip(cmds) {
        offset += block.len();
        ends.push((offset, cmd));
        offset += 2;
    }
    let keep = (budget > 0)
        .then(|| full.iter().map(|i| floor[*i]).find(|f| *f > 0))
        .flatten();
    let payload = render_payload(&out.join("\n\n"), budget, &ends, &trailer, color, keep);
    Ok(written(payload, budget))
}
/// The compact JSON size of `value`.
fn json_len(value: &Value) -> Result<usize> {
    struct Count(usize);
    impl std::io::Write for Count {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0 += bytes.len();
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut count = Count(0);
    json_output::to_writer(&mut count, value)?;
    Ok(count.0)
}
/// A window row as fitted: its value, compact size and whether it must stay.
struct JsonRow {
    value: Value,
    size: usize,
    required: bool,
}
/// A hit as fitted: the hit object with an empty window, its size, and its rows.
struct JsonHit<'a> {
    frame: Value,
    frame_size: usize,
    rows: Vec<JsonRow>,
    target: &'a str,
    anchors: Vec<&'a Anchor>,
}
impl JsonHit<'_> {
    fn size(&self) -> usize {
        self.frame_size
            + self.rows.iter().map(|r| r.size).sum::<usize>()
            + self.rows.len().saturating_sub(1)
    }
}
/// A tool row's identity as 0.4.9 keyed it: kind, session, turn, timestamp, name.
fn tool_key(row: &Value) -> [Value; 5] {
    ["kind", "session", "turn", "ts", "name"]
        .map(|key| row.get(key).cloned().unwrap_or(Value::Null))
}
/// The JSON page within `page.budget` bytes (zero is unbounded): 0.4.9 capped messages by
/// share when the page's text exceeded 85% of the budget, kept only the selected event of
/// a tool hit, counted omitted events, then shrank, dropped and omitted until it fit.
pub(super) fn json(page: &Page, envelope: &mut Value) -> Result<String> {
    let budget = page.budget;
    let blocks = page.blocks;
    let n = blocks.len();
    let room = if budget > 0 {
        (budget / JUDGEABLE_BLOCK).max(1)
    } else {
        n
    };
    let share = if budget > 0 {
        budget / n.min(room).max(1)
    } else {
        0
    };
    let turns: Vec<Vec<Turn>> = blocks.iter().map(|b| turns(&b.window.rows)).collect();
    let raw: usize = turns
        .iter()
        .flatten()
        .map(|t| t.text.len() + reply_text(t).len())
        .sum();
    let fits = budget == 0 || raw <= (budget as f64 * 0.85) as usize;
    let mut hits = Vec::new();
    for (b, turns) in blocks.iter().zip(&turns) {
        let w = &b.window;
        let claim = tool_claim(b);
        let cap = if fits { 0 } else { msg_cap(share, turns, 260) };
        let anchors = if fits {
            Vec::new()
        } else {
            page.block_anchors(b)
        };
        let mut events: BTreeMap<i64, Vec<&Event>> = BTreeMap::new();
        for e in &w.events {
            events.entry(e.turn).or_default().push(e);
        }
        let mut rows = Vec::new();
        let (mut size, mut cut) = (0, 0);
        let mut required_tools = HashSet::new();
        for t in turns {
            let hit_turn = b.hit_turns.contains(&t.turn);
            let tcap = if fits {
                0
            } else if hit_turn {
                cap.max(2400.min(share))
            } else {
                cap
            };
            for (who, text, reply) in [(t.who, t.text, None), ("agent", reply_text(t), t.reply)] {
                if text.is_empty() {
                    continue;
                }
                let expand = evidence::expand_command(&w.target, t.turn, who);
                let (capped, omitted) = if !anchors.is_empty() && hit_turn {
                    evidence::anchored_cap(text, tcap, &expand, &anchors)
                } else {
                    evidence::cap(text, tcap, &expand)
                };
                size += capped.len() + 140;
                let mut row = json!({"kind":"msg","session":w.session,"project":w.project,"turn":t.turn,"who":who,"ts":t.ts,"text":capped,"omitted_chars":omitted});
                if let Some(reply) = reply {
                    row["reply_chars"] = json!(reply.source_chars);
                    row["reply_truncated"] = json!(reply.reply_truncated());
                }
                rows.push(row);
            }
            for e in events.get(&t.turn).into_iter().flatten() {
                let m = evidence::selected_tool_match(e, claim.0.as_deref(), claim.1);
                if !m.selected && (claim.0.is_some() || (share > 0 && size >= share)) {
                    cut += 1;
                    continue;
                }
                let mut input = e.input.clone();
                if input.chars().count() > 120 {
                    input = input.chars().take(120).collect::<String>() + "…";
                }
                size += input.len() + 160;
                let mut row = json!({"kind":e.kind,"session":w.session,"project":w.project,"turn":e.turn,"ts":e.ts,"name":e.name,"input":input,"ok":e.ok,"input_chars":e.input_chars,"output_chars":e.output_chars,"output_bytes":e.output_bytes,"input_truncated":e.input_truncated(),"output_truncated":e.output_truncated()});
                if m.selected {
                    if let Some(preview) = m.preview.filter(|p| !p.is_empty()) {
                        row["match_preview"] = json!(preview);
                    }
                    required_tools.insert(tool_key(&row));
                }
                rows.push(row);
            }
        }
        if cut > 0 {
            rows.push(json!({"kind":"events_omitted","session":w.session,"project":w.project,"n":cut,"expand":window_command(b, turns)}));
        }
        let h = &b.hit;
        // 0.4.9 labeled a meaning hit's lane and cosine, rounded to six places, and scored
        // it with the keyword formula (py/recall.py:2373, 2394; py/ask.py:2491;
        // py/search.py:1938-1942).
        let meaning = h.matched == "semantic";
        let score = if meaning {
            agrep_core::query::meaning_score(h, chrono::Utc::now().timestamp_millis())
        } else {
            h.score
        };
        let mut frame = json!({"session":w.session,"turn":w.center,"ts":h.ts,"agent":w.agent,"project":w.project,"score":score,"sem_score":meaning.then(|| agrep_core::window::round6(h.score)),"score_kind":meaning.then_some("cosine"),"matched":h.matched,"handle":h.handle,"self":h.self_marker,"window":[]});
        if meaning {
            frame["lane"] = json!("semantic");
        }
        let rows = rows
            .into_iter()
            .map(|value| {
                let required = (value["kind"] == "msg" && value["turn"] == json!(w.center))
                    || required_tools.contains(&tool_key(&value));
                Ok(JsonRow {
                    size: json_len(&value)?,
                    value,
                    required,
                })
            })
            .collect::<Result<_>>()?;
        hits.push(JsonHit {
            frame_size: json_len(&frame)?,
            frame,
            rows,
            target: &w.target,
            anchors: page.block_anchors(b),
        });
    }
    let fitted = fit_json(envelope, hits, budget.saturating_sub(1), page)?;
    Ok(written(fitted, budget))
}
fn assemble(envelope: &mut Value, hits: Vec<JsonHit>) -> Result<String> {
    envelope["hits"] = Value::Array(
        hits.into_iter()
            .map(|h| {
                let mut frame = h.frame;
                frame["window"] = Value::Array(h.rows.into_iter().map(|r| r.value).collect());
                frame
            })
            .collect(),
    );
    Ok(json_output::to_string(envelope)?)
}
/// 0.4.9's `_shrink_row`: a message row's text halved to `cap`'s marker, sliding to its
/// anchors when halving would drop the match; false when the marker eats the savings.
fn shrink_row(row: &mut Value, target: &str, anchors: &[&Anchor]) -> bool {
    let old = row["text"].as_str().unwrap_or_default().to_owned();
    let body = CAP_MARKER.replace(&old, "").into_owned();
    let slid = SLID_HEAD
        .find(&body)
        .map(|m| body[..m.end()].chars().count());
    let length = body.chars().count();
    let keep = slid
        .map_or(16, |head| head + 192)
        .max(((length as i64 - 1).div_euclid(2)).max(0) as usize);
    let turn = row["turn"].as_i64().unwrap_or_default();
    let expand = evidence::expand_command(target, turn, row["who"].as_str().unwrap_or("user"));
    let prior = row["omitted_chars"].as_u64().unwrap_or_default() as usize;
    let (new, omitted) = if !anchors.is_empty() && slid.is_none() {
        let (new_body, cut) = evidence::anchored_cap(&body, keep, &expand, anchors);
        let omitted = prior + cut;
        let new = if cut > 0 {
            carry_omission(&new_body, omitted, prior, &expand)
        } else {
            new_body
        };
        (new, omitted)
    } else {
        let end = body
            .char_indices()
            .nth(keep)
            .map_or(body.len(), |(at, _)| at);
        let mut cut = &body[..end];
        let window = cut.char_indices().rev().nth(199).map_or(0, |(at, _)| at);
        if cut[window..].contains(' ') {
            cut = &cut[..cut.rfind(' ').expect("a space was found")];
        }
        let omitted = prior + length - cut.chars().count();
        (
            format!("{cut} [+{} chars - {expand}]", surface::number(omitted)),
            omitted,
        )
    };
    if new.chars().count() as i64 > old.chars().count() as i64 - 8 {
        return false;
    }
    row["text"] = json!(new);
    row["omitted_chars"] = json!(omitted);
    true
}
/// 0.4.9's `_carry_omission`: a re-shrunk row's marker counts every character it no
/// longer shows, the earlier cut included.
fn carry_omission(text: &str, omitted: usize, prior: usize, expand: &str) -> String {
    if CAP_MARKER.is_match(text) {
        return format!(
            "{} [+{} chars - {expand}]",
            CAP_MARKER.replace(text, ""),
            surface::number(omitted)
        );
    }
    if prior == 0 {
        return text.to_owned();
    }
    match SNIPPET_TAIL.captures(text) {
        None => format!("{text} [+{} chars - {expand}]", surface::number(omitted)),
        Some(tail) => {
            let gap = tail
                .get(1)
                .map_or(0, |g| g.as_str().replace(',', "").parse().unwrap_or(0))
                + prior;
            format!(
                "{} [{} chars] [snippet - open: {expand}]",
                &text[..tail.get(0).expect("whole match").start()],
                surface::number(gap)
            )
        }
    }
}
/// 0.4.9's `_fit_json_payload`: the envelope and its hits within `budget` bytes, or the
/// envelope's state alone when no hit fits.
fn fit_json<'a>(
    envelope: &mut Value,
    mut hits: Vec<JsonHit<'a>>,
    budget: usize,
    page: &'a Page,
) -> Result<String> {
    let total_of = |envelope_size: usize, hits: &[JsonHit]| {
        envelope_size + hits.iter().map(JsonHit::size).sum::<usize>() + hits.len().saturating_sub(1)
    };
    envelope["hits"] = json!([]);
    let mut envelope_size = json_len(envelope)?;
    if budget == 0 || total_of(envelope_size, &hits) <= budget {
        return assemble(envelope, hits);
    }
    let original = hits.len();
    envelope["truncated"] = json!(true);
    envelope["omitted_hits"] = json!(0);
    match &mut envelope["query"] {
        Value::String(q) if q.chars().count() > 256 => {
            *q = q.chars().take(240).collect::<String>() + "…";
        }
        Value::Array(items) => {
            items.truncate(8);
            for item in items.iter_mut() {
                let text = item.as_str().unwrap_or_default();
                if text.chars().count() > 120 {
                    *item = json!(text.chars().take(120).collect::<String>() + "…");
                }
            }
        }
        _ => {}
    }
    envelope_size = json_len(envelope)?;
    // Query anchors after each hit's own: exact phrases outrank term fallbacks.
    let named: Vec<&Vec<Anchor>> = page
        .queries
        .iter()
        .zip(&page.anchors)
        .filter(|(q, _)| !q.is_empty() && !q.starts_with('@'))
        .map(|(_, anchors)| anchors)
        .collect();
    let query_anchors: Vec<&Anchor> = named
        .iter()
        .flat_map(|a| a.iter().take(1))
        .chain(named.iter().flat_map(|a| a.iter().skip(1)))
        .collect();
    for h in &mut hits {
        h.anchors.extend(query_anchors.iter().copied());
    }
    let mut total = total_of(envelope_size, &hits);
    // Long message bodies are the normal overflow: halve the longest that can still shrink.
    // A row that cannot shrink never can again, since only shrinking changes it.
    let mut longest: BinaryHeap<(usize, Reverse<usize>, Reverse<usize>)> = BinaryHeap::new();
    for (hi, h) in hits.iter().enumerate() {
        for (ri, row) in h.rows.iter().enumerate() {
            if let Some(text) = row.value["text"]
                .as_str()
                .filter(|_| row.value["kind"] == "msg")
            {
                let chars = text.chars().count();
                if chars > 48 {
                    longest.push((chars, Reverse(hi), Reverse(ri)));
                }
            }
        }
    }
    while total > budget {
        let Some((_, Reverse(hi), Reverse(ri))) = longest.pop() else {
            break;
        };
        let h = &mut hits[hi];
        if !shrink_row(&mut h.rows[ri].value, h.target, &h.anchors) {
            continue;
        }
        let row = &mut h.rows[ri];
        let size = json_len(&row.value)?;
        total = total + size - row.size;
        row.size = size;
        let chars = row.value["text"]
            .as_str()
            .unwrap_or_default()
            .chars()
            .count();
        if chars > 48 {
            longest.push((chars, Reverse(hi), Reverse(ri)));
        }
    }
    // Optional narration goes before a hit's selected exchange or tool event.
    while total > budget {
        let Some((hi, ri)) = hits
            .iter()
            .enumerate()
            .rev()
            .find_map(|(hi, h)| h.rows.iter().rposition(|r| !r.required).map(|ri| (hi, ri)))
        else {
            break;
        };
        let h = &mut hits[hi];
        let before = h.size();
        h.rows.remove(ri);
        if h.frame.get("window_truncated").is_none() {
            h.frame["window_truncated"] = json!(true);
            h.frame_size = json_len(&h.frame)?;
        }
        total = total - before + h.size();
    }
    while total > budget && !hits.is_empty() {
        hits.pop();
        envelope["omitted_hits"] = json!(original - hits.len());
        envelope_size = json_len(envelope)?;
        total = total_of(envelope_size, &hits);
    }
    if total <= budget {
        return assemble(envelope, hits);
    }
    Ok(json_output::to_string(&minimal(envelope, original))?)
}
/// What a page too small for any hit keeps of its envelope.
fn minimal(out: &Value, original: usize) -> Value {
    let fields = |name: &str, keys: &[&str]| {
        let Some(map) = out.get(name).and_then(Value::as_object) else {
            return Value::Null;
        };
        let mut picked = Map::new();
        for key in keys {
            if let Some(item) = map.get(*key) {
                let item = match item {
                    Value::String(s) => json!(surface::prefix(s, 96)),
                    other => other.clone(),
                };
                picked.insert((*key).into(), item);
            }
        }
        Value::Object(picked)
    };
    let query = match out.get("query") {
        Some(Value::Array(items)) => json!(items
            .iter()
            .take(4)
            .map(|item| surface::prefix(item.as_str().unwrap_or_default(), 48))
            .collect::<Vec<_>>()),
        other => json!(surface::prefix(
            other.and_then(Value::as_str).unwrap_or_default(),
            96
        )),
    };
    let mut minimal = json!({
        "query": query,
        "engine": surface::prefix(out["engine"].as_str().unwrap_or_default(), 64),
        "hits": [],
        "error": fields("error", &["code", "reason"]),
        "self_exclusion": fields("self_exclusion", &["active", "reason", "scope", "session", "excluded_hits", "excluded_hits_known", "postfilter_excluded_hits", "from_turn"]),
        "freshness": fields("freshness", &["state", "failing", "checked", "may_be_stale", "code", "reason", "consecutive_failures"]),
        "semantic_coverage": fields("semantic_coverage", &["indexed", "total", "pending", "fraction", "complete", "order"]),
        "truncated": true,
        "omitted_hits": original,
    });
    if out.get("tools_excluded").is_some_and(Value::is_object) {
        minimal["tools_excluded"] = fields("tools_excluded", &["reason"]);
    }
    minimal
}
#[cfg(test)]
mod tests {
    use super::*;
    // Expected values were produced by agrep 0.4.9's own `_over_budget_trailer`,
    // `_cap_line_bytes`, `_tiny_block` and `_fit_text_payload` (py/recall.py).
    #[test]
    fn trailers_line_caps_and_payload_cuts_match_0_4_9() {
        let handles: Vec<String> = ["@a:1.ffff", "@b:2.eeee", "@c:3.dddd"]
            .map(Into::into)
            .to_vec();
        let named = "[+3 hit(s) over budget - @a:1.ffff @b:2.eeee @c:3.dddd]";
        assert_eq!(over_budget_trailer(3, &handles, 0), named);
        assert_eq!(over_budget_trailer(3, &handles, 60), named);
        assert_eq!(
            over_budget_trailer(3, &handles, 30),
            "[+3 hit(s) over budget]"
        );
        let line =
            "    4 user: some words here [+1,234 chars - agrep around abc 4 -C 0 --max-chars 0]";
        let pointer = "[+1,234 chars - agrep around abc 4 -C 0 --max-chars 0]";
        assert_eq!(cap_line_bytes(line, 70), format!("    4 user:… {pointer}"));
        assert_eq!(cap_line_bytes(line, 40), pointer);
        assert_eq!(cap_line_bytes(line, 90), line);
        let head = "── @h:1.abcd · pi · x";
        let row = "    1 user: hello there [+5 chars - agrep around h 1 -C 0 --max-chars 0]";
        assert_eq!(tiny_block(head, &[row.into()], 40).as_deref(), Some(head));
        assert_eq!(
            tiny_block(head, &["    1 user: hello there".into()], 26),
            None
        );
        let blocks = [
            format!("── @a:1.abcd\n    1 user: {}", "alpha ".repeat(30)),
            format!("── @b:2.abcd\n    2 user: {}", "beta ".repeat(30)),
        ];
        let text = blocks.join("\n\n");
        let ends = vec![
            (blocks[0].len(), "@a:1.abcd".to_owned()),
            (
                blocks[0].len() + 2 + blocks[1].len(),
                "@b:2.abcd".to_owned(),
            ),
        ];
        let over = "[+1 hit(s) over budget - @c:3.abcd]";
        assert_eq!(
            fit_text(&text, 250, &ends, "", Some(150)),
            format!("── @a:1.abcd\n    1 user: {}a\n[output truncated to --budget - rest at @a:1.abcd]\n\n[+1 hit(s) over budget - @b:2.abcd]", "alpha ".repeat(22))
        );
        assert_eq!(
            fit_text(&text, 120, &ends, over, Some(90)),
            "── @a:1.abcd\n    1 user: alpha alpha alp\n[output truncated to --budget - rest at @a:1.abcd]\n\n[+2 hit(s) over budget]"
        );
        assert_eq!(
            fit_text(&text, 70, &ends, over, Some(50)),
            "[+3 hit(s) over budget - @a:1.abcd @b:2.abcd @c:3.abcd]"
        );
    }
}
