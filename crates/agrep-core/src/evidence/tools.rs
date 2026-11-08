//! Tool match previews, markers and collapsed tool runs.
use super::py_space;
use super::{one_line, text::Indexed, Event, Row};
use crate::surface;
use std::collections::BTreeSet;

impl Row {
    /// Ingest keeps at most `REPLY_CAP` characters of a reply; 0.4.9 flagged a reply
    /// truncated exactly when its source count passed it.
    pub fn reply_truncated(&self) -> bool {
        self.who == "agent" && self.source_chars > crate::ingest::REPLY_CAP
    }
}
impl Event {
    /// Ingest keeps at most `EVENT_CAP` characters of a tool input or output; 0.4.9
    /// flagged an event truncated exactly when a source count passed it.
    pub fn input_truncated(&self) -> bool {
        self.input_chars > crate::ingest::EVENT_CAP
    }
    pub fn output_truncated(&self) -> bool {
        self.output_chars > crate::ingest::EVENT_CAP
    }
    /// The output's character span within `text`.
    fn output_bounds(&self) -> Option<(usize, usize)> {
        let start = self.raw["output_start"].as_u64()? as usize;
        let end = self.text.chars().count();
        (start < end).then_some((start, end))
    }
    /// The UTF-8 size of the output 0.4.9 kept. The index drops trailing whitespace from
    /// a whole output, whose source byte count is then the kept size.
    pub fn kept_output_bytes(&self) -> usize {
        if self.output_truncated() {
            self.output.len()
        } else {
            self.output_bytes.unwrap_or(self.output.len())
        }
    }
}
/// Whether a tool event is the one a hit selected, the hit's span within its output, and
/// a match preview (0.4.9's `_selected_tool_match`).
#[derive(Clone, Debug, Default)]
pub struct ToolMatch {
    pub selected: bool,
    pub output_span: Option<(usize, usize)>,
    pub preview: Option<String>,
}
/// `identity` and `span` are a tool hit's event selector and character span.
pub fn selected_tool_match(
    event: &Event,
    identity: Option<&str>,
    span: Option<(usize, usize)>,
) -> ToolMatch {
    if event.text.is_empty() || identity != Some(event.identity.as_str()) {
        return ToolMatch::default();
    }
    let selected = ToolMatch {
        selected: true,
        ..ToolMatch::default()
    };
    let text = Indexed::new(&event.text);
    let Some((start, end)) = span.filter(|(start, end)| start < end && *end <= text.len()) else {
        return selected;
    };
    let bounds = event.output_bounds();
    let preview = payload_snip_at(&text, start, end, 60, bounds)
        .or_else(|| payload_snip_at(&text, start, end, 60, None));
    // The event's output is exactly the indexed payload, so a span inside it only shifts.
    let output_span = bounds
        .filter(|(low, high)| *low <= start && end <= *high)
        .map(|(low, _)| (start - low, end - low));
    ToolMatch {
        selected: true,
        output_span,
        preview,
    }
}
/// 0.4.9's `_one_line_with_span`: `one_line(value)` and the span's new bounds in it.
fn one_line_with_span(value: &str, start: usize, end: usize) -> Option<(String, usize, usize)> {
    let chars: Vec<char> = value.chars().collect();
    if end < start || end > chars.len() {
        return None;
    }
    let mut out = String::with_capacity(value.len());
    let mut length = 0;
    let mut boundaries = vec![0; chars.len() + 1];
    let mut index = 0;
    while index < chars.len() {
        boundaries[index] = length;
        if py_space(chars[index]) {
            let run = index;
            while index < chars.len() && py_space(chars[index]) {
                index += 1;
            }
            if length > 0 && index < chars.len() {
                out.push(' ');
                length += 1;
            }
            for boundary in &mut boundaries[run + 1..=index] {
                *boundary = length;
            }
            continue;
        }
        out.push(chars[index]);
        length += 1;
        index += 1;
        boundaries[index] = length;
    }
    Some((out, boundaries[start], boundaries[end]))
}
/// 0.4.9's `_quoted_value_bounds`: the JSON-like string value holding character `at`,
/// without its quotes; never a key.
fn quoted_value_bounds(text: &Indexed, at: usize) -> Option<(usize, usize)> {
    let chars: Vec<char> = text.text.chars().collect();
    let mut quote = None;
    let mut escaped = false;
    for (index, c) in chars.iter().enumerate().take(at + 1) {
        if escaped {
            escaped = false;
        } else if *c == '\\' {
            escaped = true;
        } else if *c == '"' {
            quote = if quote.is_some() { None } else { Some(index) };
        }
    }
    let quote = quote?;
    let mut end = None;
    for (index, c) in chars.iter().enumerate().skip((at + 1).max(quote + 1)) {
        if escaped {
            escaped = false;
        } else if *c == '\\' {
            escaped = true;
        } else if *c == '"' {
            end = Some(index);
            break;
        }
    }
    let end = end?;
    let after = text.slice(end + 1, chars.len());
    if after.trim_start_matches(py_space).starts_with(':') {
        return None;
    }
    let before = text.slice(0, quote).trim_end_matches(py_space);
    if before.chars().last().is_some_and(|c| !":,[{".contains(c)) {
        return None;
    }
    Some((quote + 1, end))
}
/// 0.4.9's `_quoted_value_ranges`: every JSON-like quoted value, never a key.
fn quoted_value_ranges(chars: &[char]) -> Vec<(usize, usize)> {
    let mut values = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        if chars[index] != '"' {
            index += 1;
            continue;
        }
        let quote = index;
        index += 1;
        let mut escaped = false;
        while index < chars.len() {
            let c = chars[index];
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                break;
            }
            index += 1;
        }
        if index >= chars.len() {
            break;
        }
        let end = index;
        let after = chars[end + 1..].iter().find(|c| !py_space(**c));
        let before = chars[..quote].iter().rev().find(|c| !py_space(**c));
        let is_key = after == Some(&':');
        let is_value_position = before.is_none_or(|c| ":,[{".contains(*c));
        if !is_key && is_value_position {
            values.push((quote + 1, end));
        }
        index = end + 1;
    }
    values
}
/// 0.4.9's `payload_snip_at`: a one-line window of `text` around a match, spent inside the
/// payload or quoted value that holds it rather than on keys. `None` where 0.4.9 raised.
fn payload_snip_at(
    text: &Indexed,
    start: usize,
    end: usize,
    pad: usize,
    payload: Option<(usize, usize)>,
) -> Option<String> {
    let n = text.len();
    if end < start || end > n {
        return None;
    }
    if let Some((low, high)) = payload {
        if low >= high || high > n || !(low >= end || (low <= start && end <= high)) {
            return None;
        }
    }
    let bounds = if payload.is_some() {
        None
    } else {
        quoted_value_bounds(text, start)
    };
    let (left, mut source_end, mut source, mut normalized) = (0, n, text.text.to_owned(), false);
    let mut left = left;
    if let Some((low, high)) = payload {
        if low <= start {
            (left, source_end, source) = (low, high, text.slice(low, high).to_owned());
        } else {
            left = start;
            source = text.slice(start, end).to_owned();
            let body = one_line(text.slice(low, high));
            if !body.is_empty() {
                source += " · ";
                source += &body;
            }
            source_end = high;
            normalized = !text.slice(start, end).chars().any(py_space);
        }
    } else if let Some((low, high)) = bounds.filter(|(_, high)| end <= *high) {
        left = start.min(low);
        source_end = high;
        source = text.slice(left, high).to_owned();
    }
    let (mut local_start, mut local_end) = (start - left, end - left);
    if payload.is_none() && bounds.is_some() && source_end - end < pad {
        let chars: Vec<char> = text.text.chars().collect();
        if let Some((low, high)) = quoted_value_ranges(&chars)
            .into_iter()
            .rev()
            .find(|(low, high)| *low >= source_end && !one_line(text.slice(*low, *high)).is_empty())
        {
            source += " · ";
            source += &one_line(text.slice(low, high));
            source_end = high;
        }
    }
    if !(payload.is_some() && normalized) {
        (source, local_start, local_end) = one_line_with_span(&source, local_start, local_end)?;
    }
    let source = Indexed::new(&source);
    let len = source.len();
    let budget = (local_end - local_start) + 2 * pad;
    let mut a = local_start.saturating_sub(pad);
    let mut b = len.min(local_end + pad);
    let mut missing = budget.saturating_sub(b - a);
    if missing > 0 {
        let take = missing.min(len - b);
        b += take;
        missing -= take;
    }
    a -= missing.min(a);
    Some(format!(
        "{}{}{}",
        if left > 0 || a > 0 { "…" } else { "" },
        source.slice(a, b),
        if source_end < n || b < len { "…" } else { "" }
    ))
}
/// 0.4.9's `_match_centered_preview`: at most `cap` characters of `value` around a span.
fn match_centered_preview(value: &str, start: usize, end: usize, cap: usize) -> String {
    let chars = Indexed::new(value);
    let n = chars.len();
    if cap == 0 {
        return String::new();
    }
    if n <= cap {
        return value.to_owned();
    }
    let left_marker = usize::from(start > 0);
    let right_marker = usize::from(end < n);
    let room = cap.saturating_sub(left_marker + right_marker);
    if room == 0 {
        let markers = "…".repeat(left_marker + right_marker);
        return markers.chars().take(cap).collect();
    }
    let size = end.saturating_sub(start);
    let (a, b) = if size >= room {
        (start, end.min(start + room))
    } else {
        let extra = room - size;
        let mut left = start.min(extra / 2);
        let mut right = (n - end).min(extra - left);
        let mut leftover = extra - left - right;
        if leftover > 0 {
            let take = (start - left).min(leftover);
            left += take;
            leftover -= take;
        }
        if leftover > 0 {
            right += (n - end - right).min(leftover);
        }
        (start - left, end + right)
    };
    format!(
        "{}{}{}",
        if a > 0 { "…" } else { "" },
        chars.slice(a, b),
        if b < n { "…" } else { "" }
    )
}
/// Python's `str.splitlines` boundaries.
fn py_line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\x0b'
            | '\x0c'
            | '\x1c'
            | '\x1d'
            | '\x1e'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}
/// 0.4.9's `tool_output_preview` at 160 characters: the output's first nonblank line, or
/// the line around `span` (output characters) centered on it; and the source byte size
/// it could vouch for.
fn tool_output_preview(e: &Event, span: Option<(usize, usize)>) -> (String, Option<usize>) {
    const PREVIEW: usize = 160;
    let n = e.output.chars().count();
    let shown = match span.filter(|(start, end)| start < end && *end <= n) {
        Some((start, end)) => one_line_with_span(&e.output, start, end)
            .map(|(line, start, end)| match_centered_preview(&line, start, end, PREVIEW))
            .unwrap_or_default(),
        None => e
            .output
            .split(py_line_break)
            .find(|line| !line.trim_matches(py_space).is_empty())
            .map(|line| one_line(line).chars().take(PREVIEW).collect())
            .unwrap_or_default(),
    };
    let bytes = if !e.output_truncated() {
        Some(e.kept_output_bytes())
    } else {
        // A canonical excerpt is the first EVENT_CAP characters and an ellipsis; its
        // source size must be reachable from the kept prefix.
        let cap = crate::ingest::EVENT_CAP;
        let canonical = n == cap + 1 && e.output.ends_with('…');
        let retained = e.output.len().saturating_sub('…'.len_utf8());
        let remaining = e.output_chars - cap;
        e.output_bytes
            .filter(|b| canonical && (retained + remaining..=retained + 4 * remaining).contains(b))
    };
    (shown, bytes)
}
/// 0.4.9's structural control events and what a disclosure calls each, in the order it
/// lists them. Each renders as a line of its own and never counts as a tool call.
pub const MARKERS: [(&str, &str); 3] = [
    ("api_error", "API error"),
    ("compacted", "compaction"),
    ("task_notification", "task notification"),
];
const BAD: &str = "\x1b[1;31m";
pub(super) const HEAD: &str = "\x1b[1;36m";
pub(super) const DIM: &str = "\x1b[2m";
pub(super) const YELLOW: &str = "\x1b[33m";
pub(super) const CYAN: &str = "\x1b[36m";
pub(super) const RESET: &str = "\x1b[0m";
impl Event {
    pub fn is_marker(&self) -> bool {
        self.kind == "control" && MARKERS.iter().any(|(name, _)| *name == self.name)
    }
}
/// 0.4.9's `_marker_line`.
fn marker_line(e: &Event, color: bool) -> String {
    let raw = one_line(&e.input);
    let body = match e.name.as_str() {
        "api_error" => {
            let mut text = surface::safe(&raw, false);
            if text.chars().count() > 120 {
                text = text.chars().take(120).collect::<String>() + "…";
            }
            let label = if text.to_lowercase().starts_with("api error") {
                ""
            } else {
                "API error: "
            };
            format!("✗ {label}{text}")
                .trim_end_matches([' ', ':'])
                .to_owned()
        }
        "compacted" => "· context compacted".to_owned(),
        _ => {
            let status = raw
                .split(' ')
                .map(|part| part.split_once('=').unwrap_or((part, "")))
                .find(|(key, value)| *key == "status" && !value.is_empty())
                .map_or(String::new(), |(_, value)| surface::safe(value, false));
            format!(
                "↳ background task {}",
                if status.is_empty() {
                    "notification"
                } else {
                    &status
                }
            )
        }
    };
    if color {
        let tone = if e.name == "api_error" { BAD } else { DIM };
        format!("  {tone}{body}{RESET}")
    } else {
        format!("  {body}")
    }
}
/// 0.4.9's `_tool_line` without color or output lines: a tool event's glyph, name, input
/// and output preview with its size, or the selected match where it fell outside the output.
pub fn tool_line(e: &Event, output_span: Option<(usize, usize)>, preview: Option<&str>) -> String {
    if e.is_marker() {
        return marker_line(e, false);
    }
    format!("  {}", tool_body(e, 0, output_span, preview))
}
/// The body of 0.4.9's `_tool_line`; with `out_cap` set the output follows on lines of its
/// own instead of as a preview.
fn tool_body(
    e: &Event,
    out_cap: usize,
    output_span: Option<(usize, usize)>,
    preview: Option<&str>,
) -> String {
    let mut input = surface::safe(&one_line(&e.input), false);
    if input.chars().count() > 120 {
        input = input.chars().take(120).collect::<String>() + "…";
    }
    if e.input_truncated() {
        input += &format!(
            " [source input truncated at ingest; {} chars]",
            surface::number(e.input_chars)
        );
    }
    let centered = surface::safe(preview.unwrap_or_default(), false);
    match e.kind.as_str() {
        "subagent_start" => format!(
            "⇒ subagent {}",
            if centered.is_empty() {
                &input
            } else {
                &centered
            }
        ),
        "subagent_result" => {
            let out = surface::safe(
                &one_line(if e.output.is_empty() {
                    &e.name
                } else {
                    &e.output
                }),
                false,
            );
            let mut result: String = out.chars().take(120).collect();
            if out.chars().count() > 120 {
                result += "…";
            }
            if e.output_truncated() {
                result += &format!(
                    " [source result truncated at ingest; {} chars]",
                    surface::number(e.output_chars)
                );
            }
            format!(
                "⇐ subagent result {}",
                if centered.is_empty() {
                    &result
                } else {
                    &centered
                }
            )
        }
        _ => {
            let mark = if e.ok == Some(false) {
                "✗ FAILED"
            } else {
                "⚙"
            };
            let (shown, bytes) = tool_output_preview(e, output_span);
            let out = if out_cap > 0 || shown.is_empty() {
                String::new()
            } else {
                format!(" {}", surface::safe(&shown, false))
            };
            let size = match bytes {
                Some(bytes) => format!(" ({}B)", surface::number(bytes)),
                None if e.output_truncated() => format!(
                    " ({}c source; indexed excerpt)",
                    surface::number(e.output_chars)
                ),
                None if e.output_chars > 0 => format!(" ({}c)", surface::number(e.output_chars)),
                None => String::new(),
            };
            let name = surface::safe(&e.name, false);
            if !centered.is_empty() && output_span.is_none() {
                if centered.starts_with(&name) {
                    format!("{mark} {centered}{size}")
                } else {
                    format!("{mark} {name} {centered}{size}")
                }
            } else {
                format!("{mark} {name} {input}{out}{size}")
            }
        }
    }
}
/// 0.4.9's `_tool_line` in `around`: the tool line, colored, and with `out_cap` the
/// output's first `out_cap` characters on indented lines below it.
fn around_tool_line(e: &Event, color: bool, out_cap: usize, m: &ToolMatch) -> String {
    if e.is_marker() {
        return marker_line(e, color);
    }
    let body = tool_body(e, out_cap, m.output_span, m.preview.as_deref());
    let mut line = if color {
        let tone = if e.ok == Some(false) { BAD } else { DIM };
        format!("  {tone}{body}{RESET}")
    } else {
        format!("  {body}")
    };
    if out_cap > 0 && !e.output.is_empty() {
        let kept: String = e.output.chars().take(out_cap).collect();
        let more = e.output.chars().count() - kept.chars().count();
        let out = surface::safe(&kept, true);
        // Python's `splitlines`: the sanitizer left `\n` as the only line break.
        let lines = out.strip_suffix('\n').unwrap_or(&out).split('\n');
        line += "\n";
        line += &lines
            .map(|l| format!("    {l}"))
            .collect::<Vec<_>>()
            .join("\n");
        if more > 0 {
            line += &format!(" [+{} indexed chars]", surface::number(more));
        }
        if e.output_truncated() {
            line += " [source truncated at ingest]";
        }
    }
    line
}
const TOOL_RUN_MIN: usize = 5;
const TOOL_DETAIL_MAX: usize = 16;
/// 0.4.9's `_tool_run_summary`: a run of successful calls folded into one line.
fn tool_run_summary(events: &[&Event], color: bool, expand: &str) -> String {
    let mut by_name: Vec<(&str, usize)> = Vec::new();
    for e in events {
        match by_name.iter_mut().find(|(name, _)| *name == e.name) {
            Some((_, count)) => *count += 1,
            None => by_name.push((&e.name, 1)),
        }
    }
    by_name.sort_by_key(|(_, count)| std::cmp::Reverse(*count));
    let top = &by_name[..by_name.len().min(3)];
    let rest = events.len() - top.iter().map(|(_, count)| count).sum::<usize>();
    let body = format!(
        "⚙ {} tool calls ({}{}) - {expand} shows each",
        events.len(),
        top.iter()
            .map(|(name, count)| format!("{count} {}", surface::safe(name, false)))
            .collect::<Vec<_>>()
            .join(" · "),
        if rest > 0 {
            format!(" · {rest} other")
        } else {
            String::new()
        }
    );
    dim_line(&body, color)
}
fn dim_line(body: &str, color: bool) -> String {
    if color {
        format!("  {DIM}{body}{RESET}")
    } else {
        format!("  {body}")
    }
}
/// 0.4.9's `_spread_tool_groups`: at most `limit` items sampled across the whole run.
fn spread<T: Copy>(items: &[T], limit: usize) -> Vec<T> {
    if limit == 0 || items.is_empty() {
        return Vec::new();
    }
    if items.len() <= limit {
        return items.to_vec();
    }
    if limit == 1 {
        return vec![items[0]];
    }
    let picked: BTreeSet<usize> = (0..limit)
        .map(|offset| {
            ((offset * (items.len() - 1)) as f64 / (limit - 1) as f64).round_ties_even() as usize
        })
        .collect();
    picked.into_iter().map(|index| items[index]).collect()
}
/// 0.4.9's `_tool_rows`: each rendered tool call's index in `tools` and its line, `None`
/// for a collapse summary. Long tails keep failures and the selected call visible.
fn tool_rows(
    tools: &[&Event],
    color: bool,
    out_cap: usize,
    expand: &str,
    collapse: bool,
    claim: (Option<&str>, Option<(usize, usize)>),
) -> Vec<(Option<usize>, String)> {
    let selected = tools.iter().enumerate().find_map(|(at, e)| {
        let m = selected_tool_match(e, claim.0, claim.1);
        m.selected.then_some((at, m))
    });
    let is_selected = |at: usize| selected.as_ref().is_some_and(|(s, _)| *s == at);
    let line = |at: usize| {
        let m = selected
            .as_ref()
            .filter(|(s, _)| *s == at)
            .map(|(_, m)| m.clone())
            .unwrap_or_default();
        around_tool_line(tools[at], color, out_cap, &m)
    };
    let every = || (0..tools.len()).map(|at| (Some(at), line(at))).collect();
    let plain: Vec<usize> = (0..tools.len())
        .filter(|&at| {
            !matches!(
                tools[at].kind.as_str(),
                "subagent_start" | "subagent_result"
            ) && tools[at].ok == Some(true)
                && !is_selected(at)
        })
        .collect();
    let plain_events: Vec<&Event> = plain.iter().map(|&at| tools[at]).collect();
    if !collapse || out_cap > 0 || (plain.len() < TOOL_RUN_MIN && tools.len() <= TOOL_DETAIL_MAX) {
        return every();
    }
    let details: Vec<usize> = (0..tools.len()).filter(|at| !plain.contains(at)).collect();
    if plain.len() >= TOOL_RUN_MIN && details.len() < TOOL_DETAIL_MAX {
        let mut lines: Vec<_> = details.iter().map(|&at| (Some(at), line(at))).collect();
        lines.push((None, tool_run_summary(&plain_events, color, expand)));
        return lines;
    }
    let groups = group_details(tools, &details, is_selected);
    let chosen = choose_details(tools, &groups, is_selected);
    let failed = |g: &(usize, usize, usize)| tools[g.0].ok == Some(false);
    let mut lines: Vec<(Option<usize>, String)> = chosen
        .iter()
        .map(|&g| {
            let (at, count, _) = groups[g];
            let mut text = line(at);
            if count > 1 {
                text += &format!(" [×{} identical]", surface::number(count));
            }
            (Some(at), text)
        })
        .collect();
    let collapsed = tools.len() - chosen.len();
    let collapsed_failed: usize = (0..groups.len())
        .filter(|&g| failed(&groups[g]))
        .map(|g| groups[g].1 - usize::from(chosen.contains(&g)))
        .sum();
    if plain.len() >= TOOL_RUN_MIN && collapsed == plain.len() && collapsed_failed == 0 {
        lines.push((None, tool_run_summary(&plain_events, color, expand)));
    } else {
        let failed = if collapsed_failed > 0 {
            format!(" ({} failed)", surface::number(collapsed_failed))
        } else {
            String::new()
        };
        let body = format!(
            "⚙ {} tool event details collapsed{failed} - {expand} shows each",
            surface::number(collapsed)
        );
        lines.push((None, dim_line(&body, color)));
    }
    lines
}
/// 0.4.9's `_tool_block`: one turn's tool lines with each marker in place among them and
/// any collapse summary after the last tool call.
pub(super) fn tool_block(
    events: &[&Event],
    color: bool,
    out_cap: usize,
    expand: &str,
    collapse: bool,
    claim: (Option<&str>, Option<(usize, usize)>),
) -> Vec<String> {
    let tools: Vec<usize> = (0..events.len())
        .filter(|&at| !events[at].is_marker())
        .collect();
    let calls: Vec<&Event> = tools.iter().map(|&at| events[at]).collect();
    let summary_at = tools.last().map_or(-1, |&at| 2 * at as i64 + 1);
    let mut placed: Vec<(i64, String)> = tool_rows(&calls, color, out_cap, expand, collapse, claim)
        .into_iter()
        .map(|(at, line)| (at.map_or(summary_at, |at| 2 * tools[at] as i64), line))
        .collect();
    placed.extend(
        (0..events.len())
            .filter(|&at| events[at].is_marker())
            .map(|at| (2 * at as i64, marker_line(events[at], color))),
    );
    placed.sort_by_key(|(at, _)| *at);
    placed.into_iter().map(|(_, line)| line).collect()
}

fn group_details(
    tools: &[&Event],
    details: &[usize],
    is_selected: impl Fn(usize) -> bool,
) -> Vec<(usize, usize, usize)> {
    // Groups of identical evidence, in first-seen order: (call, count, order).
    let mut groups: Vec<(usize, usize, usize)> = Vec::new();
    for (order, &at) in details.iter().enumerate() {
        let e = tools[at];
        let same = |g: &&mut (usize, usize, usize)| {
            let o = tools[g.0];
            !is_selected(g.0)
                && (&o.kind, &o.name, &o.input, &o.output, o.ok)
                    == (&e.kind, &e.name, &e.input, &e.output, e.ok)
                && (o.input_chars, o.output_chars, o.output_bytes)
                    == (e.input_chars, e.output_chars, e.output_bytes)
        };
        match groups.iter_mut().find(same).filter(|_| !is_selected(at)) {
            Some(group) => group.1 += 1,
            None => groups.push((at, 1, order)),
        }
    }
    groups
}

fn choose_details(
    tools: &[&Event],
    groups: &[(usize, usize, usize)],
    is_selected: impl Fn(usize) -> bool,
) -> Vec<usize> {
    let failed = |g: &(usize, usize, usize)| tools[g.0].ok == Some(false);
    let mut required: Vec<usize> = (0..groups.len())
        .filter(|&g| is_selected(groups[g].0))
        .collect();
    let singletons: Vec<usize> = (0..groups.len())
        .filter(|&g| failed(&groups[g]) && groups[g].1 == 1 && !is_selected(groups[g].0))
        .collect();
    if singletons.len() == 1 {
        required.push(singletons[0]);
    }
    let remaining: Vec<usize> = (0..groups.len())
        .filter(|g| !required.contains(g))
        .collect();
    let failures: Vec<usize> = remaining
        .iter()
        .copied()
        .filter(|&g| failed(&groups[g]))
        .collect();
    let other: Vec<usize> = remaining
        .iter()
        .copied()
        .filter(|&g| !failed(&groups[g]))
        .collect();
    let mut slots = TOOL_DETAIL_MAX.saturating_sub(required.len());
    let mut chosen = required;
    let picked = spread(&failures, slots);
    slots -= picked.len();
    chosen.extend(picked);
    chosen.extend(spread(&other, slots));
    chosen.sort_by_key(|&g| groups[g].2);
    chosen
}
