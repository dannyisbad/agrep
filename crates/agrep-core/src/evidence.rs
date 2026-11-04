//! Canonical transcript windows, read together with events in one SQLite snapshot.
use crate::{
    handles::{self, Claim, SessionIndex},
    query::content_digest,
    store, surface,
};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Row {
    pub session: String,
    pub turn: i64,
    pub who: String,
    pub ts: i64,
    pub text: String,
    pub model: String,
    pub model_source: String,
    pub source: i64,
    pub source_chars: usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub turn: i64,
    pub ts: i64,
    pub name: String,
    pub kind: String,
    pub input: String,
    pub output: String,
    pub input_chars: usize,
    pub output_chars: usize,
    pub output_bytes: Option<usize>,
    pub ok: Option<bool>,
    pub identity: String,
    pub text: String,
    pub raw: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Window {
    #[serde(skip)]
    pub target: String,
    pub session: String,
    pub agent: String,
    pub project: String,
    pub title: String,
    pub family: String,
    pub side: bool,
    pub center: i64,
    pub radius: Option<i64>,
    pub latest: bool,
    pub first: i64,
    pub last: i64,
    pub rows: Vec<Row>,
    pub events: Vec<Event>,
    pub notes: Vec<Value>,
    pub claim: Option<Claim>,
}
#[derive(Clone, Debug)]
pub struct Request {
    pub target: String,
    pub turn: Option<i64>,
    pub radius: Option<i64>,
    pub exact: bool,
}
impl Request {
    pub fn new(target: &str, turn: Option<i64>, radius: Option<i64>) -> Self {
        Self {
            target: target.into(),
            turn,
            radius,
            exact: handles::is_handle(target) || (target.starts_with('@') && target.contains(':')),
        }
    }
}
pub fn load(c: &Connection, request: &Request) -> Result<Window> {
    load_in(c, &SessionIndex::load(c)?, request)
}
fn load_in(c: &Connection, index: &SessionIndex, request: &Request) -> Result<Window> {
    let claim = if request.exact {
        Some(handles::parse(&request.target)?)
    } else {
        None
    };
    if claim.is_some() && request.turn.is_some() {
        bail!("a result handle already includes its turn")
    }
    let (target, requested) = if let Some(claim) = &claim {
        (claim.session.clone(), Some(claim.turn))
    } else if request.turn.is_none() {
        match request.target.rsplit_once(':') {
            Some((s, t)) if t.parse::<i64>().is_ok() => (s.to_owned(), Some(t.parse()?)),
            _ => (request.target.clone(), None),
        }
    } else {
        (request.target.clone(), request.turn)
    };
    let candidates = index.candidates(c, &target)?;
    if candidates.is_empty() {
        bail!("no indexed session matches {target}; rerun the search for a current handle")
    }
    let session = if candidates.len() == 1 {
        candidates[0].clone()
    } else if let Some(ref claim) = claim {
        let holders: Vec<_> = candidates
            .iter()
            .filter(|s| {
                claim.digest.as_ref().is_some_and(|d| {
                    has_claim(c, s, claim.turn, d, claim.event.as_deref()).unwrap_or(false)
                })
            })
            .cloned()
            .collect();
        if holders.len() != 1 {
            bail!(
                "result handle session is ambiguous: {}",
                candidates.join(", ")
            )
        }
        holders[0].clone()
    } else {
        bail!("session is ambiguous: {}", candidates.join(", "))
    };
    let (first, last): (Option<i64>, Option<i64>) = c.query_row(
        "SELECT min(position),max(position) FROM prose WHERE session=? AND visible=1",
        [&session],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let first = first.context("session has no indexed turns")?;
    let last = last.unwrap_or(first);
    let requested = requested.unwrap_or(last);
    let mut center = requested.clamp(first, last);
    let mut notes = Vec::new();
    if center != requested {
        if request.exact {
            bail!("result handle is stale: turn {requested} is outside chat turns {first}-{last}; rerun the search for a current handle")
        }
        notes.push(json!({"kind":"turn_clamped","requested":requested,"served":center,"note":format!("turn {requested} clamped to {center} (chat has turns {first}-{last})")}));
    }
    if let Some(claim) = &claim {
        if let Some(digest) = &claim.digest {
            if !has_claim(c, &session, center, digest, claim.event.as_deref())? {
                let radius = request.radius.unwrap_or(last - first).max(4);
                let positions:Vec<i64>=c.prepare("SELECT DISTINCT position FROM prose WHERE session=? AND visible=1 AND position BETWEEN ? AND ? ORDER BY position")?.query_map(params![session,center.saturating_sub(radius),center.saturating_add(radius)],|r|r.get(0))?.collect::<rusqlite::Result<_>>()?;
                let mut holders = Vec::new();
                for p in positions {
                    if has_claim(c, &session, p, digest, claim.event.as_deref())? {
                        holders.push(p)
                    }
                }
                match holders.len(){0=>bail!("result handle no longer names the cited content; rerun the search for a current handle"),1=>{center=holders[0];notes.push(json!({"kind":"content_moved","requested":requested,"served":center,"note":format!("handle content moved: turn {requested} → {center} (session was renumbered)")}));},_=>bail!("handle content is ambiguous in session {}: turns {}; rerun the search for a current handle",SessionIndex::default().target(&session),holders.iter().map(i64::to_string).collect::<Vec<_>>().join(", "))}
            }
        }
    }
    let (lo, hi) = request
        .radius
        .map(|r| (center.saturating_sub(r), center.saturating_add(r)))
        .unwrap_or((first, last));
    let (agent, project, title, family, side) = c.query_row(
        "SELECT agent,project,title,family,side FROM sessions WHERE id=?",
        [&session],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
    )?;
    let rows = load_rows(c, &session, lo, hi)?;
    let events = load_events(c, &session, lo, hi)?;
    Ok(Window {
        target: index.target(&session),
        session,
        agent,
        project,
        title,
        family,
        side,
        center,
        radius: request.radius,
        latest: requested == last && request.turn.is_none() && claim.is_none(),
        first,
        last,
        rows,
        events,
        notes,
        claim,
    })
}
fn has_claim(
    c: &Connection,
    session: &str,
    turn: i64,
    digest: &str,
    event: Option<&str>,
) -> Result<bool> {
    for table in ["prose", "tools"] {
        if event.is_some() && table == "prose" {
            continue;
        }
        let mut q = c.prepare(&format!(
            "SELECT text,ts FROM {table} WHERE session=? AND position=? AND visible=1"
        ))?;
        for row in q.query_map(params![session, turn], |r| {
            Ok((r.get::<_, Vec<u8>>(0)?, r.get::<_, i64>(1)?))
        })? {
            let (raw, ts) = row?;
            let text = store::decode(table, raw)?;
            if content_digest(&text) == digest
                && event.is_none_or(|id| handles::event_id(session, ts, &text) == id)
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}
pub fn load_rows(c: &Connection, session: &str, lo: i64, hi: i64) -> Result<Vec<Row>> {
    let mut q=c.prepare("SELECT p.session,p.position,p.who,p.ts,p.text,p.model,p.model_source,p.source,s.body FROM prose p LEFT JOIN reply_shapes s ON s.source=p.source AND s.session=p.session AND s.turn=p.turn WHERE p.visible=1 AND p.session=? AND p.position BETWEEN ? AND ? ORDER BY p.position,CASE p.who WHEN 'agent' THEN 1 ELSE 0 END,p.ts,p.id")?;
    let mut rows = Vec::new();
    for row in q.query_map(params![session, lo, hi], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, i64>(3)?,
            r.get::<_, Vec<u8>>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
            r.get::<_, i64>(7)?,
            r.get::<_, Option<String>>(8)?,
        ))
    })? {
        let (session, turn, who, ts, bytes, model, model_source, source, shape) = row?;
        let text = store::decode("prose", bytes)?;
        let source_chars = if who == "agent" {
            shape
                .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                .and_then(|v| v["original_reply_chars"].as_u64())
                .map(|v| v as usize)
                .unwrap_or_else(|| text.chars().count())
        } else {
            text.chars().count()
        };
        rows.push(Row {
            session,
            turn,
            who,
            ts,
            text,
            model,
            model_source,
            source,
            source_chars,
        });
    }
    Ok(rows)
}
pub fn load_events(c: &Connection, session: &str, lo: i64, hi: i64) -> Result<Vec<Event>> {
    let mut q=c.prepare("SELECT e.position,e.ts,e.text,e.body FROM events e WHERE e.visible=1 AND e.session=? AND e.position BETWEEN ? AND ? ORDER BY e.position,e.ts,e.call_id")?;
    let mut events = Vec::new();
    let mut seen = BTreeSet::new();
    for r in q.query_map(params![session, lo, hi], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, Option<Vec<u8>>>(2)?,
            r.get::<_, String>(3)?,
        ))
    })? {
        let (turn, ts, bytes, body) = r?;
        let text = bytes
            .map(|bytes| store::decode("tools", bytes))
            .transpose()?
            .unwrap_or_default();
        let raw: Value = serde_json::from_str(&body)?;
        let string = |key: &str| raw[key].as_str().unwrap_or_default().to_owned();
        let identity = handles::event_id(session, ts, if text.is_empty() { &body } else { &text });
        if !seen.insert(identity.clone()) {
            continue;
        }
        let (input, output) = crate::tool_text::split(
            &text,
            raw["kind"].as_str().unwrap_or("tool"),
            &string("name"),
            raw["ok"].as_bool(),
            raw["input_end"].as_u64().map(|v| v as usize),
            raw["output_start"].as_u64().map(|v| v as usize),
        );
        events.push(Event {
            turn,
            ts,
            name: string("name"),
            kind: raw["kind"].as_str().unwrap_or("tool").into(),
            input_chars: raw["input_chars"]
                .as_u64()
                .map_or_else(|| input.chars().count(), |v| v as usize),
            output_chars: raw["output_chars"]
                .as_u64()
                .map_or_else(|| output.chars().count(), |v| v as usize),
            output_bytes: raw["output_bytes"].as_u64().map(|v| v as usize),
            ok: raw["ok"].as_bool(),
            input,
            output,
            identity,
            text,
            raw,
        });
    }
    Ok(events)
}
/// Batch all windows on the same connection/snapshot; no source file is reopened.
pub fn windows(c: &Connection, requests: &[Request]) -> Result<Vec<Window>> {
    let index = SessionIndex::load(c)?;
    requests.iter().map(|r| load_in(c, &index, r)).collect()
}
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
    fn find(&self, needle: char, from: usize) -> Option<usize> {
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
#[derive(Clone, Debug)]
pub struct Display {
    pub caller: crate::caller::Caller,
    pub json: bool,
    pub full: bool,
    pub max_chars: usize,
    pub explicit_max_chars: bool,
    pub no_tools: bool,
    pub tool_output: usize,
    pub who: Option<String>,
    pub color: bool,
    pub checked: bool,
    pub pending_sources: usize,
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
/// 0.4.9's `_tool_line` without color or output lines: a tool event's glyph, name, input
/// and output preview with its size, or the selected match where it fell outside the output.
pub fn tool_line(e: &Event, output_span: Option<(usize, usize)>, preview: Option<&str>) -> String {
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
    let body = match e.kind.as_str() {
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
            let out = if shown.is_empty() {
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
    };
    format!("  {body}")
}
pub fn render(w: &Window, d: &Display) -> Result<String> {
    if d.no_tools && w.claim.as_ref().is_some_and(|c| c.event.is_some()) {
        bail!("--no-tools conflicts with this tool result handle because it would erase the cited evidence")
    }
    let target = w.target.clone();
    let selected = |r: &&Row| {
        d.who
            .as_ref()
            .map(|who| who.split(',').any(|s| s == r.who))
            .unwrap_or_else(|| {
                d.full
                    || ["user", "agent"].contains(&r.who.as_str())
                    || w.side
                    || w.claim.as_ref().is_some_and(|cl| {
                        cl.digest
                            .as_ref()
                            .is_some_and(|dg| r.turn == w.center && *dg == content_digest(&r.text))
                    })
            })
    };
    let rows: Vec<_> = w.rows.iter().filter(selected).collect();
    let tool_mode = if d.no_tools {
        "excluded"
    } else if d.full || d.tool_output > 0 || d.who.as_deref() == Some("tool") {
        "all"
    } else if w.claim.as_ref().is_some_and(|c| c.event.is_some()) {
        "selected"
    } else {
        "excluded"
    };
    let events: Vec<_> = w
        .events
        .iter()
        .filter(|e| {
            tool_mode == "all"
                || (tool_mode == "selected"
                    && w.claim
                        .as_ref()
                        .and_then(|c| c.event.as_ref())
                        .is_some_and(|id| id == &e.identity))
        })
        .collect();
    let row_cap = if d.full
        || (w.side
            && w.claim
                .as_ref()
                .is_some_and(|c| c.digest.is_some() && c.event.is_none())
            && !d.explicit_max_chars)
    {
        0
    } else {
        d.max_chars
    };
    let mut rendered = Vec::new();
    let mut truncated = 0;
    for r in &rows {
        let cmd = format!(
            "agrep around {target} {} -C 0 --max-chars 0{}",
            r.turn,
            if ["user", "agent"].contains(&r.who.as_str()) {
                String::new()
            } else {
                format!(" --who {}", r.who)
            }
        );
        let source = if !d.json && row_cap > 0 && r.who != "agent" {
            r.text.split_whitespace().collect::<Vec<_>>().join(" ")
        } else {
            r.text.clone()
        };
        let (text, omitted) = cap(&source, row_cap, &cmd);
        truncated += usize::from(omitted > 0);
        rendered.push((*r, text, omitted));
    }
    let expansion = vec![
        "agrep".to_owned(),
        "around".into(),
        target.clone(),
        w.center.to_string(),
        "-C".into(),
        w.radius
            .unwrap_or((w.center - w.first).max(w.last - w.center))
            .to_string(),
        "--full".into(),
    ];
    let mut out = String::new();
    if d.json {
        let counts = |rs: &[&Row]| {
            let mut v = BTreeMap::<String, usize>::new();
            for r in rs {
                *v.entry(r.who.clone()).or_default() += 1;
            }
            v
        };
        let all: Vec<_> = w.rows.iter().collect();
        let mut meta = json!({"kind":"agrep-meta","scope":{"policy":if d.full{"forensic"}else if w.claim.is_some(){"bounded_inclusive"}else{"root_prose"},"session":w.session,"selected_session_role":if w.side{"delegated"}else{"root_main"},"selected_record_role":if w.claim.is_none(){"none"}else if w.claim.as_ref().is_some_and(|c|c.event.is_some()){"tool"}else{"prose_turn_inclusive"},"center_turn":w.center,"radius":w.radius.unwrap_or((w.center-w.first).max(w.last-w.center)),"bounds":{"first_indexed_turn":w.first,"last_indexed_turn":w.last},"selection_order":if w.radius.is_none(){"whole_chat"}else if w.latest{"newest_tail"}else{"requested_center"},"render_order":"chronological","family_widened":false,"project_or_folder_ranked":false,"counts_state":"exact","tools":{"mode":tool_mode,"available":w.events.len(),"shown":events.len(),"hidden":w.events.len()-events.len()},"prose":{"available_by_role":counts(&all),"shown_by_role":counts(&rows),"hidden":w.rows.len()-rows.len()},"adoption":if w.side{"unresolved"}else{"root_main"},"truncation":{"message_cap_chars":if row_cap==0{None}else{Some(row_cap)},"message_cap_state":if d.full{"forensic_uncapped"}else if row_cap==0{"explicit_uncapped"}else{"bounded"},"render_truncated_rows":truncated,"source_truncated_agent_rows":w.rows.iter().filter(|r|r.reply_truncated()).count(),"source_truncated_events":w.events.iter().filter(|e|e.input_truncated()||e.output_truncated()).count()},"expansion_argv":expansion}});
        meta["self_exclusion"] = d.caller.metadata(
            crate::caller::Request {
                waived: Some("chat-filter"),
                ..Default::default()
            },
            None,
        );
        meta["freshness"] = crate::render::freshness(d.checked, d.pending_sources);
        if !w.notes.is_empty() {
            meta["served"] = json!({"served_as_requested":false,"divergences":w.notes});
        }
        out += &format!("{}\n", crate::json_output::to_string(&meta)?);
    } else {
        let head = [target.as_str(), &w.agent, &w.project]
            .into_iter()
            .filter(|s| !s.is_empty())
            .map(|s| surface::safe(s, false))
            .collect::<Vec<_>>()
            .join(" · ");
        let lo = w.rows.first().map(|r| r.turn).unwrap_or(w.center);
        let hi = w.rows.last().map(|r| r.turn).unwrap_or(w.center);
        out += &format!("{head}  turns {lo}-{hi} of {}-{}\n", w.first, w.last);
        if w.events.len() > events.len() || w.rows.len() > rows.len() || w.side {
            let mut parts = Vec::new();
            if w.side {
                parts.push("subagent conversation".into())
            }
            if w.events.len() > events.len() {
                parts.push(format!(
                    "{} tool events omitted",
                    w.events.len() - events.len()
                ))
            }
            if w.rows.len() > rows.len() {
                parts.push(format!("{} messages omitted", w.rows.len() - rows.len()))
            }
            out += &format!("{} · {}\n", parts.join(" · "), surface::shell(&expansion));
        }
    }
    let turns: BTreeSet<_> = rows
        .iter()
        .map(|r| r.turn)
        .chain(events.iter().map(|e| e.turn))
        .collect();
    for turn in turns {
        if !d.json {
            let ts = rows
                .iter()
                .find(|r| r.turn == turn)
                .map(|r| r.ts)
                .unwrap_or(0);
            out += &format!(
                "── turn {turn} {} {}\n",
                "─".repeat(40),
                surface::timestamp(ts)
            );
        }
        for (r, text, omitted) in rendered
            .iter()
            .filter(|(r, _, _)| r.turn == turn && (d.json || r.who != "agent"))
        {
            out += &prose_output(w, d, r, text, *omitted);
        }
        for e in events.iter().filter(|e| e.turn == turn) {
            let selected = w
                .claim
                .as_ref()
                .and_then(|c| c.event.as_ref())
                .is_some_and(|id| id == &e.identity);
            let preview = if selected {
                w.claim
                    .as_ref()
                    .and_then(|c| c.span)
                    .filter(|(_, end)| *end <= e.text.chars().count())
                    .map(|(start, end)| {
                        e.text
                            .chars()
                            .skip(start.saturating_sub(60))
                            .take((end - start + 120).min(360))
                            .collect::<String>()
                    })
            } else {
                None
            };
            if d.json {
                let mut v = json!({"kind":e.kind,"session":w.session,"project":w.project,"turn":e.turn,"ts":e.ts,"name":e.name,"input":e.input,"ok":e.ok,"input_chars":e.input_chars,"output_chars":e.output_chars,"output_bytes":e.output_bytes,"input_truncated":e.input_truncated(),"output_truncated":e.output_truncated()});
                if let Some(p) = preview {
                    v["match_preview"] = json!(p);
                }
                if d.tool_output > 0 {
                    v["output"] = json!(e.output.chars().take(d.tool_output).collect::<String>())
                }
                out += &format!("{}\n", crate::json_output::to_string(&v)?);
            } else {
                let input = surface::clip(
                    &surface::safe(preview.as_deref().unwrap_or(&e.input), false),
                    240,
                );
                let output = if d.tool_output == 0 && preview.is_none() && !e.output.is_empty() {
                    format!(" {}", surface::clip(&surface::safe(&e.output, false), 240))
                } else {
                    String::new()
                };
                let size = e
                    .output_bytes
                    .map(|n| format!(" ({}B)", surface::number(n)))
                    .unwrap_or_default();
                out += &format!(
                    "  {} {} {input}{output}{size}\n",
                    if e.ok == Some(false) {
                        "✗ FAILED"
                    } else {
                        "⚙"
                    },
                    surface::safe(&e.name, false)
                );
                if d.tool_output > 0 {
                    out += &format!(
                        "    {}{}\n",
                        surface::safe(
                            &e.output.chars().take(d.tool_output).collect::<String>(),
                            true
                        )
                        .replace('\n', "\n    "),
                        if e.output_truncated() {
                            " [source truncated at ingest]"
                        } else {
                            ""
                        }
                    )
                }
            }
        }
        for (r, text, omitted) in rendered
            .iter()
            .filter(|(r, _, _)| r.turn == turn && !d.json && r.who == "agent")
        {
            out += &prose_output(w, d, r, text, *omitted);
        }
    }
    Ok(out)
}

fn prose_output(w: &Window, d: &Display, r: &Row, text: &str, omitted: usize) -> String {
    if d.json {
        let mut v = json!({"kind":"msg","self":d.caller.resolved && d.caller.agent == w.agent && d.caller.session == w.session,"session":w.session,"project":w.project,"turn":r.turn,"who":r.who,"ts":r.ts,"text":text,"omitted_chars":omitted});
        if r.who == "agent" {
            v["reply_chars"] = json!(r.source_chars);
            v["reply_truncated"] = json!(r.reply_truncated())
        }
        return format!(
            "{}\n",
            crate::json_output::to_string(&v).expect("serializing JSON value")
        );
    }
    let loss = if r.reply_truncated() {
        format!(
            " [source truncated at ingest; original reply {} chars]",
            surface::number(r.source_chars)
        )
    } else {
        String::new()
    };
    let text = surface::safe(text, true);
    if d.color {
        format!(
            "{}{}:\x1b[0m {text}{loss}\n",
            if r.who == "agent" {
                "\x1b[36m"
            } else {
                "\x1b[33m"
            },
            r.who
        )
    } else {
        format!("{}: {text}{loss}\n", r.who)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::data::DataRoot;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };

    #[test]
    fn a_window_reads_controls_without_materializing_unrelated_tool_rows() {
        let mut work = Vec::new();
        for count in [2048, 8192] {
            let temp = tempfile::tempdir().unwrap();
            let root = DataRoot::open(temp.path(), true).unwrap();
            let mut c = store::writer(&root).unwrap();
            let tx = c.transaction().unwrap();
            tx.execute_batch("INSERT INTO sources VALUES('fixture','pi',0,0,'',NULL); INSERT INTO event_sessions VALUES(1,'pi','selected'),(2,'pi','unrelated'); INSERT INTO symbols VALUES(1,'tool'),(2,'shell'),(3,'control'),(4,'compaction');").unwrap();
            tx.execute(
                "INSERT INTO tool_blobs(id,digest,body) VALUES(1,X'01',?)",
                [store::encode_tool("shell: needle").unwrap()],
            )
            .unwrap();
            for id in 1..=count {
                tx.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,input_end,project_key,blob) VALUES(?1,?1,1,?2,?3,1,2,?1,?4,?4,6,0,0,'','','',13,0,1)", params![id, if id <= 2 {1} else {2}, id.to_string(), id-1]).unwrap();
            }
            tx.execute_batch("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key) VALUES(90000,90000,1,1,'control',3,4,7,1,1,0,0,0,'','','marker',0)").unwrap();
            tx.commit().unwrap();
            let operations = Arc::new(AtomicUsize::new(0));
            let recorded = operations.clone();
            c.progress_handler(
                100,
                Some(move || {
                    recorded.fetch_add(100, Ordering::Relaxed);
                    false
                }),
            );
            let events = load_events(&c, "selected", 0, 1).unwrap();
            assert_eq!(events.len(), 3);
            let control = events.iter().find(|e| e.kind == "control").unwrap();
            assert_eq!((control.ts, control.turn), (7, 1));
            assert_eq!(events.iter().filter(|e| e.input == "needle").count(), 2);
            work.push(operations.load(Ordering::Relaxed));
        }
        assert!(work[1] <= work[0] * 2 + 100, "{work:?}");
    }
}
#[cfg(test)]
mod weak_snippet_tests {
    fn weak(text: &str, fallback: &str, query: &str) -> String {
        let anchors = super::anchor_patterns(query);
        super::weak_snippet(text, fallback, &anchors.iter().collect::<Vec<_>>())
    }
    // Expected lines were rendered by agrep 0.4.9's `_weak_line` (py/recall.py).
    #[test]
    fn weak_snippets_anchor_like_0_4_9() {
        let text = format!(
            "intro {}the Flaky   Windows cache broke again {}",
            "filler word ".repeat(20),
            "tail word ".repeat(20)
        );
        assert_eq!(
            weak(&text, "", "flaky windows cache"),
            "…er word filler word filler word filler word filler word the Flaky Windows cache broke again tail word tail word tail word tail word tail wo…"
        );
        assert_eq!(
            weak(
                &"no match here ".repeat(20),
                "snippet mentions windows twice windows",
                "windows"
            ),
            "snippet mentions windows twice windows"
        );
        assert_eq!(weak("short text", "", "zzz"), "short text");
    }
}
#[cfg(test)]
mod parity_tests {
    use super::*;
    // Expected values were produced by agrep 0.4.9's own `_cap` and `_tool_line` with
    // `_selected_tool_match` (py/around.py) and `_anchored_cap` with `_anchor_patterns`
    // (py/recall.py).
    #[test]
    fn caps_and_anchored_caps_match_0_4_9() {
        let words: Vec<String> = (0..80).map(|i| format!("w{i:03}")).collect();
        let words = words.join(" ");
        // A space exactly 200 characters before the limit still ends the kept text.
        let edge = format!("{} {}", "a".repeat(50), "b".repeat(250));
        assert_eq!(
            cap(&edge, 250, "CMD"),
            (format!("{} [+251 chars - CMD]", "a".repeat(50)), 251)
        );
        assert_eq!(
            cap(&words, 120, "CMD"),
            ("w000 w001 w002 w003 w004 w005 w006 w007 w008 w009 w010 w011 w012 w013 w014 w015 w016 w017 w018 w019 w020 w021 w022 w023 [+280 chars - CMD]".into(), 280)
        );
        assert_eq!(
            cap(&words, 60, "CMD"),
            (
                "w000 w001 w002 w003 w004 w005 w006 w007 w008 w009 w010 w011 [+340 chars - CMD]"
                    .into(),
                340
            )
        );
        let anchors = anchor_patterns("copper falcon migration");
        let anchors: Vec<&Anchor> = anchors.iter().collect();
        let long = format!(
            "{}the copper falcon migration stalled {} copper falcon migration again {}",
            "lead ".repeat(60),
            "tail ".repeat(200),
            "end ".repeat(100)
        );
        let lead = |n: usize| "lead ".repeat(n).trim_end().to_owned();
        let tail = |n: usize| "tail ".repeat(n).trim_end().to_owned();
        assert_eq!(
            anchored_cap(&long, 500, "CMD", &anchors),
            (format!("[210 chars] {} the copper falcon migration stalled {} [826 chars] {}  copper falcon migration again {} [313 chars] [snippet - open: CMD]", lead(18), tail(17), tail(18), "end ".repeat(22).trim_end()), 1349)
        );
        assert_eq!(
            anchored_cap(&long, 260, "CMD", &anchors),
            (format!("[+260 chars - CMD] {} the copper falcon migration stalled {} [+1,252 chars - CMD]", lead(8), tail(36)), 1512)
        );
        let late = format!("{}falcon here {}", "x ".repeat(300), "y ".repeat(50));
        assert_eq!(
            anchored_cap(&late, 200, "CMD", &anchors),
            (
                format!(
                    "[+554 chars - CMD] {}falcon here {}",
                    "x ".repeat(23),
                    "y ".repeat(50)
                ),
                554
            )
        );
    }
    #[test]
    fn selected_tool_events_render_0_4_9_lines() {
        let output = format!(
            "line one\n{}ERROR copper falcon migration timeout\n{}",
            "pad ".repeat(40),
            "tail ".repeat(30)
        );
        let text = format!("bash [failed]: ./run --step 3\n{}", output.trim_end());
        let event = Event {
            turn: 3,
            ts: 1_700_000_000_000,
            name: "bash".into(),
            kind: "tool".into(),
            input: "./run --step 3".into(),
            output: output.trim_end().into(),
            input_chars: 14,
            output_chars: output.chars().count() + 1,
            output_bytes: Some(output.len() + 1),
            ok: Some(false),
            identity: crate::handles::event_id("sess-1", 1_700_000_000_000, &text),
            raw: serde_json::json!({"output_start": 30}),
            text,
        };
        let id = "71ab9121ac178bea371c414e";
        assert_eq!(event.identity, id);
        let found = selected_tool_match(&event, Some(id), Some((205, 228)));
        let pads = |n: usize| "pad ".repeat(n);
        assert_eq!(
            found.preview.as_deref(),
            Some(
                format!(
                    "…d {}ERROR copper falcon migration timeout {}t…",
                    pads(13),
                    "tail ".repeat(10)
                )
                .as_str()
            )
        );
        assert_eq!(
            tool_line(&event, found.output_span, found.preview.as_deref()),
            format!("  ✗ FAILED bash ./run --step 3 … {}ERROR copper falcon migration timeout {}… (358B)", pads(15), "tail ".repeat(12).trim_end())
        );
        let input = selected_tool_match(&event, Some(id), Some((17, 20)));
        assert_eq!(
            tool_line(&event, input.output_span, input.preview.as_deref()),
            format!("  ✗ FAILED bash …run · line one {}… (358B)", pads(27))
        );
        assert!(!selected_tool_match(&event, Some("other"), Some((17, 20))).selected);
    }
}
