//! Reply prose, final questions and open next-step sections.
use crate::history;

pub(super) fn re(pattern: &'static str) -> regex::Regex {
    use std::sync::{LazyLock, Mutex};
    // Only the finite set of literal patterns in this module enters the cache.
    // A project briefing should compile each expression once, not once per chat.
    static PATTERNS: LazyLock<Mutex<std::collections::HashMap<&'static str, regex::Regex>>> =
        LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));
    PATTERNS
        .lock()
        .expect("pattern cache")
        .entry(pattern)
        .or_insert_with(|| regex::Regex::new(pattern).expect("constant regex"))
        .clone()
}
/// Python's `str.splitlines()`.
fn splitlines(s: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((at, c)) = chars.next() {
        if matches!(
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
        ) {
            out.push(&s[start..at]);
            start = at + c.len_utf8();
            if c == '\r' && chars.peek().is_some_and(|(_, next)| *next == '\n') {
                chars.next();
                start += 1;
            }
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}
/// Unicode 16 format characters (`Cf`), which Python's `repr` escapes.
const FORMAT: [(u32, u32); 21] = [
    (0xAD, 0xAD),
    (0x600, 0x605),
    (0x61C, 0x61C),
    (0x6DD, 0x6DD),
    (0x70F, 0x70F),
    (0x890, 0x891),
    (0x8E2, 0x8E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
];
/// Python's `repr()` of a string; unassigned code points print as themselves.
pub fn repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let printable = |c: char| {
        let cp = c as u32;
        !(c.is_control()
            || crate::unicode_v16::is_space(c)
            || FORMAT.iter().any(|(lo, hi)| (*lo..=*hi).contains(&cp))
            || (0xE000..=0xF8FF).contains(&cp)
            || cp >= 0xF0000)
    };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if c == quote => {
                out.push('\\');
                out.push(c)
            }
            _ if c < ' ' || c == '\x7f' => out.push_str(&format!("\\x{:02x}", c as u32)),
            _ if c.is_ascii() || printable(c) => out.push(c),
            _ if (c as u32) <= 0xFF => out.push_str(&format!("\\x{:02x}", c as u32)),
            _ if (c as u32) <= 0xFFFF => out.push_str(&format!("\\u{:04x}", c as u32)),
            _ => out.push_str(&format!("\\U{:08x}", c as u32)),
        }
    }
    out.push(quote);
    out
}
pub(super) fn one_line(s: &str) -> String {
    history::one_line(s, usize::MAX)
}
/// 0.4.9's `_prose` (py/summary.py:595-600): a reply without code, quotes or links.
pub(super) fn prose(reply: &str) -> String {
    let text = re(r"(?s)```.*?```").replace_all(reply, " ");
    let text = re(r"`[^`\n]*`").replace_all(&text, " ");
    let text = splitlines(&text)
        .into_iter()
        .filter(|line| !line.trim_start().starts_with('>'))
        .collect::<Vec<_>>()
        .join("\n");
    re(r"https?://\S+").replace_all(&text, " ").into_owned()
}
/// 0.4.9's `_final_sentence` (py/summary.py:603-609).
pub(super) fn final_sentence(prose: &str) -> String {
    let Some(paragraph) = re(r"\n\s*\n")
        .split(prose)
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .last()
    else {
        return String::new();
    };
    let tail = one_line(&re(r"[*_~#>]+").replace_all(paragraph, " "));
    // Python splits at `(?<=[.!?])\s+`; one_line leaves single spaces.
    let mut last = "";
    let mut start = 0;
    let mut previous = ' ';
    for (at, c) in tail.char_indices() {
        if c == ' ' && matches!(previous, '.' | '!' | '?') {
            if !tail[start..at].trim().is_empty() {
                last = &tail[start..at];
            }
            start = at + 1;
        }
        previous = c;
    }
    if !tail[start..].trim().is_empty() {
        last = &tail[start..];
    }
    last.trim().to_owned()
}
/// 0.4.9's `_direct_request` (py/summary.py:612-622): the final sentence when it asks the
/// human something.
pub(super) fn direct_request(reply: &str) -> Option<String> {
    let sentence = final_sentence(&prose(reply));
    let asks = sentence.ends_with('?')
        && re(r"(?i)\b(should i|shall i|do you want|would you like|which|what do you|what would you|can you|could you|please confirm|your call|confirm|choose|prefer|do you|are you|is it ok|ok to|may i|want me to|how should|where should|tell me)\b").is_match(&sentence);
    (asks
        || re(r"(?i)\b(please confirm|awaiting your|waiting for your|waiting on you|your call)\b")
            .is_match(&sentence))
    .then_some(sentence)
}
/// 0.4.9's `_courtesy`: a short sign-off with no substance of its own.
fn courtesy(block: &str) -> bool {
    block.chars().count() <= 200
        && re(r"(?i)\b(let me know|tell me|say the word|happy to|glad to|feel free|if you(?:'d| would)? (?:like|want|prefer|need)|want me to|i can (?:also|then|take)|just (?:say|ask)|shout|ping me)\b").is_match(block)
}
/// 0.4.9's `_open_section_items` (py/summary.py:625-664): (heading, open items, closed
/// count) for a next-steps style section ending one reply block.
pub(super) fn section(block: &str) -> Option<(String, Vec<String>, usize)> {
    let text = prose(block);
    let lines = splitlines(&text);
    let heading = re(
        r"(?i)^\s*(?:#+\s*)?(?:\*\*|__)?\s*(next steps?|remaining(?: work| items?| tasks?| steps?)?|follow[- ]?ups?|to[- ]?dos?|open items?|outstanding(?: work| items?| tasks?)?|still to do|left to do|what'?s left)\s*:?\s*(?:\*\*|__)?\s*:?\s*$",
    );
    let at = lines.iter().rposition(|line| heading.is_match(line))?;
    let name = heading.captures(lines[at])?[1].to_lowercase();
    let bullet = re(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*\S)\s*$");
    let mut bullets: Vec<String> = vec![];
    let mut trailing: Vec<String> = vec![];
    for line in &lines[at + 1..] {
        if line.trim().is_empty() {
            continue;
        }
        match bullet.captures(line) {
            Some(m) if trailing.is_empty() => bullets.push(m[1].trim().to_owned()),
            _ if !bullets.is_empty() => trailing.push(line.trim().to_owned()),
            _ => {}
        }
    }
    let last = bullets.last_mut()?;
    // Python splits the last bullet once at `(?<=[.!?])\s+(?=\S)`: a sign-off written on its line.
    let chars: Vec<(usize, char)> = last.char_indices().collect();
    let cut = (1..chars.len()).find_map(|i| {
        if !matches!(chars[i - 1].1, '.' | '!' | '?') || !chars[i].1.is_whitespace() {
            return None;
        }
        let end = (i..chars.len()).find(|j| !chars[*j].1.is_whitespace())?;
        Some((chars[i].0, chars[end].0))
    });
    let mut rest = vec![];
    if let Some((head, tail)) = cut {
        rest.push(last[tail..].to_owned());
        last.truncate(head);
    }
    rest.extend(trailing);
    let tail = rest.join(" ");
    if !tail.is_empty() && !courtesy(&tail) {
        return None;
    }
    let checked = re(r"^\[[xX✓✔]\]");
    let done = re(
        r"(?i)(?:^~~.*~~$)|(?:\((?:done|completed|cancelled|canceled|skipped)\)\s*$)|(?:[-:—]\s*(?:done|completed|cancelled|canceled|skipped)\.?\s*$)",
    );
    let none = re(
        r"(?i)^(?:(?:none|nothing)(?: else| more| further| remaining| left| open| outstanding| pending| for now| so far| at (?:the|this) (?:moment|time|point)| at present)?|n/?a|all done|no(?:thing)? (?:open|remaining|outstanding|further|more|pending)\b.*)\.?$",
    );
    let mut open = vec![];
    let mut closed = 0;
    for item in &bullets {
        if checked.is_match(item) || done.is_match(item) {
            closed += 1;
            continue;
        }
        let item = re(r"^\[\s\]").replace(item, "");
        let item = one_line(&re(r"[*_~#>]+").replace_all(item.trim(), " "));
        if !item.is_empty() && !none.is_match(&item) {
            open.push(item);
        }
    }
    Some((name, open, closed))
}
const WORKED_YES: u8 = 1;
const WORKED_UNKNOWN: u8 = 2;
/// 0.4.9's `_open_section` (py/summary.py:674-705): the open section the reply ends on, with
/// the caveat a later block earns it.
pub(super) fn open_section(
    blocks: &[String],
    worked: &[u8],
) -> Option<(String, Vec<String>, usize, Option<String>)> {
    for index in (0..blocks.len()).rev() {
        let Some((heading, open, closed)) = section(&blocks[index]) else {
            continue;
        };
        if index + 1 == blocks.len() {
            return Some((heading, open, closed, None));
        }
        let joins = blocks.len() - 1 - index;
        let mut later_joins: Vec<u8> = worked.iter().skip(index).take(joins).copied().collect();
        later_joins.resize(joins, WORKED_UNKNOWN);
        if later_joins.contains(&WORKED_YES) {
            return None;
        }
        let later: Vec<&String> = blocks[index + 1..]
            .iter()
            .filter(|b| !b.trim().is_empty())
            .collect();
        let substantive = later.iter().find(|b| !courtesy(b));
        let quote = |block: &str| repr(&one_line(block).chars().take(80).collect::<String>());
        let caveat = if later.is_empty() {
            None
        } else if later_joins.contains(&WORKED_UNKNOWN) {
            if substantive.is_some() {
                return None;
            }
            Some(format!(
                "whether work followed this list is not recorded; a later block: {}",
                quote(later[0])
            ))
        } else {
            substantive.map(|b| format!("a later reply block may report this done: {}", quote(b)))
        };
        return Some((heading, open, closed, caveat));
    }
    None
}
/// 0.4.9's `_blocks` (py/summary.py:433-444): a reply split at the joining spaces the ingest
/// recorded, blank blocks dropped.
pub(super) fn blocks(reply: &str, breaks: &[u32]) -> Vec<String> {
    if reply.is_empty() {
        return vec![];
    }
    let chars: Vec<char> = reply.chars().collect();
    let mut out: Vec<String> = vec![];
    let mut start = 0;
    for offset in breaks.iter().map(|o| *o as usize) {
        if start <= offset && offset < chars.len() && chars[offset] == ' ' {
            out.push(chars[start..offset].iter().collect());
            start = offset + 1;
        }
    }
    out.push(chars[start..].iter().collect());
    out.retain(|b| !b.trim().is_empty());
    if out.is_empty() {
        vec![reply.to_owned()]
    } else {
        out
    }
}
