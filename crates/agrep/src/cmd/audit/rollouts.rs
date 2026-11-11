//! The full audit's own reading of Codex rollouts. Every user and assistant message and every
//! tool call a rollout holds must be among the rows the index keeps for that file, less the
//! exclusions docs/BROWSE.md lists. The reading shares none of the parser's rules for where a
//! subagent's own transcript starts or which rows a turn makes, so a row the parser drops
//! shows up here as a gap.
use super::problems::{Problems, Seen};
use agrep_core::ingest::{codex::is_codex_injected, is_wrapper, REPLY_CAP};
use anyhow::Result;
use memchr::memmem;
use rayon::prelude::*;
use rusqlite::OptionalExtension;
use serde::Deserialize;
use serde_json::{json, value::RawValue, Value};
use sha2::{Digest, Sha256};
use std::{
    borrow::Cow,
    collections::{HashMap, HashSet},
    io::{BufRead, BufReader, Read},
    path::Path,
    sync::{Arc, OnceLock},
};

/// Rollout bytes on disk read per batch. A batch's readings meet the index before the next
/// batch is read, so memory holds one batch's messages rather than a whole history's.
const BATCH_BYTES: u64 = 256 << 20;

/// A rollout the census found that the index holds as it is now.
pub(super) struct Rollout {
    pub(super) path: String,
    pub(super) size: u64,
}

/// The rows the readings expect, and the expected rows the index lacks.
#[derive(Default)]
pub(super) struct Checked {
    pub(super) expected: usize,
    pub(super) gaps: Vec<Value>,
}

pub(super) fn check(
    c: &rusqlite::Connection,
    rollouts: &[Rollout],
    problems: &mut Problems,
) -> Result<Checked> {
    let ancestry = Ancestry::of(rollouts);
    let mut checked = Checked::default();
    let mut batch = Vec::new();
    let mut bytes = 0;
    for rollout in rollouts {
        if bytes + rollout.size > BATCH_BYTES && !batch.is_empty() {
            check_batch(
                c,
                &std::mem::take(&mut batch),
                &ancestry,
                problems,
                &mut checked,
            )?;
            bytes = 0;
        }
        bytes += rollout.size;
        batch.push(rollout.path.as_str());
    }
    check_batch(c, &batch, &ancestry, problems, &mut checked)?;
    checked.gaps.sort_by(|a, b| {
        (a["path"].as_str(), a["who"].as_str()).cmp(&(b["path"].as_str(), b["who"].as_str()))
    });
    Ok(checked)
}

fn check_batch(
    c: &rusqlite::Connection,
    paths: &[&str],
    ancestry: &Ancestry<'_>,
    problems: &mut Problems,
    checked: &mut Checked,
) -> Result<()> {
    let readings: Vec<_> = paths
        .par_iter()
        .map(|path| read_rollout(path, ancestry))
        .collect();
    let mut held = Vec::with_capacity(readings.len());
    for (path, reading) in paths.iter().zip(readings) {
        match reading {
            Ok(reading) => held.push((*path, Rows::load(c, path)?, reading)),
            Err(error) => problems.source(*path, format!("cannot read: {error}"), Seen::Reparse),
        }
    }
    let lacking: Vec<_> = held
        .par_iter()
        .map(|(_, rows, reading)| reading.lacking(rows))
        .collect();
    for ((path, _, reading), lacking) in held.iter().zip(lacking) {
        checked.expected += reading.expected();
        for (who, rows) in ["user", "agent", "tool"].into_iter().zip(lacking) {
            if rows > 0 {
                checked.gaps.push(json!({"agent":"codex","session":reading.session,"path":path,"who":who,"rows":rows,"reason":"rollout row missing from index"}));
            }
        }
    }
    Ok(())
}

/// Which rollouts hold each session, and each rollout's record digests once a fork child
/// needs them to tell its replay of that history from its own records.
struct Ancestry<'a> {
    by_session: HashMap<String, Vec<&'a str>>,
    digests: HashMap<&'a str, OnceLock<Arc<HashSet<[u8; 32]>>>>,
}

impl<'a> Ancestry<'a> {
    fn of(rollouts: &'a [Rollout]) -> Self {
        let ids: Vec<_> = rollouts
            .par_iter()
            .map(|rollout| session_of(Path::new(&rollout.path)))
            .collect();
        let mut by_session: HashMap<String, Vec<&str>> = HashMap::new();
        for (rollout, id) in rollouts.iter().zip(ids) {
            by_session.entry(id).or_default().push(&rollout.path);
        }
        let digests = rollouts
            .iter()
            .map(|rollout| (rollout.path.as_str(), OnceLock::new()))
            .collect();
        Self {
            by_session,
            digests,
        }
    }

    /// The digests of every record the rollouts of `sessions` hold, or `None` when no
    /// rollout of theirs is here.
    fn records_of(&self, sessions: &[String]) -> Option<HashSet<[u8; 32]>> {
        let paths: Vec<&str> = sessions
            .iter()
            .filter_map(|id| self.by_session.get(id))
            .flatten()
            .copied()
            .collect();
        if paths.is_empty() {
            return None;
        }
        let mut all = HashSet::new();
        for path in paths {
            let digests = self.digests[path].get_or_init(|| Arc::new(record_digests(path)));
            all.extend(digests.iter().copied());
        }
        Some(all)
    }
}

/// The session a rollout is: its first `session_meta`'s id, else its file name's.
fn session_of(path: &Path) -> String {
    let mut first = None;
    let _ = each_line(path, |ordinal, line| {
        if ordinal == 0 {
            first = serde_json::from_slice::<Record<'_>>(line)
                .ok()
                .filter(|record| record.ty.as_deref() == Some("session_meta"))
                .and_then(|record| serde_json::from_str::<Meta<'_>>(record.payload?.get()).ok())
                .and_then(|meta| meta.id.map(Cow::into_owned))
                .filter(|id| !id.is_empty());
        }
        false
    });
    first.unwrap_or_else(|| file_id(path))
}

/// The id at the end of a `rollout-<time>-<id>.jsonl[.zst]` file name.
fn file_id(path: &Path) -> String {
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
    let stem = name
        .strip_suffix(".zst")
        .unwrap_or(name)
        .strip_suffix(".jsonl")
        .unwrap_or(name);
    let parts: Vec<&str> = stem.split('-').collect();
    parts[parts.len().saturating_sub(5)..].join("-")
}

fn record_digests(path: &str) -> HashSet<[u8; 32]> {
    let mut digests = HashSet::new();
    let _ = each_line(Path::new(path), |_, line| {
        if let Ok(record) = serde_json::from_slice::<Record<'_>>(line) {
            if let (Some("response_item"), Some(payload)) = (record.ty.as_deref(), record.payload) {
                digests.extend(digest(payload));
            }
        }
        true
    });
    digests
}

/// Calls `each` with every line of the rollout at `path` and its ordinal, decompressing a
/// `.zst` rollout as it reads, until `each` returns false.
fn each_line(path: &Path, mut each: impl FnMut(usize, &[u8]) -> bool) -> std::io::Result<()> {
    let file = std::fs::File::open(path)?;
    let compressed = path.extension().is_some_and(|x| x == "zst");
    let reader: Box<dyn Read> = if compressed {
        Box::new(zstd::stream::read::Decoder::new(file)?)
    } else {
        Box::new(file)
    };
    let mut reader = BufReader::with_capacity(1 << 16, reader);
    let mut line = Vec::new();
    for ordinal in 0.. {
        line.clear();
        if reader.read_until(b'\n', &mut line)? == 0 {
            break;
        }
        let text = line.strip_suffix(b"\n").unwrap_or(&line);
        if !each(ordinal, text.strip_suffix(b"\r").unwrap_or(text)) {
            break;
        }
    }
    Ok(())
}

/// A payload's identity whatever the order of its keys: a fork child's copy of a parent
/// record is written anew.
fn digest(payload: &RawValue) -> Option<[u8; 32]> {
    let value: Value = serde_json::from_str(payload.get()).ok()?;
    let mut hasher = Sha256::new();
    canonical(&value, &mut hasher);
    Some(hasher.finalize().into())
}

fn canonical(value: &Value, hasher: &mut Sha256) {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<_> = map.iter().collect();
            keys.sort_unstable_by_key(|(key, _)| *key);
            hasher.update(b"{");
            for (key, value) in keys {
                hasher.update(Value::from(key.as_str()).to_string().as_bytes());
                hasher.update(b":");
                canonical(value, hasher);
                hasher.update(b",");
            }
            hasher.update(b"}");
        }
        Value::Array(values) => {
            hasher.update(b"[");
            for value in values {
                canonical(value, hasher);
                hasher.update(b",");
            }
            hasher.update(b"]");
        }
        scalar => hasher.update(scalar.to_string().as_bytes()),
    }
}

#[derive(Deserialize)]
struct Record<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    payload: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct Meta<'a> {
    #[serde(borrow)]
    id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    forked_from_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    parent_thread_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    thread_source: Option<Cow<'a, str>>,
    #[serde(borrow)]
    source: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct Event<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Cow<'a, str>>,
    #[serde(borrow)]
    thread_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    turn_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    item: Option<Item<'a>>,
}

#[derive(Deserialize)]
struct Item<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    role: Option<Cow<'a, str>>,
    #[serde(borrow)]
    content: Option<Vec<Block<'a>>>,
    #[serde(borrow)]
    call_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    internal_chat_message_metadata_passthrough: Option<Passthrough<'a>>,
}

#[derive(Deserialize)]
struct Passthrough<'a> {
    #[serde(borrow)]
    turn_id: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
struct Block<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(borrow)]
    text: Option<Cow<'a, str>>,
}

/// The newline-joined text of the blocks of the given types, if any is visible.
fn blocks_text(blocks: &[Block<'_>], kinds: &[&str]) -> Option<String> {
    let text = blocks
        .iter()
        .filter(|block| block.ty.as_deref().is_some_and(|ty| kinds.contains(&ty)))
        .filter_map(|block| block.text.as_deref())
        .collect::<Vec<_>>()
        .join("\n");
    (!text.trim().is_empty()).then_some(text)
}

#[derive(PartialEq)]
enum Kind {
    User,
    Assistant,
    /// An `agent_message` item: a message between agents.
    Handoff,
    Tool,
}

/// One message or call a rollout holds.
struct Unit {
    ordinal: usize,
    kind: Kind,
    text: Option<String>,
    call: Option<String>,
    turn: Option<String>,
    /// Kept only once the rollout proved it replays its parent.
    digest: Option<[u8; 32]>,
}

/// Everything one pass over a rollout keeps.
#[derive(Default)]
struct Pass {
    session: String,
    subagent: bool,
    guardian: bool,
    /// The sessions whose history the rollout replays before its own.
    replays: Vec<String>,
    /// The ordinal of the rollout's own first turn: a `task_started` whose UUIDv7 turn id is
    /// newer than the rollout's.
    own_turn: Option<usize>,
    typed: Vec<String>,
    desktop: Vec<(String, String, String)>,
    units: Vec<Unit>,
}

fn read_rollout(path: &str, ancestry: &Ancestry<'_>) -> std::io::Result<Reading> {
    let file = Path::new(path);
    let mut pass = Pass {
        session: file_id(file),
        ..Pass::default()
    };
    each_line(file, |ordinal, line| {
        pass.take(ordinal, line);
        true
    })?;
    let replayed = (!pass.replays.is_empty())
        .then(|| ancestry.records_of(&pass.replays))
        .flatten();
    Ok(pass.expected(file, replayed.as_ref()))
}

impl Pass {
    fn take(&mut self, ordinal: usize, line: &[u8]) {
        match top_type(line) {
            Some(b"session_meta" | b"event_msg" | b"response_item") | None => {}
            Some(_) => return,
        }
        let Ok(record) = serde_json::from_slice::<Record<'_>>(line) else {
            return;
        };
        let Some(payload) = record.payload else {
            return;
        };
        match record.ty.as_deref() {
            Some("session_meta") => self.meta(ordinal, payload),
            Some("event_msg") if wanted_event(line) => self.event(ordinal, payload),
            Some("response_item") => self.item(ordinal, payload),
            _ => {}
        }
    }

    fn meta(&mut self, ordinal: usize, payload: &RawValue) {
        let Ok(meta) = serde_json::from_str::<Meta<'_>>(payload.get()) else {
            return;
        };
        let id = meta.id.as_deref().filter(|id| !id.is_empty());
        if ordinal == 0 {
            if let Some(id) = id {
                self.session = id.to_owned();
            }
            self.subagent = meta.thread_source.as_deref() == Some("subagent");
            self.guardian = meta
                .source
                .and_then(|source| serde_json::from_str::<Value>(source.get()).ok())
                .is_some_and(|source| source["subagent"]["other"] == "guardian");
            if let Some(fork) = meta.forked_from_id.as_deref().filter(|id| !id.is_empty()) {
                self.replays.push(fork.to_owned());
                self.replays
                    .extend(meta.parent_thread_id.map(Cow::into_owned));
            }
        } else if let Some(id) = id.filter(|id| *id != self.session) {
            self.replays.push(id.to_owned());
        }
    }

    fn event(&mut self, ordinal: usize, payload: &RawValue) {
        let Ok(event) = serde_json::from_str::<Event<'_>>(payload.get()) else {
            return;
        };
        match event.ty.as_deref() {
            Some("user_message") => {
                let typed = event.message.as_deref().map(str::trim).unwrap_or("");
                if !typed.is_empty() {
                    self.typed.push(typed.to_owned());
                }
            }
            Some("item_completed") => {
                let (Some(thread), Some(turn), Some(item)) =
                    (event.thread_id, event.turn_id, event.item)
                else {
                    return;
                };
                if item.ty.as_deref() == Some("UserMessage") {
                    if let Some(text) = item
                        .content
                        .as_deref()
                        .and_then(|b| blocks_text(b, &["text"]))
                    {
                        self.desktop.push((
                            thread.into_owned(),
                            turn.into_owned(),
                            text.trim().to_owned(),
                        ));
                    }
                }
            }
            Some("task_started")
                if self.own_turn.is_none()
                    && event
                        .turn_id
                        .as_deref()
                        .is_some_and(|turn| v7_after(turn, &self.session)) =>
            {
                self.own_turn = Some(ordinal);
            }
            _ => {}
        }
    }

    fn item(&mut self, ordinal: usize, payload: &RawValue) {
        let Ok(item) = serde_json::from_str::<Item<'_>>(payload.get()) else {
            return;
        };
        let kind = match (item.ty.as_deref(), item.role.as_deref()) {
            (Some("message"), Some("user")) => Kind::User,
            (Some("message"), Some("assistant")) => Kind::Assistant,
            (Some("agent_message"), _) => Kind::Handoff,
            (
                Some("function_call" | "custom_tool_call" | "tool_search_call" | "web_search_call"),
                _,
            ) => Kind::Tool,
            _ => return,
        };
        let kinds: &[&str] = if kind == Kind::Assistant {
            &["output_text", "text"]
        } else {
            &["input_text", "output_text", "text"]
        };
        let call = item
            .call_id
            .as_deref()
            .or(item.id.as_deref())
            .filter(|id| !id.trim().is_empty())
            .map(str::to_owned);
        self.units.push(Unit {
            ordinal,
            text: item.content.as_deref().and_then(|b| blocks_text(b, kinds)),
            call,
            turn: item
                .internal_chat_message_metadata_passthrough
                .and_then(|meta| meta.turn_id.map(Cow::into_owned)),
            digest: (!self.replays.is_empty())
                .then(|| digest(payload))
                .flatten(),
            kind,
        });
    }
}

/// A record's top-level type when its layout shows it before the payload, else `None`.
fn top_type(line: &[u8]) -> Option<&[u8]> {
    let at = memmem::find(line, b"\"type\":\"")?;
    if memmem::find(&line[..at], b"\"payload\"").is_some() {
        return None;
    }
    let rest = &line[at + 8..];
    Some(&rest[..memchr::memchr(b'"', rest)?])
}

/// Whether an `event_msg` can be a submission or a turn's start.
fn wanted_event(line: &[u8]) -> bool {
    [
        &b"\"user_message\""[..],
        b"\"UserMessage\"",
        b"\"task_started\"",
    ]
    .iter()
    .any(|needle| memmem::find(line, needle).is_some())
}

fn v7_after(candidate: &str, earlier: &str) -> bool {
    let v7 = |id: &str| {
        let b = id.as_bytes();
        b.len() == 36
            && b[14] == b'7'
            && b.iter().enumerate().all(|(i, c)| {
                if matches!(i, 8 | 13 | 18 | 23) {
                    *c == b'-'
                } else {
                    c.is_ascii_hexdigit()
                }
            })
    };
    v7(candidate) && v7(earlier) && candidate > earlier
}

/// One rollout's expected rows: its calls' ids, its user and assistant texts in order, and the
/// lines its voice transcripts record.
struct Reading {
    session: String,
    calls: Vec<String>,
    users: Vec<String>,
    agents: Vec<String>,
    spoken: Vec<Spoken>,
}

/// A voice transcript's line: what the user or, with `assistant`, the voice assistant said.
struct Spoken {
    assistant: bool,
    line: String,
}

impl Pass {
    fn expected(&self, file: &Path, replayed: Option<&HashSet<[u8; 32]>>) -> Reading {
        let stem = file_stem(file);
        let mut reading = Reading {
            session: self.session.clone(),
            calls: Vec::new(),
            users: Vec::new(),
            agents: Vec::new(),
            spoken: Vec::new(),
        };
        for unit in &self.units {
            if self.replayed(unit, replayed) {
                continue;
            }
            match unit.kind {
                Kind::Handoff => {}
                Kind::Tool => reading.calls.push(
                    unit.call
                        .clone()
                        .unwrap_or_else(|| format!("codex:{stem}:{}", unit.ordinal)),
                ),
                // A guardian's verdicts are rows of their own; its requests quote the chat
                // it reviews.
                Kind::Assistant if self.guardian => reading
                    .users
                    .extend(unit.text.as_deref().map(|text| text.trim().to_owned())),
                Kind::User if self.guardian => {}
                Kind::Assistant => reading.agents.extend(
                    unit.text
                        .as_deref()
                        .filter(|text| !is_envelope(text))
                        .map(|text| text.trim().to_owned()),
                ),
                Kind::User => {
                    reading.users.extend(self.request(unit));
                    reading.spoken.extend(self.spoken(unit));
                }
            }
        }
        reading.calls.sort_unstable();
        reading.calls.dedup();
        reading
    }

    /// Whether `unit` is the parent history the rollout replays: a copy of a record the
    /// parent's rollouts hold or, with those gone, a record before the rollout's own turn.
    fn replayed(&self, unit: &Unit, records: Option<&HashSet<[u8; 32]>>) -> bool {
        if self.replays.is_empty() {
            return false;
        }
        match records {
            Some(records) => unit.digest.is_some_and(|digest| records.contains(&digest)),
            None => self.own_turn.is_some_and(|own| unit.ordinal < own),
        }
    }

    /// The text a user message must have put in the index: the request a delegation or a
    /// voice session carries, else what the user typed. `None` for text Codex composed.
    fn request(&self, unit: &Unit) -> Option<String> {
        let text = unit.text.as_deref()?;
        let attested = self.attests(text, unit.turn.as_deref());
        if let Some(request) = delegated(text, self.subagent) {
            return (self.subagent || attested).then_some(request);
        }
        if is_wrapper(text) || is_codex_injected(text) {
            return None;
        }
        // A delegated child has no keyboard: with no submission log, its prompts are its own.
        let unlogged_child = self.subagent && self.typed.is_empty() && self.desktop.is_empty();
        (attested || unlogged_child).then(|| text.trim().to_owned())
    }

    /// The lines a voice session's delegation records of what was said, the flush ending the
    /// session included, when its chat's own submission attests the delegation.
    fn spoken(&self, unit: &Unit) -> Vec<Spoken> {
        match unit.text.as_deref() {
            Some(text)
                if text.trim_start().starts_with("<realtime_delegation")
                    && self.attests(text, unit.turn.as_deref()) =>
            {
                transcript(text)
            }
            _ => Vec::new(),
        }
    }

    /// Whether a submission event attests `text`: the user typed it, perhaps with markers
    /// appended; a Desktop submission must also be this rollout's and this turn's.
    fn attests(&self, text: &str, turn: Option<&str>) -> bool {
        let text = text.trim();
        self.typed
            .iter()
            .any(|typed| text.starts_with(typed.as_str()))
            || turn.is_some_and(|turn| {
                self.desktop.iter().any(|(thread, at, typed)| {
                    *thread == self.session
                        && at == turn
                        && !typed.is_empty()
                        && text.starts_with(typed.as_str())
                })
            })
    }
}

fn file_stem(file: &Path) -> &str {
    let name = file.file_name().and_then(|n| n.to_str()).unwrap_or("");
    let name = name.strip_suffix(".zst").unwrap_or(name);
    name.strip_suffix(".jsonl").unwrap_or(name)
}

/// The request a delegation hands an agent: a child's task, or in any chat a voice
/// session's request. The flush ending a voice session hands Codex's own note instead.
fn delegated(text: &str, child: bool) -> Option<String> {
    let text = text.trim_start();
    let voice = text.starts_with("<realtime_delegation");
    if !voice && !(child && text.starts_with("<codex_delegation")) {
        return None;
    }
    let (head, rest) = text.split_once("<input>")?;
    if voice && head.contains("transcript_tail_flush") {
        return None;
    }
    let body = rest.split_once("</input>")?.0.trim();
    (!body.is_empty()).then(|| unescape(body))
}

/// The lines of a voice delegation's `<transcript_delta>`, `user: …` or `assistant: …`; a line
/// naming no speaker continues the one before.
fn transcript(text: &str) -> Vec<Spoken> {
    let Some((_, rest)) = text.split_once("<transcript_delta>") else {
        return Vec::new();
    };
    let body = rest
        .split_once("</transcript_delta>")
        .map_or(rest, |(body, _)| body);
    let mut lines: Vec<Spoken> = Vec::new();
    for line in unescape(body).lines() {
        let (assistant, said) = match (line.strip_prefix("assistant:"), line.strip_prefix("user:"))
        {
            (Some(said), _) => (true, said),
            (None, Some(said)) => (false, said),
            (None, None) => match lines.last_mut() {
                Some(last) => {
                    last.line.push('\n');
                    last.line.push_str(line);
                    continue;
                }
                None => (false, line),
            },
        };
        lines.push(Spoken {
            assistant,
            line: said.to_owned(),
        });
    }
    lines
}

/// Delegation wrappers XML-escape what they carry in older Codex journals.
fn unescape(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// The words of `text`: its runs of letters, digits and apostrophes.
fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !(c.is_alphanumeric() || c == '\'' || c == '\u{2019}'))
        .filter(|word| !word.is_empty())
}

/// Team mode's JSON envelope around a message between agents.
fn is_envelope(text: &str) -> bool {
    let text = text.trim();
    text.starts_with('{')
        && text.contains("\"recipient\"")
        && serde_json::from_str::<Value>(text)
            .is_ok_and(|value| value["author"].is_string() && value["recipient"].is_string())
}

/// The rows the index keeps for one rollout, visible or not.
#[derive(Default)]
struct Rows {
    /// Replies, in turn order.
    agents: Vec<String>,
    others: Vec<String>,
    calls: HashSet<String>,
}

impl Rows {
    fn load(c: &rusqlite::Connection, path: &str) -> Result<Self> {
        let mut rows = Self::default();
        let Some(source) = c
            .prepare_cached("SELECT rowid FROM sources WHERE path=?1")?
            .query_row([path], |r| r.get::<_, i64>(0))
            .optional()?
        else {
            return Ok(rows);
        };
        let mut prose =
            c.prepare_cached("SELECT who,text FROM prose WHERE source=?1 ORDER BY session,turn")?;
        let mut found = prose.query([source])?;
        while let Some(row) = found.next()? {
            let text = agrep_core::store::decode("prose", row.get(1)?)?;
            if row.get_ref(0)?.as_str()? == "agent" {
                rows.agents.push(text);
            } else {
                rows.others.push(text);
            }
        }
        rows.calls = c
            .prepare_cached("SELECT call_id FROM event_rows WHERE source=?1 AND active=1")?
            .query_map([source], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        Ok(rows)
    }
}

impl Reading {
    fn expected(&self) -> usize {
        self.calls.len() + self.users.len() + self.agents.len() + self.spoken.len()
    }

    /// How many user texts, assistant texts and calls the rows lack. A voice transcript's
    /// line is lacking when a word of it is in no row of its speaker's side, wherever the
    /// parser put the line.
    fn lacking(&self, rows: &Rows) -> [usize; 3] {
        let users = self
            .users
            .iter()
            .filter(|text| !rows.others.iter().any(|row| row.contains(text.as_str())))
            .count();
        let calls = self
            .calls
            .iter()
            .filter(|id| !rows.calls.contains(id.as_str()))
            .count();
        let mut unheard = [0, 0];
        if !self.spoken.is_empty() {
            let (by_user, by_assistant) = (heard(&rows.others), heard(&rows.agents));
            for spoken in &self.spoken {
                let heard = if spoken.assistant {
                    &by_assistant
                } else {
                    &by_user
                };
                if words(&spoken.line).any(|word| !heard.contains(word)) {
                    unheard[usize::from(spoken.assistant)] += 1;
                }
            }
        }
        [
            users + unheard[0],
            lacking_replies(&self.agents, &rows.agents) + unheard[1],
            calls,
        ]
    }
}

/// Every word the texts hold.
fn heard(texts: &[String]) -> HashSet<&str> {
    texts.iter().flat_map(|text| words(text)).collect()
}

/// Assistant messages the replies lack. A turn's reply joins its messages in order, so each
/// is sought from where the one before was found. Text past a turn's `REPLY_CAP` is not
/// indexed: past a capped reply, a message counts as held.
fn lacking_replies(messages: &[String], replies: &[String]) -> usize {
    let capped: Vec<bool> = replies
        .iter()
        .map(|reply| reply.ends_with('…') && reply.chars().count() == REPLY_CAP + 1)
        .collect();
    let (mut at, mut offset) = (0, 0);
    let mut lacking = 0;
    for message in messages {
        let found = replies.iter().enumerate().skip(at).find_map(|(i, reply)| {
            let from = if i == at { offset } else { 0 };
            memmem::find(&reply.as_bytes()[from..], message.as_bytes())
                .map(|start| (i, from + start + message.len()))
        });
        if let Some((row, end)) = found {
            (at, offset) = (row, end);
            continue;
        }
        let head: String = message.chars().take(64).collect();
        let past_cap = capped.get(at).copied().unwrap_or(false)
            || replies
                .iter()
                .zip(&capped)
                .any(|(reply, capped)| *capped && reply.contains(head.as_str()));
        if !past_cap && !replies.iter().any(|reply| reply.contains(message.as_str())) {
            lacking += 1;
        }
    }
    lacking
}
