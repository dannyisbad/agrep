//! Character-indexed anchors, snippets, caps and indexed prose coverage.
use crate::{store, surface};
use anyhow::Result;
use rusqlite::{params, Connection};
use std::collections::BTreeMap;

/// Connectives that carry no content in a natural-language query.
const STOP: &[&str] = &[
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could", "did", "do",
    "does", "for", "from", "had", "has", "have", "how", "i", "if", "in", "is", "it", "its", "me",
    "my", "not", "of", "on", "or", "our", "should", "so", "that", "the", "their", "them", "they",
    "this", "these", "those", "to", "was", "we", "were", "what", "when", "where", "which", "who",
    "why", "will", "with", "would", "you", "your", "whats", "what's", "im", "i'm", "dont", "don't",
    "isnt", "isn't", "didnt", "didn't",
];
/// A query's content words: tokens split at whitespace, `-` and `_`, without surrounding
/// ASCII punctuation, at least two characters long and not connectives.
pub fn content_terms(query: &str) -> Vec<String> {
    query
        .split(|c: char| c.is_whitespace() || c == '-' || c == '_')
        .map(|t| t.trim_matches(|c: char| c.is_ascii_punctuation()))
        .filter(|t| t.chars().count() >= 2 && !STOP.contains(&t.to_lowercase().as_str()))
        .map(str::to_owned)
        .collect()
}
/// Python's `str.isspace`, which 0.4.9 split and stripped on: Unicode White_Space and the
/// information separators U+001C-U+001F.
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
/// `text`'s words joined by single spaces: Python's `" ".join(text.split())`.
pub fn one_line(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for word in text.split(py_space).filter(|word| !word.is_empty()) {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(word);
    }
    out
}
/// A string addressed by character, as 0.4.9's Python slices it.
pub(crate) struct Indexed<'a> {
    pub(crate) text: &'a str,
    starts: Vec<usize>,
}
impl<'a> Indexed<'a> {
    pub(crate) fn new(text: &'a str) -> Self {
        let mut starts: Vec<usize> = text.char_indices().map(|(at, _)| at).collect();
        starts.push(text.len());
        Self { text, starts }
    }
    pub(crate) fn len(&self) -> usize {
        self.starts.len() - 1
    }
    fn byte(&self, index: usize) -> usize {
        self.starts[index.min(self.len())]
    }
    /// `text[a:b]`, clamped as Python clamps.
    pub(crate) fn slice(&self, a: usize, b: usize) -> &'a str {
        let (a, b) = (self.byte(a), self.byte(b));
        if a < b {
            &self.text[a..b]
        } else {
            ""
        }
    }
    /// The character index of a byte offset on a character boundary.
    pub(crate) fn index(&self, byte: usize) -> usize {
        self.starts.partition_point(|start| *start < byte)
    }
    /// Python's `text.find(needle, from)` for a one-character needle.
    pub(super) fn find(&self, needle: char, from: usize) -> Option<usize> {
        let rest = self.slice(from, self.len());
        rest.find(needle)
            .map(|at| from.min(self.len()) + rest[..at].chars().count())
    }
}
/// The words of a query and its content terms as 0.4.9's `_anchor_patterns` compiled them:
/// the phrase with flexible whitespace, then every content term of three or more
/// characters, longest first, as one alternation.
pub struct Anchor {
    regex: regex::Regex,
    /// Term alternatives in group order; zero for the phrase.
    terms: usize,
}
/// Python's `\s`.
const PY_SPACE_CLASS: &str = r"[\s\x1C-\x1F]";
pub fn anchor_patterns(query: &str) -> Vec<Anchor> {
    let mut out = Vec::new();
    let words: Vec<String> = query
        .split(py_space)
        .filter(|word| !word.is_empty())
        .map(regex::escape)
        .collect();
    if !words.is_empty() {
        let phrase = words.join(&format!("{PY_SPACE_CLASS}+"));
        if let Ok(regex) = regex::Regex::new(&format!("(?i)(?P<agrep_phrase>{phrase})")) {
            out.push(Anchor { regex, terms: 0 });
        }
    }
    let mut terms: Vec<String> = content_terms(query)
        .into_iter()
        .filter(|term| term.chars().count() >= 3)
        .map(|term| term.to_lowercase())
        .collect();
    terms.sort_by(|a, b| {
        b.chars()
            .count()
            .cmp(&a.chars().count())
            .then_with(|| a.cmp(b))
    });
    terms.dedup();
    if !terms.is_empty() {
        let choices: Vec<String> = terms
            .iter()
            .enumerate()
            .map(|(index, term)| format!("(?P<agrep_term_{index}>{})", regex::escape(term)))
            .collect();
        if let Ok(regex) = regex::Regex::new(&format!("(?i){}", choices.join("|"))) {
            out.push(Anchor {
                regex,
                terms: terms.len(),
            });
        }
    }
    out
}
/// The first match of the first anchor that matches, as a character span.
fn first_anchor_span(text: &Indexed, anchors: &[&Anchor]) -> Option<(usize, usize)> {
    anchors
        .iter()
        .find_map(|anchor| anchor.regex.find(text.text))
        .map(|m| (text.index(m.start()), text.index(m.end())))
}
/// One whitespace-collapsed line of `text` around the first match of `anchors`, padded by
/// 60 characters; else around such a match in `fallback`; else `text`'s first 160
/// characters (0.4.9's `_weak_line` snippet).
pub fn weak_snippet(text: &str, fallback: &str, anchors: &[&Anchor]) -> String {
    let flat = one_line(text);
    let mut chars = Indexed::new(&flat);
    let mut span = first_anchor_span(&chars, anchors);
    let other = one_line(fallback);
    if span.is_none() && !fallback.is_empty() {
        let indexed = Indexed::new(&other);
        if let Some(found) = first_anchor_span(&indexed, anchors) {
            (chars, span) = (indexed, Some(found));
        }
    }
    let n = chars.len();
    let Some((start, end)) = span else {
        let head = chars.slice(0, 160).to_owned();
        return if n > 160 { head + "…" } else { head };
    };
    let (a, b) = (start.saturating_sub(60), (end + 60).min(n));
    one_line(&format!(
        "{}{}{}",
        if a > 0 { "…" } else { "" },
        chars.slice(a, b),
        if b < n { "…" } else { "" }
    ))
}
/// A session's indexed prose before `before` (every turn when `None`): its character
/// count, and each turn holding at least one of `terms` (lowercase substrings) with the
/// turn's latest timestamp and how many distinct terms it holds.
pub struct Coverage {
    pub chars: usize,
    pub turns: Vec<(i64, i64, u32)>,
}
pub fn coverage(
    c: &Connection,
    session: &str,
    before: Option<i64>,
    terms: &[String],
) -> Result<Coverage> {
    let mut q = c.prepare(
        "SELECT position,ts,text FROM prose WHERE visible=1 AND session=? AND position<? ORDER BY position",
    )?;
    let mut chars = 0;
    let mut turns: BTreeMap<i64, (i64, u64)> = BTreeMap::new();
    for row in q.query_map(params![session, before.unwrap_or(i64::MAX)], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, Vec<u8>>(2)?,
        ))
    })? {
        let (turn, ts, bytes) = row?;
        let text = store::decode("prose", bytes)?;
        chars += text.chars().count();
        let entry = turns.entry(turn).or_insert((ts, 0));
        entry.0 = entry.0.max(ts);
        if !terms.is_empty() {
            let low = text.to_lowercase();
            for (bit, term) in terms.iter().enumerate().take(64) {
                if low.contains(term.as_str()) {
                    entry.1 |= 1 << bit;
                }
            }
        }
    }
    Ok(Coverage {
        chars,
        turns: turns
            .into_iter()
            .filter(|(_, (_, mask))| *mask != 0)
            .map(|(turn, (ts, mask))| (turn, ts, mask.count_ones()))
            .collect(),
    })
}
/// 0.4.9's `_cap`: the first `limit` characters, cut back to their last space when it
/// falls in the window Python's `cut[limit - 200:]` names, and a marker counting the
/// omitted characters with the command that prints them. A zero limit keeps everything.
pub fn cap(text: &str, limit: usize, command: &str) -> (String, usize) {
    if limit == 0 {
        return (text.to_owned(), 0);
    }
    let Some((end, _)) = text.char_indices().nth(limit) else {
        return (text.to_owned(), 0);
    };
    let total = limit + text[end..].chars().count();
    let kept = &text[..kept_head(&text[..end], limit)];
    let omitted = total - kept.chars().count();
    (
        format!("{kept} [+{} chars - {command}]", surface::number(omitted)),
        omitted,
    )
}
/// The bytes `_cap` keeps of `head`, a longer text's first `limit` characters.
fn kept_head(head: &str, limit: usize) -> usize {
    // A negative Python slice start counts back from the end of the head.
    let from = if limit >= 200 {
        limit - 200
    } else {
        (2 * limit).saturating_sub(200)
    };
    let from = head
        .char_indices()
        .nth(from)
        .map_or(head.len(), |(at, _)| at);
    match head.rfind(' ') {
        Some(space) if space >= from => space,
        _ => head.len(),
    }
}
/// 0.4.9's `_expand_command`: the `around` call that prints one message whole, naming
/// speakers other than the user and the agent.
pub fn expand_command(target: &str, turn: i64, who: &str) -> String {
    let mut argv: Vec<String> = vec![
        "agrep".into(),
        "around".into(),
        target.into(),
        turn.to_string(),
        "-C".into(),
        "0".into(),
        "--max-chars".into(),
        "0".into(),
    ];
    if who != "user" && who != "agent" {
        argv.push("--who".into());
        argv.push(who.into());
    }
    surface::shell(&argv)
}
/// Below this many characters a window stops being judgeable.
const WINDOW_MIN: i64 = 140;
/// What one `[1,234 chars] ` gap marker costs.
const ELISION_COST: i64 = 16;
fn merge_spans(spans: &[(usize, usize)]) -> Vec<(usize, usize)> {
    let mut sorted = spans.to_vec();
    sorted.sort_unstable();
    sorted.dedup();
    let mut merged: Vec<(usize, usize)> = Vec::new();
    for (start, end) in sorted {
        match merged.last_mut() {
            Some(last) if start as i64 <= last.1 as i64 + WINDOW_MIN / 2 => {
                last.1 = last.1.max(end)
            }
            _ => merged.push((start, end)),
        }
    }
    merged
}
/// 0.4.9's `_snap`: an offset moved to the nearest word boundary, never below `floor`.
fn snap(text: &Indexed, index: usize, forward: bool, floor: usize) -> usize {
    if index <= floor {
        return floor;
    }
    if forward {
        let window = text.slice(index, index + 40);
        return window
            .find(' ')
            .map_or(index, |at| index + window[..at].chars().count() + 1);
    }
    let window = text.slice(floor.max(index.saturating_sub(40)), index);
    match window.rfind(' ') {
        Some(at) => {
            let back = window.chars().count() - window[..at].chars().count();
            floor.max(index - back)
        }
        None => index,
    }
}
/// 0.4.9's `_match_windows`: the text around each anchor match instead of its head, every
/// gap counted, or `None` when one window is all `limit` affords.
fn match_windows(
    text: &Indexed,
    limit: usize,
    command: &str,
    anchors: &[&Anchor],
) -> Option<(String, usize)> {
    let action = format!(" [snippet - open: {command}]");
    let window_limit = limit as i64 - action.chars().count() as i64;
    let capacity = (window_limit - ELISION_COST)
        .div_euclid(WINDOW_MIN + ELISION_COST)
        .max(1);
    if capacity < 2 {
        return None;
    }
    let capacity = capacity as usize;
    let mut phrases = Vec::new();
    let mut distinct: Vec<(usize, (usize, usize))> = Vec::new();
    let mut repeats = Vec::new();
    for anchor in anchors {
        for found in anchor.regex.captures_iter(text.text) {
            let Some((group, m)) = found
                .iter()
                .enumerate()
                .skip(1)
                .find_map(|(group, m)| m.map(|m| (group, m)))
            else {
                continue;
            };
            let span = (text.index(m.start()), text.index(m.end()));
            if anchor.terms == 0 {
                phrases.push(span);
                if phrases.len() >= capacity {
                    break;
                }
                continue;
            }
            // Term groups are named by position, so equal positions of two queries'
            // alternations count as one term, as 0.4.9's named groups did.
            let term = group - 1;
            if !distinct.iter().any(|(seen, _)| *seen == term) {
                distinct.push((term, span));
            } else if repeats.len() < capacity {
                repeats.push(span);
            }
            if (0..anchor.terms).all(|t| distinct.iter().any(|(seen, _)| *seen == t))
                && repeats.len() >= capacity
            {
                break;
            }
        }
        if !phrases.is_empty() {
            break;
        }
    }
    let candidates: Vec<(usize, usize)> = if phrases.is_empty() {
        distinct
            .iter()
            .map(|(_, span)| *span)
            .chain(repeats)
            .collect()
    } else {
        phrases
    };
    let mut selected: Vec<(usize, usize)> = Vec::new();
    for span in candidates {
        let mut trial = selected.clone();
        trial.push(span);
        if merge_spans(&trial).len() <= capacity {
            selected.push(span);
        }
    }
    let merged = merge_spans(&selected);
    let count = merged.len() as i64;
    if count < 2 {
        return None;
    }
    let room = (window_limit - (count + 1) * ELISION_COST).div_euclid(count);
    let n = text.len();
    let mut out = Vec::new();
    let (mut cursor, mut omitted) = (0, 0);
    for (start, end) in merged {
        let pad = (room - (end - start) as i64).div_euclid(2).max(0) as usize;
        let left = snap(text, cursor.max(start.saturating_sub(pad)), true, cursor);
        let right = snap(text, n.min(end + pad), false, left + 1);
        if left > cursor {
            out.push(format!("[{} chars]", surface::number(left - cursor)));
            omitted += left - cursor;
        }
        out.push(text.slice(left, right).trim_matches(py_space).to_owned());
        cursor = right;
    }
    if cursor < n {
        out.push(format!("[{} chars]", surface::number(n - cursor)));
        omitted += n - cursor;
    }
    Some((out.join(" ") + &action, omitted))
}
/// 0.4.9's `_anchored_cap`: `cap`, except that a capped text keeps its anchors' matches,
/// either as several windows around them or as one slice slid to the first match.
pub fn anchored_cap(
    text: &str,
    limit: usize,
    command: &str,
    anchors: &[&Anchor],
) -> (String, usize) {
    if limit == 0 {
        return (text.to_owned(), 0);
    }
    let chars = Indexed::new(text);
    if chars.len() <= limit {
        return (text.to_owned(), 0);
    }
    if let Some(multi) = match_windows(&chars, limit, command, anchors) {
        return multi;
    }
    if let Some((start, end)) = first_anchor_span(&chars, anchors) {
        let head = chars.slice(0, limit);
        let head = head[..kept_head(head, limit)].chars().count();
        let mut from = start.saturating_sub(120.min(48.max(limit / 6)));
        if let Some(space) = chars.find(' ', from).filter(|space| *space < start) {
            from = space + 1;
        }
        if end > head && from > 0 {
            let (body, tail) = cap(chars.slice(from, chars.len()), limit, command);
            return (
                format!("[+{} chars - {command}] {body}", surface::number(from)),
                from + tail,
            );
        }
    }
    cap(text, limit, command)
}
