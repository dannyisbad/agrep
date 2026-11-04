//! Evidence-backed pending items and interval arithmetic for local briefings.
use crate::{
    history::{self, Row, Session},
    model::ReplyShape,
};
use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{
    cell::OnceCell,
    collections::{BTreeMap, HashSet},
};

/// An indexed event as briefings read it. Classification reads the columns of every event of
/// a chat but the text of only a few, so the stored text is decoded on first use.
pub struct Event<'a> {
    pub ts: i64,
    pub kind: &'a str,
    pub name: &'a str,
    pub ok: Option<bool>,
    pub child_session: String,
    pub meta: String,
    pub state: String,
    input_chars: i64,
    input_end: Option<usize>,
    output_start: Option<usize>,
    blob: Option<i64>,
    text: OnceCell<(String, String)>,
}
impl Event<'_> {
    /// The input and output `evidence::load_events` gives the event.
    fn text(&self, c: &Connection) -> Result<&(String, String)> {
        if let Some(text) = self.text.get() {
            return Ok(text);
        }
        let text = if self.kind == "control" {
            (self.meta.clone(), String::new())
        } else {
            let stored = match self.blob {
                Some(blob) => c
                    .prepare_cached("SELECT body FROM tool_blobs WHERE id=?1")?
                    .query_row([blob], |r| r.get::<_, Vec<u8>>(0))
                    .optional()?,
                None => None,
            };
            let text = stored
                .map(|bytes| crate::store::decode("tools", bytes))
                .transpose()?
                .unwrap_or_default();
            crate::tool_text::split(
                &text,
                self.kind,
                self.name,
                self.ok,
                self.input_end,
                self.output_start,
            )
        };
        Ok(self.text.get_or_init(|| text))
    }
    pub fn input(&self, c: &Connection) -> Result<&str> {
        Ok(&self.text(c)?.0)
    }
    pub fn output(&self, c: &Connection) -> Result<&str> {
        Ok(&self.text(c)?.1)
    }
}
/// The kind and tool names `event_rows` refers to by id.
pub struct Symbols(Vec<(i64, String)>);
impl Symbols {
    pub fn load(c: &Connection) -> Result<Self> {
        let mut q = c.prepare("SELECT id,value FROM symbols ORDER BY id")?;
        let names = q.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
        Ok(Self(names.collect::<rusqlite::Result<_>>()?))
    }
    fn get(&self, id: i64) -> Option<&str> {
        let at = self.0.binary_search_by_key(&id, |(key, _)| *key).ok()?;
        Some(&self.0[at].1)
    }
}
/// The events `evidence::load_events` loads for the chat, in its order and with its replay
/// de-duplication, their text left stored.
pub fn events<'a>(c: &Connection, symbols: &'a Symbols, s: &Session) -> Result<Vec<Event<'a>>> {
    // That query's plan feeds SQLite's sorter the rows in this index order to sort by
    // (position, ts, call_id). The sorter is stable, so a stable sort of the same rows here
    // gives its order without copying every row through the sorter.
    let mut q = c.prepare_cached("SELECT e.position,e.call_id,e.ts,e.kind_key,e.name_key,CASE WHEN e.ok IS NULL THEN NULL WHEN e.ok THEN 1 ELSE 0 END,e.child_session,e.meta,e.state,e.input_chars,e.input_end,e.output_start,e.blob FROM event_rows e JOIN event_sessions s ON s.id=e.session_key WHERE e.active=1 AND e.visible=1 AND s.session=?1 AND e.position BETWEEN ?2 AND ?3 ORDER BY s.id,e.position,e.ts,e.id")?;
    let mut rows = q.query(params![s.id, i64::MIN, i64::MAX])?;
    let offset = |v: Option<i64>| v.and_then(|v| usize::try_from(v).ok());
    let mut found: Vec<(i64, String, Event<'a>)> = vec![];
    while let Some(r) = rows.next()? {
        let (Some(kind), Some(name)) = (symbols.get(r.get(3)?), symbols.get(r.get(4)?)) else {
            continue;
        };
        found.push((
            r.get(0)?,
            r.get(1)?,
            Event {
                ts: r.get(2)?,
                kind,
                name,
                ok: r.get::<_, Option<i64>>(5)?.map(|ok| ok != 0),
                child_session: r.get(6)?,
                meta: r.get(7)?,
                state: r.get(8)?,
                input_chars: r.get(9)?,
                input_end: offset(r.get(10)?),
                output_start: offset(r.get(11)?),
                blob: r.get(12)?,
                text: OnceCell::new(),
            },
        ));
    }
    found.sort_by(|(p1, call1, e1), (p2, call2, e2)| (p1, e1.ts, call1).cmp(&(p2, e2.ts, call2)));
    // Replay copies of one call share its id; separate identical calls stay separate.
    let mut seen = HashSet::new();
    Ok(found
        .into_iter()
        .filter_map(|(_, call, event)| seen.insert(call).then_some(event))
        .collect())
}

pub fn merge(mut intervals: Vec<(i64, i64)>) -> Vec<(i64, i64)> {
    intervals.sort_unstable();
    let mut out: Vec<(i64, i64)> = vec![];
    for (start, end) in intervals {
        if end <= start {
            continue;
        }
        if let Some(last) = out.last_mut() {
            if start <= last.1 {
                last.1 = last.1.max(end);
                continue;
            }
        }
        out.push((start, end));
    }
    out
}
pub fn span(intervals: &[(i64, i64)]) -> i64 {
    intervals.iter().map(|(s, e)| e - s).sum()
}
pub fn intervals(
    rows: &[Row],
    events: &[Event],
    cap: i64,
    since: i64,
    until: i64,
) -> Vec<(i64, i64)> {
    let mut points: Vec<_> = rows
        .iter()
        .filter(|r| r.who != "agent" && r.ts > 0)
        .map(|r| r.ts)
        .chain(
            events
                .iter()
                .filter(|e| e.name == "compacted")
                .map(|e| e.ts),
        )
        .collect();
    points.sort_unstable();
    points.dedup();
    let mut raw: Vec<_> = points
        .windows(2)
        .map(|w| (w[0], w[1].min(w[0].saturating_add(cap))))
        .collect();
    if let Some(&last) = points.last() {
        if let Some(end) = events
            .iter()
            .filter(|e| e.kind == "tool" || e.kind == "subagent_result")
            .map(|e| e.ts)
            .max()
        {
            raw.push((last, end.min(last.saturating_add(cap))));
        }
    }
    merge(
        raw.into_iter()
            .map(|(s, e)| (s.max(since), e.min(until)))
            .collect(),
    )
}
fn re(pattern: &'static str) -> regex::Regex {
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
fn one_line(s: &str) -> String {
    history::one_line(s, usize::MAX)
}
/// 0.4.9's `_prose` (py/summary.py:595-600): a reply without code, quotes or links.
fn prose(reply: &str) -> String {
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
fn final_sentence(prose: &str) -> String {
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
fn direct_request(reply: &str) -> Option<String> {
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
fn section(block: &str) -> Option<(String, Vec<String>, usize)> {
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
fn open_section(
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
fn blocks(reply: &str, breaks: &[u32]) -> Vec<String> {
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
#[derive(Clone, Default)]
struct Task {
    id: String,
    phase: String,
    text: String,
    status: String,
}
fn task(item: &Value) -> Task {
    if let Some(text) = item.as_str() {
        return Task {
            text: history::one_line(text, usize::MAX),
            status: "open".into(),
            ..Default::default()
        };
    }
    Task {
        id: match &item["id"] {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            _ => String::new(),
        },
        phase: String::new(),
        text: [
            "content",
            "title",
            "task",
            "step",
            "text",
            "name",
            "description",
            "subject",
        ]
        .iter()
        .find_map(|k| item[*k].as_str().filter(|s| !s.trim().is_empty()))
        .map(|s| history::one_line(s, usize::MAX))
        .unwrap_or_default(),
        status: item["status"]
            .as_str()
            .or_else(|| item["state"].as_str())
            .unwrap_or("")
            .to_lowercase()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join("_"),
    }
}
fn todo_op(tasks: &mut Vec<Task>, payload: &Value) -> bool {
    let items = payload["items"].as_array();
    let phases = payload["list"].as_array();
    let phase = payload["phase"].as_str().unwrap_or("");
    let op = payload["op"].as_str().unwrap_or_else(|| {
        if phases.is_some() {
            "init"
        } else if items.is_some() && !phase.is_empty() {
            "append"
        } else if items.is_some() && tasks.is_empty() {
            "init"
        } else {
            ""
        }
    });
    match op {
        "view" => true,
        "init" => {
            let mut fresh = vec![];
            if let Some(phases) = phases {
                for p in phases {
                    let name = p["phase"].as_str().unwrap_or("Tasks");
                    if let Some(items) = p["items"].as_array() {
                        for i in items {
                            let mut t = task(i);
                            if t.text.is_empty() {
                                continue;
                            }
                            t.phase = name.into();
                            t.status = "pending".into();
                            fresh.push(t);
                        }
                    }
                }
            } else if let Some(items) = items {
                for i in items {
                    let mut t = task(i);
                    if t.text.is_empty() {
                        continue;
                    }
                    t.phase = if phase.is_empty() { "Tasks" } else { phase }.into();
                    t.status = "pending".into();
                    fresh.push(t);
                }
            } else {
                return false;
            }
            *tasks = fresh;
            true
        }
        "append" => {
            let Some(items) = items else { return false };
            if phase.is_empty() {
                return false;
            }
            let fresh: Vec<_> = items
                .iter()
                .map(task)
                .filter(|t| !t.text.is_empty())
                .collect();
            if fresh.iter().any(|f| tasks.iter().any(|t| t.text == f.text)) {
                return false;
            }
            for mut t in fresh {
                t.phase = phase.into();
                t.status = "pending".into();
                tasks.push(t);
            }
            true
        }
        "start" | "done" | "drop" | "block" | "unblock" | "rm" => {
            let target = payload["task"].as_str().unwrap_or("");
            if (op == "start" && target.is_empty())
                || (matches!(op, "block" | "unblock") && target.is_empty() && phase.is_empty())
            {
                return false;
            }
            let hits: Vec<_> = tasks
                .iter()
                .enumerate()
                .filter(|(_, t)| {
                    if !target.is_empty() {
                        t.text == target
                    } else if !phase.is_empty() {
                        t.phase == phase
                    } else {
                        true
                    }
                })
                .map(|(i, _)| i)
                .collect();
            if hits.is_empty() {
                return false;
            }
            if op == "start" {
                for t in tasks.iter_mut() {
                    if t.status == "in_progress" {
                        t.status = "pending".into();
                    }
                }
            }
            if op == "rm" {
                for i in hits.into_iter().rev() {
                    tasks.remove(i);
                }
                return true;
            }
            for i in hits {
                let t = &mut tasks[i];
                let state = match op {
                    "start" => "in_progress",
                    "done" => "completed",
                    "drop" => "abandoned",
                    "block"
                        if matches!(t.status.as_str(), "pending" | "in_progress" | "blocked") =>
                    {
                        "blocked"
                    }
                    "unblock" if t.status == "blocked" => "pending",
                    _ => continue,
                };
                t.status = state.into();
            }
            true
        }
        _ => false,
    }
}
fn todos(c: &Connection, events: &[Event]) -> Result<(Vec<String>, Option<String>)> {
    let mut tasks: Vec<Task> = vec![];
    let mut claude = BTreeMap::<String, Task>::new();
    let mut claude_last = false;
    let mut unknown = None;
    let mut claude_unknown = None;
    for event in events {
        let name = event.name;
        if !name.to_lowercase().contains("todo")
            && !matches!(
                name,
                "update_plan" | "TaskCreate" | "TaskUpdate" | "TaskList"
            )
        {
            continue;
        }
        if event.ok == Some(false) {
            continue;
        }
        let raw = if event.state.is_empty() {
            event.input(c)?
        } else {
            event.state.as_str()
        };
        let state = serde_json::from_str::<Value>(raw).ok();
        if name.starts_with("Task") {
            claude_last = true;
            let output = event.output(c)?;
            match name {
                "TaskCreate" => {
                    if let Some(m) =
                        re(r"(?s)^Task #(\S+) created successfully: (.*)").captures(output)
                    {
                        claude.insert(
                            m[1].into(),
                            Task {
                                id: m[1].into(),
                                text: history::one_line(&m[2], usize::MAX),
                                status: "pending".into(),
                                ..Default::default()
                            },
                        );
                    } else {
                        claude_unknown = Some("task creation result cannot be resolved".into());
                    }
                }
                "TaskUpdate" => {
                    if !output.is_empty() && !output.starts_with("Updated task #") {
                        continue;
                    }
                    let update = re(r"^Updated task #(\S+)").captures(output);
                    let id = update
                        .as_ref()
                        .map(|m| m[1].to_string())
                        .or_else(|| {
                            state.as_ref().and_then(|s| {
                                ["taskId", "id", "task_id"]
                                    .iter()
                                    .find_map(|k| s[*k].as_str().map(str::to_string))
                            })
                        })
                        .unwrap_or_default();
                    if let Some(s) = state.as_ref() {
                        if s["status"] == "deleted" {
                            claude.remove(&id);
                            continue;
                        }
                        if let Some(t) = claude.get_mut(&id) {
                            if let Some(status) = s["status"].as_str() {
                                t.status = status.into()
                            }
                            if let Some(subject) = s["subject"].as_str() {
                                t.text = subject.into()
                            }
                        } else {
                            claude_unknown = Some("task update refers to an unknown task".into());
                        }
                    } else {
                        claude_unknown = Some("task update payload is incomplete".into());
                    }
                }
                "TaskList" => {
                    let listing = state
                        .as_ref()
                        .and_then(|s| s["listing"].as_str())
                        .unwrap_or(output);
                    if listing.is_empty() {
                        continue;
                    }
                    let mut fresh = BTreeMap::new();
                    let mut valid = true;
                    for line in listing.lines() {
                        if let Some(m) = re(r"^#(\S+) \[([a-z_]+)\](?: (.*))?$").captures(line) {
                            let text = claude
                                .get(&m[1])
                                .map(|t| t.text.clone())
                                .unwrap_or_else(|| m.get(3).map_or("", |s| s.as_str()).into());
                            fresh.insert(
                                m[1].into(),
                                Task {
                                    id: m[1].into(),
                                    text,
                                    status: m[2].into(),
                                    ..Default::default()
                                },
                            );
                        } else if line != "No tasks found" {
                            valid = false;
                        }
                    }
                    if valid {
                        claude = fresh;
                        claude_unknown = None
                    } else {
                        claude_unknown = Some("task listing is incomplete".into())
                    }
                }
                _ => {}
            }
            continue;
        }
        claude_last = false;
        let Some(state) = state else {
            unknown = Some("task-list payload is incomplete".into());
            continue;
        };
        if state == json!({}) {
            continue;
        }
        let whole = state
            .as_array()
            .or_else(|| ["todos", "plan"].iter().find_map(|k| state[*k].as_array()));
        if let Some(items) = whole {
            let incoming: Vec<_> = items.iter().map(task).collect();
            if state["merge"] == true {
                for incoming in incoming {
                    if let Some(t) = tasks
                        .iter_mut()
                        .find(|t| !incoming.id.is_empty() && t.id == incoming.id)
                    {
                        if !incoming.text.is_empty() {
                            t.text = incoming.text;
                        }
                        if !incoming.status.is_empty() {
                            t.status = incoming.status;
                        }
                    } else if !incoming.text.is_empty() {
                        tasks.push(incoming)
                    } else {
                        unknown = Some("merged task has no known content".into());
                    }
                }
            } else {
                tasks = incoming
                    .into_iter()
                    .filter(|t| !t.text.is_empty())
                    .collect();
                unknown = None;
            }
        } else if todo_op(&mut tasks, &state) {
            if state["op"] == "init" || state.get("list").is_some() {
                unknown = None
            }
        } else {
            unknown = Some("task operation cannot be resolved".into())
        }
    }
    let (tasks, unknown) = if claude_last {
        (claude.into_values().collect::<Vec<_>>(), claude_unknown)
    } else {
        (tasks, unknown)
    };
    let open = tasks
        .into_iter()
        .filter(|t| {
            matches!(
                t.status.as_str(),
                "pending"
                    | "in_progress"
                    | "in-progress"
                    | "not_started"
                    | "not-started"
                    | "open"
                    | "todo"
                    | "active"
                    | "blocked"
                    | "queued"
            )
        })
        .map(|t| t.text)
        .collect();
    Ok(if unknown.is_some() {
        (vec![], unknown)
    } else {
        (open, unknown)
    })
}
pub fn is_turn(rows: &[Row], row: &Row) -> bool {
    row.who != "agent"
        && (row.who != "recap"
            || rows
                .iter()
                .find(|r| r.who != "agent")
                .is_some_and(|first| first.turn == row.turn))
}
fn terminal_reply(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    last: &Row,
) -> Result<(String, ReplyShape)> {
    let mut text = String::new();
    let mut combined = ReplyShape::default();
    let mut q = c.prepare_cached(
        "SELECT body FROM reply_shapes WHERE source=?1 AND session=?2 AND turn=?3",
    )?;
    for reply in rows.iter().filter(|r| {
        r.who == "agent"
            && (r.turn == last.turn
                || (r.turn > last.turn
                    && rows.iter().any(|p| p.turn == r.turn && p.who == "recap")))
    }) {
        let mut query = q.query(params![reply.source, s.id, reply.source_turn])?;
        let shape = if let Some(row) = query.next()? {
            serde_json::from_str::<ReplyShape>(&row.get::<_, String>(0)?)?
        } else {
            ReplyShape::default()
        };
        let mut offset = text.chars().count() as u32;
        if !text.is_empty() {
            combined.breaks.push(offset);
            combined.worked.push(shape.lead);
            text.push(' ');
            offset += 1;
        }
        combined
            .breaks
            .extend(shape.breaks.iter().map(|p| p + offset));
        combined.worked.extend(shape.worked);
        combined.end = shape.end;
        combined.end_cut = shape.end_cut;
        text.push_str(&reply.text);
    }
    combined.head_chars = text.chars().count() as u32;
    combined.joins = combined.breaks.len() as u32;
    Ok((text, combined))
}
/// What `_terminal` (py/summary.py:711-727) reads for a turn: the text to classify, its
/// blocks, and why its ending is missing.
struct Terminal {
    text: String,
    blocks: Vec<String>,
    worked: Vec<u8>,
    missing: Option<&'static str>,
}
fn terminal(c: &Connection, s: &Session, rows: &[Row], last: &Row) -> Result<Terminal> {
    let (reply, shape) = terminal_reply(c, s, rows, last)?;
    let mut parts = blocks(&reply, &shape.breaks);
    if !shape.end.is_empty() {
        parts.push(shape.end.clone());
        return Ok(Terminal {
            text: shape.end,
            blocks: parts,
            worked: shape.worked,
            missing: shape.end_cut.then_some("reply capped at index time; its final block ran past 8,000 chars, so a list above its ending was not indexed"),
        });
    }
    let missing = (reply.chars().count() > 64_000 && reply.ends_with('…'))
        .then_some("reply capped at index time; ending not indexed");
    Ok(Terminal {
        text: reply,
        blocks: parts,
        worked: shape.worked,
        missing,
    })
}
fn named<'a>(e: &Event<'a>, fallback: &'a str) -> &'a str {
    if e.name.is_empty() {
        fallback
    } else {
        e.name
    }
}
/// Python's `max(events, key=(ts, i))`: the latest, the last recorded on a tie.
fn newest<'a, 'b>(events: impl Iterator<Item = &'a Event<'b>>) -> Option<&'a Event<'b>> {
    events.max_by_key(|e| e.ts)
}
/// 0.4.9's `_question_head` (py/summary.py:1215-1239): what a parked question tool asked.
fn question_head(raw: &str, name: &str) -> String {
    let payload = serde_json::from_str::<Value>(raw).ok();
    if let Some(payload) = payload.as_ref().filter(|p| p.is_object()) {
        if let Some(questions) = payload["questions"].as_array() {
            if let Some(q) = questions.iter().find_map(|q| q["question"].as_str()) {
                return one_line(q);
            }
        }
        for key in ["question", "plan", "prompt", "message"] {
            if let Some(text) = payload[key].as_str().filter(|t| !t.trim().is_empty()) {
                return one_line(text);
            }
        }
    }
    for pattern in [
        r#""question"\s*:\s*"((?:[^"\\]|\\.)*)"#,
        r#""plan"\s*:\s*"((?:[^"\\]|\\.)*)"#,
    ] {
        if let Some(found) = re(pattern)
            .captures(raw)
            .filter(|m| !m[1].trim().is_empty())
        {
            // `_json_fragment`: the input cap may have cut the string mid-escape.
            let body = &found[1];
            let ends: Vec<usize> = body
                .char_indices()
                .map(|(at, _)| at)
                .chain([body.len()])
                .collect();
            let decoded = ends.iter().rev().take(6).find_map(|end| {
                serde_json::from_str::<String>(&format!("\"{}\"", &body[..*end])).ok()
            });
            return one_line(&decoded.unwrap_or_else(|| body.to_owned()));
        }
    }
    if payload.is_none() && raw.trim_start().starts_with(['{', '[']) {
        return if name == "ExitPlanMode" {
            "presented a plan"
        } else {
            "asked a question"
        }
        .into();
    }
    one_line(raw)
}
/// 0.4.9's `_api_error` (py/summary.py:1265-1288): the error a Claude turn stopped on.
fn api_error(c: &Connection, s: &Session, later: &[&Event], reply: &str) -> Result<Option<String>> {
    if s.agent != "claude" || reply.is_empty() {
        return Ok(None);
    }
    let Some(event) = newest(later.iter().copied()) else {
        return Ok(None);
    };
    if event.kind != "control" || event.name != "api_error" {
        return Ok(None);
    }
    let text = event.input(c)?;
    let length = text.chars().count();
    let chars = u64::try_from(event.input_chars).unwrap_or(0) as usize;
    let (head, ends) = if text.ends_with('…') && chars >= length {
        let head = one_line(&text.chars().take(length - 1).collect::<String>());
        let reply_chars: Vec<char> = reply.chars().collect();
        let split = reply_chars.len().saturating_sub(chars);
        let tail: String = reply_chars[split..].iter().collect();
        let ends = reply_chars.len() >= chars
            && (split == 0 || reply_chars[split - 1].is_whitespace())
            && one_line(&tail).starts_with(&head);
        (head, ends)
    } else {
        let head = one_line(text);
        let flat = one_line(reply);
        let ends = flat == head || flat.ends_with(&format!(" {head}"));
        (head, ends)
    };
    Ok((!head.is_empty() && ends).then(|| one_line(text)))
}
/// 0.4.9's `_trailing_call` (py/summary.py:1378-1387): the turn's latest work call when no
/// reply followed it.
fn trailing_call<'a, 'b>(later: &[&'a Event<'b>]) -> Option<&'a Event<'b>> {
    let latest = newest(later.iter().copied().filter(|e| {
        let name = e.name;
        e.kind == "tool"
            && !(name.to_lowercase().contains("todo")
                || name.to_lowercase().contains("update_plan")
                || matches!(name, "TaskCreate" | "TaskUpdate" | "TaskList")
                || name.eq_ignore_ascii_case("yield"))
    }))?;
    let after_reply = latest
        .meta
        .split_whitespace()
        .find_map(|part| {
            part.split_once('=')
                .filter(|(key, _)| *key == "after_reply")
        })
        .is_some_and(|(_, value)| value == "1");
    after_reply.then_some(latest)
}
#[derive(Default)]
struct Record {
    status: &'static str,
    confidence: Option<&'static str>,
    signals: Vec<String>,
    evidence: String,
    items: Vec<String>,
    caveats: Vec<String>,
}
fn plural(n: usize) -> &'static str {
    if n == 1 {
        ""
    } else {
        "s"
    }
}
fn last_turn(rows: &[Row]) -> Option<&Row> {
    rows.iter().rev().find(|r| is_turn(rows, r))
}
/// 0.4.9's `_classify_root` (py/summary.py:1291-1371): why a root chat still needs the human.
fn classify_root(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
    last: &Row,
) -> Result<Option<Record>> {
    let t = terminal(c, s, rows, last)?;
    let reply = t.text.as_str();
    let later: Vec<&Event> = events
        .iter()
        .filter(|e| last.ts <= 0 || e.ts >= last.ts)
        .collect();
    let mut r = Record::default();
    let question = newest(later.iter().copied().filter(|e| e.kind == "tool")).filter(|e| {
        re(r"(?i)^(?:AskUserQuestion|ExitPlanMode|ask|request_user_input|question)$")
            .is_match(e.name)
            && e.ok.is_none()
    });
    if let Some(question) = question {
        if question.output(c)?.trim().is_empty() {
            r.status = "waiting_on_user";
            r.signals = vec![format!("{} awaits your answer", question.name)];
            r.evidence = question_head(question.input(c)?, question.name);
            return Ok(Some(r));
        }
    }
    if let Some(error) = api_error(c, s, &later, reply)? {
        r.status = "agent_work_incomplete";
        r.signals = vec!["final reply is an API error".into()];
        r.evidence = error;
        return Ok(Some(r));
    }
    if let Some(request) = Some(reply)
        .filter(|r| !r.is_empty())
        .and_then(direct_request)
    {
        r.status = "waiting_on_user";
        r.signals = vec!["final reply asks you something".into()];
        r.evidence = request;
        return Ok(Some(r));
    }
    if let Some((heading, open, closed, later_block)) = Some(reply)
        .filter(|r| !r.is_empty())
        .and_then(|_| open_section(&t.blocks, &t.worked))
        .filter(|section| !section.1.is_empty())
    {
        let done = if closed > 0 {
            format!(" ({closed} done)")
        } else {
            String::new()
        };
        r.status = "open_next_steps";
        r.signals = vec![format!(
            "{heading} section lists {} open item{}{done}",
            open.len(),
            plural(open.len())
        )];
        r.evidence = open[0].clone();
        r.items = open;
        if let Some(caveat) = later_block {
            r.confidence = Some("medium");
            r.caveats = vec![caveat];
        }
        return Ok(Some(r));
    }
    let failed = later
        .iter()
        .rev()
        .find(|e| e.kind == "tool" && e.ok == Some(false));
    let stopped = later
        .iter()
        .rev()
        .find(|e| e.kind == "control" && re(r"(?i)interrupt|abort|cancel|stop").is_match(e.name));
    let failed_signal = failed.map(|e| format!("latest tool failed ({})", named(e, "tool")));
    let stopped_signal = stopped.map(|e| format!("turn {}", named(e, "stopped")));
    if reply.is_empty() {
        r.signals = vec!["no captured reply to the last turn".into()];
        r.signals.extend(failed_signal);
        r.signals.extend(stopped_signal);
        if last.ts <= 0 {
            r.status = "unknown";
            r.signals.push("last turn has no timestamp".into());
            r.evidence = one_line(&last.text);
        } else {
            r.status = "agent_work_incomplete";
            r.evidence = one_line(match failed {
                Some(e) => e.output(c)?,
                None => &last.text,
            });
        }
        return Ok(Some(r));
    }
    let (open, unknown) = todos(c, events)?;
    r.caveats = unknown.into_iter().collect();
    if !open.is_empty() {
        r.status = "todo_open";
        r.signals = vec![format!(
            "last todo list has {} open item{}",
            open.len(),
            plural(open.len())
        )];
        r.evidence = open[0].clone();
        r.items = open;
        return Ok(Some(r));
    }
    if let Some(trailing) = trailing_call(&later) {
        r.status = "agent_work_incomplete";
        r.signals = vec![format!(
            "reply ended before the turn's last tool call ({})",
            named(trailing, "tool")
        )];
        r.signals.extend(failed_signal);
        r.signals.extend(stopped_signal);
        r.evidence = final_sentence(&prose(reply));
        return Ok(Some(r));
    }
    if let Some(missing) = t.missing {
        r.status = "unknown";
        r.signals = vec![missing.into()];
        r.evidence = final_sentence(&prose(reply));
        return Ok(Some(r));
    }
    if !r.caveats.is_empty() {
        r.status = "unknown";
        r.signals = r.caveats.clone();
        r.evidence = one_line(&last.text);
        return Ok(Some(r));
    }
    Ok(None)
}
/// The body of 0.4.9's `_classify_side` (py/summary.py:1901-1951) once its family checks
/// passed: why a side chat's open work still needs the human.
fn classify_side(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
    last: &Row,
) -> Result<Option<Record>> {
    if last.ts <= 0 {
        return Ok(None);
    }
    let t = terminal(c, s, rows, last)?;
    let later: Vec<&Event> = events.iter().filter(|e| e.ts >= last.ts).collect();
    let mut r = Record {
        status: "agent_work_incomplete",
        ..Default::default()
    };
    if t.text.is_empty() {
        r.signals = vec!["side chat has no captured reply to its last turn".into()];
        if let Some(failed) = later
            .iter()
            .rev()
            .find(|e| e.kind == "tool" && e.ok == Some(false))
        {
            r.signals
                .push(format!("latest tool failed ({})", named(failed, "tool")));
        }
        r.evidence = one_line(&last.text);
        return Ok(Some(r));
    }
    if let Some(error) = api_error(c, s, &later, &t.text)? {
        r.signals = vec!["side chat's final reply is an API error".into()];
        r.evidence = error;
        return Ok(Some(r));
    }
    let (open, unknown) = todos(c, events)?;
    r.caveats = unknown.into_iter().collect();
    if !open.is_empty() {
        r.status = "todo_open";
        r.signals = vec![format!(
            "side chat todo list has {} open item{}",
            open.len(),
            plural(open.len())
        )];
        r.evidence = open[0].clone();
        r.items = open;
        return Ok(Some(r));
    }
    if let Some(trailing) = trailing_call(&later) {
        r.signals = vec![format!(
            "side chat's reply ended before its last tool call ({})",
            named(trailing, "tool")
        )];
        r.evidence = final_sentence(&prose(&t.text));
        return Ok(Some(r));
    }
    r.status = "unknown";
    if let Some(missing) = t.missing {
        r.signals = vec![format!("side chat's {missing}")];
        r.evidence = final_sentence(&prose(&t.text));
        return Ok(Some(r));
    }
    if !r.caveats.is_empty() {
        r.signals = r
            .caveats
            .iter()
            .map(|c| format!("side chat's {c}"))
            .collect();
        r.evidence = one_line(&last.text);
        return Ok(Some(r));
    }
    Ok(None)
}
/// 0.4.9's `_pending_item` (py/summary.py:1955-1993) for one chat's classification.
fn item(s: &Session, last: &Row, r: Record, source: &str) -> Value {
    let confidence = r.confidence.unwrap_or(match r.status {
        "waiting_on_user" | "open_next_steps" => "high",
        "agent_work_incomplete" | "todo_open" => "medium",
        _ => "low",
    });
    json!({"kind":"pending","status":r.status,"confidence":confidence,"project":history::project_leaf(&s.project),"project_label":s.project,"agent":s.agent,"session":s.id,"session_handle":history::target(&s.id),"last_ts":s.last_ts,"turn":last.turn,"handle":history::handle(&s.id,last),"source":source,"signals":r.signals,"evidence":r.evidence.chars().take(240).collect::<String>(),"items":r.items,"first_text":s.title,"caveats":r.caveats})
}
pub fn pending(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
) -> Result<Option<Value>> {
    let Some(last) = last_turn(rows) else {
        return Ok(None);
    };
    Ok(classify_root(c, s, rows, events, last)?.map(|r| item(s, last, r, "root")))
}
/// A side chat's pending item, worded for its root's briefing.
pub fn side_pending(
    c: &Connection,
    s: &Session,
    rows: &[Row],
    events: &[Event],
) -> Result<Option<Value>> {
    let Some(last) = last_turn(rows) else {
        return Ok(None);
    };
    Ok(classify_side(c, s, rows, events, last)?.map(|r| item(s, last, r, "side-chat")))
}
/// A chat of a family: its session, rows and events.
pub type Member<'a> = (&'a Session, Vec<Row>, Vec<Event<'a>>);
/// Which members of a family (`members[0]` its root) handed their result back to the root:
/// a side chat that yielded it, or whose final reply a reached member's delegation returned.
pub fn handed_back(c: &Connection, members: &[Member<'_>]) -> Result<Vec<bool>> {
    let mut reached = vec![false; members.len()];
    let mut holders = vec![0];
    reached[0] = true;
    for (at, (_, rows, events)) in members.iter().enumerate().skip(1) {
        if yielded(c, rows, events)? {
            reached[at] = true;
            holders.push(at);
        }
    }
    let finals: Vec<String> = members
        .iter()
        .map(|(_, rows, _)| {
            rows.iter()
                .rev()
                .find(|r| r.who == "agent")
                .map(|r| history::one_line(&r.text, usize::MAX))
                .unwrap_or_default()
        })
        .collect();
    while let Some(holder) = holders.pop() {
        let events = &members[holder].2;
        let results: HashSet<&str> = events
            .iter()
            .filter(|e| e.kind == "subagent_result" && e.ok != Some(false))
            .map(|e| e.child_session.as_str())
            .collect();
        // Each delegation call's linked child and flattened output, decoded once a side chat
        // has a final reply long enough to match.
        let mut delegations: Option<Vec<(&str, String)>> = None;
        for side in 1..members.len() {
            if reached[side] {
                continue;
            }
            let id = members[side].0.id.as_str();
            let mut received = results.contains(id);
            if !received && finals[side].len() >= 20 {
                if delegations.is_none() {
                    let mut calls = vec![];
                    for e in events.iter().filter(|e| {
                        matches!(
                            e.name,
                            "task" | "agent" | "subagent" | "wait_agent" | "Task"
                        )
                    }) {
                        calls.push((
                            e.child_session.as_str(),
                            history::one_line(e.output(c)?, usize::MAX),
                        ));
                    }
                    delegations = Some(calls);
                }
                received = delegations.iter().flatten().any(|(child, output)| {
                    (child.is_empty() || *child == id) && output.contains(&finals[side])
                });
            }
            if received {
                reached[side] = true;
                holders.push(side);
            }
        }
    }
    Ok(reached)
}
/// Whether the side chat's last work call is a `yield` that handed its result back.
fn yielded(c: &Connection, rows: &[Row], events: &[Event]) -> Result<bool> {
    let Some(last) = events
        .iter()
        .filter(|e| matches!(e.kind, "tool" | "subagent_result"))
        .max_by_key(|e| e.ts)
    else {
        return Ok(false);
    };
    if !last.name.eq_ignore_ascii_case("yield") || last.ok == Some(false) {
        return Ok(false);
    }
    if rows.iter().any(|r| r.who != "agent" && r.ts > last.ts) {
        return Ok(false);
    }
    Ok(!serde_json::from_str::<Value>(last.input(c)?)
        .ok()
        .is_some_and(|v| v["type"].is_array()))
}
/// Whether a root event linked to the side chat is its result or returned output.
pub fn delivered(c: &Connection, root: &[Event], side: &Session) -> Result<bool> {
    for e in root.iter().filter(|e| e.child_session == side.id) {
        if e.kind == "subagent_result" || !e.output(c)?.is_empty() {
            return Ok(true);
        }
    }
    Ok(false)
}
#[cfg(test)]
mod tests {
    use super::*;
    fn event(name: &'static str, state: Value) -> Event<'static> {
        Event {
            ts: 0,
            kind: "tool",
            name,
            ok: Some(true),
            child_session: String::new(),
            meta: String::new(),
            state: state.to_string(),
            input_chars: 0,
            input_end: None,
            output_start: None,
            blob: None,
            text: OnceCell::new(),
        }
    }
    fn open(events: &[Event]) -> Vec<String> {
        todos(&Connection::open_in_memory().unwrap(), events)
            .unwrap()
            .0
    }
    #[test]
    fn whole_lists_and_partial_merges_preserve_fields() {
        let initial = event(
            "todo_write",
            json!({"todos":[{"id":"a","content":"keep name","status":"in_progress"},{"id":2,"content":"second","status":"pending"}]}),
        );
        let merge = event(
            "todo_write",
            json!({"merge":true,"todos":[{"id":"a","status":"completed"}]}),
        );
        assert_eq!(open(&[initial, merge]), vec!["second"]);
        assert_eq!(
            open(&[event(
                "write_todos",
                json!({"todos":[{"description":"Gemini task","status":"In Progress"}]})
            )]),
            vec!["Gemini task"]
        );
    }
    #[test]
    fn phase_operations_and_failed_updates_are_evidence() {
        let init = || {
            event(
                "todo",
                json!({"list":[{"phase":"build","items":["compile","test"]}]}),
            )
        };
        let done = event("todo", json!({"op":"done","phase":"build"}));
        assert!(open(&[init(), done]).is_empty());
        let mut failed = event("todo", json!({"op":"done","phase":"build"}));
        failed.ok = Some(false);
        assert_eq!(open(&[init(), failed]), vec!["compile", "test"]);
    }
    // Expected values are 0.4.9's own helpers in py/summary.py.
    #[test]
    fn completed_checklists_and_later_work_do_not_reopen() {
        assert_eq!(
            section("Next steps:\n- [x] done\n- None"),
            Some(("next steps".into(), vec![], 1))
        );
        let text = "Next steps:\n- run tests All tests passed.";
        let parts = blocks(text, &[23]);
        assert_eq!(parts, ["Next steps:\n- run tests", "All tests passed."]);
        assert!(open_section(&parts, &[1]).is_none());
        assert_eq!(
            section("## Follow-ups\n- check `x` (done)\n- [ ] wire the CLI. Let me know if you want more."),
            Some(("follow-ups".into(), vec!["wire the CLI.".into()], 1))
        );
        assert_eq!(
            final_sentence(&prose(
                "Done.\n\nSee https://x.y/z?a=1 for **details**. Shall we ship it?"
            )),
            "Shall we ship it?"
        );
        let asked = question_head;
        assert_eq!(
            asked(
                r#"{"questions":[{"question":"Pick one?"}]}"#,
                "AskUserQuestion"
            ),
            "Pick one?"
        );
        assert_eq!(
            asked(r#"{"plan": "step one\nstep tw"#, "ExitPlanMode"),
            "step one step tw"
        );
        assert_eq!(asked(r#"{"x": 1"#, "ExitPlanMode"), "presented a plan");
        assert_eq!(
            repr("I'll check the 'legacy' schema \"before\" that."),
            r#"'I\'ll check the \'legacy\' schema "before" that.'"#
        );
        assert_eq!(repr("it's\u{200d}\u{a0}é"), r#""it's\u200d\xa0é""#);
    }
    #[test]
    fn interval_union_counts_overlap_once() {
        assert_eq!(
            merge(vec![(0, 20), (10, 30), (30, 40), (50, 50)]),
            vec![(0, 40)]
        );
    }
}
