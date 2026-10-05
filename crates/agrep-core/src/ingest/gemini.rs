//! Gemini CLI adapter: ~/.gemini/tmp/<projectHash>/chats/session-*.jsonl (and legacy .json)
//!
//! Shapes follow google-gemini/gemini-cli packages/core/src/services/chatRecordingService.ts
//! (fb972b2). Since #23749 a session is JSONL: a metadata line `{ sessionId, projectHash,
//! startTime, ... }`, then whole message records - re-recording a message (tool calls, tokens)
//! appends it again under the same `id`; the last write wins at its first position - plus
//! `$set` (metadata; a `messages` array there replaces the history), `$rewindTo` (drop that
//! message and every later one; an unknown id drops them all) and `$patch` (content and
//! tool-result updates, `removeIds`, `orderIds`). Resuming a legacy session copies `X.json`
//! into `X.jsonl` beside it and leaves the `.json` behind, so a `.json` with a `.jsonl`
//! sibling is superseded and not a source. The legacy store is one JSON object per session:
//!   { sessionId, projectHash, startTime, lastUpdated, messages: [ ... ] }
//! each message is `{ id, timestamp (RFC3339), type, content, displayContent?, ... }` and
//! `content` is a string or, since early 2026, a Part[] such as `[{"text": ...}]`:
//!   type=="user"   -> the human's prompt; `displayContent` holds what was typed when the sent
//!                     request differs (expanded @file context). Tool results are recorded as
//!                     user turns of functionResponse parts and the environment preamble as a
//!                     `<session_context>` user turn - neither is the human
//!   type=="gemini" -> the model's turn: `content` prose reply (thought parts excluded),
//!                     `model`, `toolCalls[]`, plus `thoughts` (reasoning, excluded) and `tokens`
//!   type=="info"   -> CLI system notices (auth flow, etc.) - not the user, skipped
//! a toolCall is `{ id, name, args, result: [{functionResponse:{response:{output}}}],
//! status }`. projectHash is a SHA256 of the project dir with no reverse map in the store,
//! so project attribution falls back to "gemini" (see collect).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::ingest::{cap_event_output, is_wrapper, summarize_tool_input_with_chars};
use crate::ingest_cache::ReadOutcome;
use crate::intake::{Skip, Tally};
use crate::model::{Event, Message};

/// `result[*].functionResponse.response.output` of one result part.
fn function_output(part: &Value) -> Option<&str> {
    part.get("functionResponse")?
        .get("response")?
        .get("output")?
        .as_str()
}

/// A toolCall's captured output + outcome: `result[*].functionResponse.response.output`,
/// with `status` giving the ok signal when the store recorded one. `result` is a
/// PartListUnion, so a bare string or a single part is accepted too.
fn tool_result(tc: &Value) -> (String, usize, usize, Option<bool>) {
    let outputs: Vec<&str> = match tc.get("result") {
        Some(Value::String(text)) => vec![text.as_str()],
        Some(Value::Array(parts)) => parts.iter().filter_map(function_output).collect(),
        Some(part @ Value::Object(_)) => function_output(part).into_iter().collect(),
        _ => Vec::new(),
    };
    let ok = match tc.get("status").and_then(|s| s.as_str()) {
        Some("success") => Some(true),
        Some("error") | Some("failed") => Some(false),
        _ => None,
    };
    let (output, output_chars, output_bytes) = cap_event_output(&outputs.join("\n"));
    (output, output_chars, output_bytes, ok)
}

/// The text a PartListUnion carries: the string itself, or its text parts joined the way
/// upstream's `partToString` joins them. Thought parts are reasoning, never prose.
fn part_text(value: &Value) -> String {
    fn push_part(out: &mut String, part: &Value) {
        match part {
            Value::String(text) => out.push_str(text),
            Value::Object(fields) => {
                let thought = fields
                    .get("thought")
                    .is_some_and(|flag| !matches!(flag, Value::Null | Value::Bool(false)));
                if let (false, Some(Value::String(text))) = (thought, fields.get("text")) {
                    out.push_str(text);
                }
            }
            _ => {}
        }
    }
    let mut out = String::new();
    match value {
        Value::Array(parts) => parts.iter().for_each(|part| push_part(&mut out, part)),
        part => push_part(&mut out, part),
    }
    out
}

/// A user turn carrying a functionResponse part is a tool result, not something typed.
fn carries_function_response(value: &Value) -> bool {
    match value {
        Value::Array(parts) => parts
            .iter()
            .any(|part| part.get("functionResponse").is_some()),
        part => part.get("functionResponse").is_some(),
    }
}

/// Upstream `isIgnoredUserContent`'s machine-written prefixes: the environment preamble every
/// new session records as its first user turn, and hook-injected context.
fn is_injected_context(text: &str) -> bool {
    let text = text.trim_start();
    text.starts_with("<session_context>") || text.starts_with("<hook_context>")
}

fn file_stem(path: &Path) -> String {
    path.file_stem()
        .map(|stem| stem.to_string_lossy().to_string())
        .unwrap_or_default()
}

/// One session's messages as rows and events. Callers have already counted every message
/// as seen; each one here lands in exactly one row, agent row or named skip.
fn emit_messages<'a>(
    session: &str,
    messages: impl IntoIterator<Item = &'a Value>,
    tally: &Tally,
) -> (Vec<Message>, Vec<Event>) {
    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut turn = 0u32;
    for (message_ordinal, m) in messages.into_iter().enumerate() {
        let ty = m.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let ts = parse_timestamp::rfc3339(m.get("timestamp").and_then(|t| t.as_str()));
        match ty {
            "user" => {
                let content = m.get("content").unwrap_or(&Value::Null);
                if carries_function_response(content) {
                    tally.skip(Skip::NonHuman);
                    continue;
                }
                let typed = m
                    .get("displayContent")
                    .map(part_text)
                    .filter(|text| !text.trim().is_empty());
                let text = typed.unwrap_or_else(|| part_text(content));
                if text.trim().is_empty() {
                    tally.skip(Skip::EmptyText);
                    continue;
                }
                if is_wrapper(&text) || is_injected_context(&text) {
                    tally.skip(Skip::Wrapper);
                    continue;
                }
                tally.row();
                out.push(crate::model::RawMessage {
                    agent: "gemini",
                    project: "gemini".to_string(),
                    session: session.to_string(),
                    ts,
                    turn,
                    text,
                    model: String::new(),
                    reply: String::new(),
                    reply_chars: 0,
                    side: false,
                    parent: String::new(),
                });
                turn += 1;
            }
            "gemini" => {
                // prose reply -> the user turn it answers (thoughts are reasoning, excluded)
                let txt = m.get("content").map(part_text).unwrap_or_default();
                if !txt.trim().is_empty() {
                    if let Some(last) = out.last_mut() {
                        let chars = crate::ingest::append_capped(
                            &mut last.reply,
                            &txt,
                            crate::ingest::REPLY_CAP,
                        );
                        last.reply_chars += chars;
                    }
                }
                if let Some(last) = out.last_mut() {
                    if last.model.is_empty() {
                        if let Some(md) = m.get("model").and_then(|v| v.as_str()) {
                            if !md.is_empty() {
                                last.model = md.to_string();
                            }
                        }
                    }
                }
                if let Some(calls) = m.get("toolCalls").and_then(|c| c.as_array()) {
                    for (call_ordinal, tc) in calls.iter().enumerate() {
                        let name = tc.get("name").and_then(|n| n.as_str()).unwrap_or("?");
                        let (input, input_chars) = tc
                            .get("args")
                            .map(summarize_tool_input_with_chars)
                            .unwrap_or_default();
                        let (output, output_chars, output_bytes, ok) = tool_result(tc);
                        tally.event();
                        events.push(Event {
                            agent: "gemini",
                            session: session.to_string(),
                            ts,
                            kind: "tool",
                            name: name.to_string(),
                            input,
                            output,
                            input_chars,
                            output_chars,
                            output_bytes,
                            ok,
                            call_id: tc
                                .get("id")
                                .and_then(|i| i.as_str())
                                .filter(|id| !id.trim().is_empty())
                                .map(str::to_string)
                                .unwrap_or_else(|| {
                                    format!("gemini:{message_ordinal}:{call_ordinal}")
                                }),
                            child_session: String::new(),
                        });
                    }
                }
                tally.agent_row();
            }
            "info" => tally.skip(Skip::Meta),
            _ => tally.skip(Skip::NonHuman),
        }
    }
    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        events,
    )
}

/// A legacy whole-session document; seen = elements of its messages array.
fn parse_document(root: &Value, path: &Path, tally: &Tally) -> (Vec<Message>, Vec<Event>) {
    let session = root
        .get("sessionId")
        .and_then(|s| s.as_str())
        .map(str::to_string)
        .unwrap_or_else(|| file_stem(path));
    let Some(messages) = root.get("messages").and_then(|m| m.as_array()) else {
        return (Vec::new(), Vec::new());
    };
    tally.seen_n(messages.len() as u64);
    emit_messages(&session, messages, tally)
}

/// JS `typeof value === 'object' && value !== null`, the test upstream applies to `$set`/`$patch`.
fn is_js_object(value: &Value) -> bool {
    matches!(value, Value::Object(_) | Value::Array(_))
}

fn js_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|n| n != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(_) | Value::Object(_) => true,
    }
}

/// Upstream's `createJsonlRecordAccumulator` (full-load mode): messages keyed by id in
/// first-insertion order like its JS Map, record kinds tested in its order. Every non-blank
/// line and every message inside a `messages` array is one seen unit, and each unit leaves
/// through exactly one counter below or survives into the final history.
#[derive(Default)]
struct Fold {
    session_id: Option<Value>,
    project_hash: Option<Value>,
    slots: Vec<Option<(String, Value)>>,
    index: HashMap<String, usize>,
    seen: u64,
    meta: u64,
    non_message: u64,
    replay: u64,
    unreferenced: u64,
    errors: u64,
    first_error: Option<String>,
}

impl Fold {
    fn read(data: &str) -> Self {
        let mut fold = Self::default();
        for line in data.lines() {
            if line.trim().is_empty() {
                continue;
            }
            fold.seen += 1;
            match serde_json::from_str::<Value>(line) {
                Ok(record) => fold.apply(record),
                Err(error) => {
                    fold.errors += 1;
                    if fold.first_error.is_none() {
                        fold.first_error =
                            Some(format!("{error}: {}", crate::intake::clip(line, 80)));
                    }
                }
            }
        }
        fold
    }

    fn apply(&mut self, record: Value) {
        if let Some(target) = record.get("$rewindTo").and_then(Value::as_str) {
            self.meta += 1;
            self.rewind(target);
        } else if let Some(patch) = record.get("$patch").filter(|patch| is_js_object(patch)) {
            self.meta += 1;
            self.patch(patch);
        } else if let (Some(Value::String(id)), None) = (record.get("id"), record.get("$patch")) {
            let id = id.clone();
            self.put(id, record);
        } else if let Some(set) = record.get("$set").filter(|set| is_js_object(set)) {
            self.meta += 1;
            if let Some(Value::Array(messages)) = set.get("messages") {
                self.replay += self.index.len() as u64;
                self.slots.clear();
                self.index.clear();
                self.insert_all(messages);
            }
            self.merge_metadata(set);
        } else if record.get("sessionId").is_some_and(Value::is_string)
            && record.get("projectHash").is_some_and(Value::is_string)
        {
            self.meta += 1;
            self.merge_metadata(&record);
            if let Some(Value::Array(messages)) = record.get("messages") {
                self.insert_all(messages);
            }
        } else {
            self.non_message += 1;
        }
    }

    fn merge_metadata(&mut self, fields: &Value) {
        if let Some(session_id) = fields.get("sessionId") {
            self.session_id = Some(session_id.clone());
        }
        if let Some(project_hash) = fields.get("projectHash") {
            self.project_hash = Some(project_hash.clone());
        }
    }

    fn insert_all(&mut self, messages: &[Value]) {
        for message in messages {
            self.seen += 1;
            match (message.get("id"), message.get("$patch")) {
                (Some(Value::String(id)), None) => self.put(id.clone(), message.clone()),
                _ => self.non_message += 1,
            }
        }
    }

    fn put(&mut self, id: String, message: Value) {
        if let Some(&slot) = self.index.get(&id) {
            if let Some(entry) = self.slots[slot].as_mut() {
                entry.1 = message;
            }
            self.replay += 1;
        } else {
            self.index.insert(id.clone(), self.slots.len());
            self.slots.push(Some((id, message)));
        }
    }

    fn remove(&mut self, id: &str) {
        if let Some(slot) = self.index.remove(id) {
            self.slots[slot] = None;
            self.unreferenced += 1;
        }
    }

    fn rewind(&mut self, id: &str) {
        let from = self.index.get(id).copied().unwrap_or(0);
        for slot in &mut self.slots[from..] {
            if let Some((id, _)) = slot.take() {
                self.index.remove(&id);
                self.unreferenced += 1;
            }
        }
        self.slots.truncate(from);
    }

    /// Listed ids move to the end in list order; unlisted messages keep their order ahead.
    fn reorder(&mut self, order: &[Value]) {
        let mut moved = Vec::new();
        let mut taken = HashSet::new();
        for id in order.iter().filter_map(Value::as_str) {
            if let Some(&slot) = self.index.get(id) {
                if taken.insert(slot) {
                    moved.push(slot);
                }
            }
        }
        let mut slots = std::mem::take(&mut self.slots);
        let kept: Vec<usize> = (0..slots.len())
            .filter(|slot| slots[*slot].is_some() && !taken.contains(slot))
            .collect();
        self.slots = kept
            .into_iter()
            .chain(moved)
            .filter_map(|slot| slots[slot].take())
            .map(Some)
            .collect();
        self.index = self
            .slots
            .iter()
            .enumerate()
            .filter_map(|(slot, entry)| entry.as_ref().map(|(id, _)| (id.clone(), slot)))
            .collect();
    }

    fn patch(&mut self, patch: &Value) {
        if patch.get("id").is_some_and(Value::is_string) {
            self.update(patch);
        }
        if let Some(Value::Array(updates)) = patch.get("updates") {
            for update in updates {
                if update.get("id").is_some_and(Value::is_string) {
                    self.update(update);
                }
            }
        }
        if let Some(Value::Array(ids)) = patch.get("removeIds") {
            for id in ids.iter().filter_map(Value::as_str) {
                self.remove(id);
            }
        }
        if let Some(Value::Array(order)) = patch.get("orderIds") {
            self.reorder(order);
        }
    }

    /// Upstream `applySinglePatch`: replace `content`; set `result` on gemini tool calls by id.
    fn update(&mut self, patch: &Value) {
        let Some(&slot) = patch
            .get("id")
            .and_then(Value::as_str)
            .and_then(|id| self.index.get(id))
        else {
            return;
        };
        let Some((_, Value::Object(message))) = self.slots[slot].as_mut() else {
            return;
        };
        if let Some(content) = patch.get("content") {
            message.insert("content".to_string(), content.clone());
        }
        let Some(Value::Array(calls)) = patch.get("toolCalls") else {
            return;
        };
        if message.get("type").and_then(Value::as_str) != Some("gemini") {
            return;
        }
        let Some(Value::Array(existing)) = message.get_mut("toolCalls") else {
            return;
        };
        for call in calls.iter().filter(|call| call.is_object()) {
            let id = call.get("id");
            let target = existing.iter_mut().find(|tc| tc.get("id") == id);
            if let (Some(result), Some(Value::Object(target))) = (call.get("result"), target) {
                target.insert("result".to_string(), result.clone());
            }
        }
    }

    /// Upstream `finalize` refuses a history without both ids and falls back to legacy JSON.
    fn complete(&self) -> bool {
        self.session().is_some() && self.project_hash.as_ref().is_some_and(js_truthy)
    }

    fn session(&self) -> Option<String> {
        self.session_id
            .as_ref()
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string)
    }

    fn record(&self, tally: &Tally) {
        tally.seen_n(self.seen);
        tally.skip_n(Skip::Meta, self.meta);
        tally.skip_n(Skip::NonMessage, self.non_message);
        tally.skip_n(Skip::Replay, self.replay);
        tally.skip_n(Skip::Unreferenced, self.unreferenced);
        let sample = self.first_error.as_deref().unwrap_or("");
        for _ in 0..self.errors {
            tally.error(sample);
        }
    }

    fn messages(&self) -> impl Iterator<Item = &Value> {
        self.slots.iter().flatten().map(|(_, message)| message)
    }
}

fn parse_jsonl(data: &str, path: &Path, tally: &Tally) -> (Vec<Message>, Vec<Event>) {
    let fold = Fold::read(data);
    if !fold.complete() {
        if let Ok(root) = serde_json::from_str::<Value>(data) {
            if root.get("sessionId").is_some() {
                return parse_document(&root, path, tally);
            }
        }
    }
    fold.record(tally);
    let session = fold.session().unwrap_or_else(|| file_stem(path));
    emit_messages(&session, fold.messages(), tally)
}

fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    parse_with_tally(path, &crate::intake::file("gemini", path))
}

fn parse_with_tally(path: &Path, tally: &Tally) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!(
                "  ! gemini: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            );
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (Vec::new(), Vec::new(), ReadOutcome::Skipped);
        }
    };
    if path.extension().and_then(|x| x.to_str()) == Some("jsonl") {
        let (messages, events) = parse_jsonl(&data, path, tally);
        return (messages, events, ReadOutcome::Complete);
    }
    match serde_json::from_str::<Value>(&data) {
        Ok(root) => {
            let (messages, events) = parse_document(&root, path, tally);
            (messages, events, ReadOutcome::Complete)
        }
        Err(e) => {
            // whole-file failure: the file itself is the one seen-and-errored record
            tally.seen();
            tally.error(&format!("{e}: {}", crate::intake::clip(&data, 80)));
            (Vec::new(), Vec::new(), ReadOutcome::Complete)
        }
    }
}

/// A main-session file name. Subagent chats nest as `chats/<parent>/<id>.jsonl` and activity
/// logs live at `logs/session-<id>.jsonl`; neither is a top-level conversation here.
fn is_session_name(name: &str) -> bool {
    name.starts_with("session-") && (name.ends_with(".json") || name.ends_with(".jsonl"))
}

/// A legacy `X.json` with a plain `X.jsonl` beside it: resuming migrated the whole record
/// into the `.jsonl`, which carries the conversation from then on.
fn superseded(path: &Path) -> bool {
    if path.extension().and_then(|x| x.to_str()) != Some("json") {
        return false;
    }
    let mut sibling = path.as_os_str().to_os_string();
    sibling.push("l");
    fs::symlink_metadata(&sibling).is_ok_and(|meta| meta.is_file() && !metadata_is_link(&meta))
}

/// Walk every `~/.gemini/tmp/<projectHash>/chats/session-*.json(l)` and collect the user's
/// Gemini CLI turns + tool events, returning superseded legacy files separately so the cache
/// forgets them. Project attribution is the constant "gemini": the store keys sessions by an
/// opaque SHA256 projectHash with no path recorded, so unlike the other adapters there is no
/// readable working dir to bucket by (a hash->path map is a follow-up).
fn session_files(root: &Path) -> (Vec<PathBuf>, HashSet<PathBuf>) {
    let mut files: Vec<PathBuf> = Vec::new();
    let mut retired: HashSet<PathBuf> = HashSet::new();
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("gemini", root, "source root is not a plain directory");
            return (files, retired);
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return (files, retired),
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            return (files, retired);
        }
    }
    let dirs = match fs::read_dir(root) {
        Ok(dirs) => dirs,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            return (files, retired);
        }
    };
    for entry in dirs {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", root, &error);
                continue;
            }
        };
        let project = entry.path();
        match plain_entry_metadata(&entry) {
            Ok(Some(meta)) if meta.is_dir() => {}
            Ok(None) => {
                crate::ingest::warn_source_skip(
                    "gemini",
                    &project,
                    "symlink sources are not followed",
                );
                continue;
            }
            Ok(Some(_)) => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", &project, &error);
                continue;
            }
        }
        let chats = project.join("chats");
        match fs::symlink_metadata(&chats) {
            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
            Ok(meta) if metadata_is_link(&meta) => {
                crate::ingest::warn_source_skip(
                    "gemini",
                    &chats,
                    "symlink sources are not followed",
                );
                continue;
            }
            Ok(_) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", &chats, &error);
                continue;
            }
        }
        let rd = match fs::read_dir(&chats) {
            Ok(rd) => rd,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", &chats, &error);
                continue;
            }
        };
        for file in rd {
            let file = match file {
                Ok(file) => file,
                Err(error) => {
                    crate::ingest::warn_source_skip("gemini", &chats, &error);
                    continue;
                }
            };
            let path = file.path();
            match plain_entry_metadata(&file) {
                Ok(Some(meta)) if meta.is_file() => {}
                Ok(None) => {
                    crate::ingest::warn_source_skip(
                        "gemini",
                        &path,
                        "symlink sources are not followed",
                    );
                    continue;
                }
                Ok(Some(_)) => continue,
                Err(error) => {
                    crate::ingest::warn_source_skip("gemini", &path, &error);
                    continue;
                }
            }
            let name = path
                .file_name()
                .map(|name| name.to_string_lossy())
                .unwrap_or_default();
            if !is_session_name(&name) {
                continue;
            }
            if superseded(&path) {
                retired.insert(path);
            } else {
                files.push(path);
            }
        }
    }
    (files, retired)
}

pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".gemini").join("tmp");
    let (files, retired) = session_files(&root);
    let pass = crate::ingest_cache::collect_cached_retiring_for(
        cache, "gemini", &root, &files, &retired, parse_file,
    );
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). File store -> Stat.
pub struct Gemini;
impl crate::ingest::registry::Adapter for Gemini {
    fn name(&self) -> &'static str {
        "gemini"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".gemini").join("tmp")]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        // chats/session-*.json(l) is the conversation; logs/ and tool-outputs/ are telemetry
        path.file_name()
            .and_then(|x| x.to_str())
            .is_some_and(is_session_name)
            && path
                .parent()
                .and_then(Path::file_name)
                .is_some_and(|dir| dir == "chats")
            && !superseded(path)
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_with_tally, session_files, Gemini};
    use crate::ingest::registry::Adapter;
    use crate::ingest_cache::ReadOutcome;
    use crate::intake::{Skip, Tally};
    use std::path::{Path, PathBuf};

    fn temp_root(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "agrep-gemini-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    fn parse(path: &Path) -> (Vec<crate::model::Message>, Vec<crate::model::Event>, Tally) {
        let tally = Tally::default();
        let (messages, events, outcome) = parse_with_tally(path, &tally);
        assert_eq!(outcome, ReadOutcome::Complete);
        (messages, events, tally)
    }

    /// (seen, rows, agent_rows, Σskips over every kind, errors): the audit identity's terms.
    fn identity(tally: &Tally) -> (u64, u64, u64, u64, u64) {
        let (seen, rows, agent_rows, _, errors) = tally.test_record_counts(Skip::Meta);
        let skips = Skip::ALL
            .iter()
            .map(|skip| tally.test_record_counts(*skip).3)
            .sum();
        (seen, rows, agent_rows, skips, errors)
    }

    #[test]
    fn jsonl_folds_last_writes_rewinds_and_patches_like_upstream() {
        let root = temp_root("fold");
        let path = root.join("session-2026-04-20T09-00-6a6a6a6a.jsonl");
        let lines = [
            r#"{"sessionId":"6a6a6a6a-0000-4000-8000-000000000001","projectHash":"h","startTime":"2026-04-20T09:00:00.000Z","lastUpdated":"2026-04-20T09:00:00.000Z","kind":"main"}"#,
            r#"{"id":"env","timestamp":"2026-04-20T09:00:00.500Z","type":"user","content":[{"text":"<session_context>\nThis is the Gemini CLI.\n</session_context>"}]}"#,
            r#"{"id":"u1","timestamp":"2026-04-20T09:00:01.000Z","type":"user","content":[{"text":"port the "},{"text":"env loader"},{"inlineData":{"mimeType":"image/png","data":"AA=="}}]}"#,
            r#"{"$set":{"lastUpdated":"2026-04-20T09:00:01.000Z"}}"#,
            r#"{"id":"g1","timestamp":"2026-04-20T09:00:02.000Z","type":"gemini","content":"","model":"gemini-3-pro-preview","thoughts":[{"subject":"plan","description":"hidden"}]}"#,
            r#"{"id":"g1","timestamp":"2026-04-20T09:00:02.000Z","type":"gemini","content":"","model":"gemini-3-pro-preview","toolCalls":[{"id":"todo-1","name":"write_todos","args":{"todos":[{"description":"Port the yaml loader","status":"pending"}]},"result":null,"status":"success"}]}"#,
            r#"{"id":"r1","timestamp":"2026-04-20T09:00:03.000Z","type":"user","content":[{"functionResponse":{"id":"todo-1","name":"write_todos","response":{"output":"ok"}}}]}"#,
            r#"{"id":"g2","timestamp":"2026-04-20T09:00:04.000Z","type":"gemini","content":"Ported the env loader.","model":"gemini-3-pro-preview"}"#,
            r#"{"id":"u2","timestamp":"2026-04-20T09:01:00.000Z","type":"user","content":[{"text":"undo that please"}]}"#,
            r#"{"id":"g3","timestamp":"2026-04-20T09:01:01.000Z","type":"gemini","content":"Undone.","model":"gemini-3-pro-preview"}"#,
            r#"{"$rewindTo":"u2"}"#,
            r#"{"$patch":{"updates":[{"id":"g1","toolCalls":[{"id":"todo-1","result":[{"functionResponse":{"id":"todo-1","name":"write_todos","response":{"output":"Updated 1 todo"}}}]}]},{"id":"g2","content":[{"text":"weighing it","thought":true},{"text":"Ported the env loader, tests pass."}]}],"removeIds":["r1"]}}"#,
            r#"{"id":"u3","timestamp":"2026-04-20T09:02:00.000Z","type":"user","content":[{"text":"@notes.md summarize"},{"text":"\n--- Content from referenced files ---\nexpanded file body"}],"displayContent":[{"text":"@notes.md summarize"}]}"#,
            r#"{"id":"i1","timestamp":"2026-04-20T09:02:00.500Z","type":"info","content":"Request cancelled."}"#,
            r#"{"$patch":{"orderIds":["u3","i1","env"]}}"#,
            "{\"id\":\"torn\",\"type\":\"user\",\"content\":[{\"te",
        ];
        std::fs::write(&path, lines.join("\n")).unwrap();
        let (messages, events, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);

        let rows: Vec<(&str, &str, u32)> = messages
            .iter()
            .map(|m| (&*m.text, &*m.reply, m.turn))
            .collect();
        assert_eq!(
            rows,
            vec![
                (
                    "port the env loader",
                    "Ported the env loader, tests pass.",
                    0
                ),
                ("@notes.md summarize", "", 1),
            ]
        );
        assert!(messages
            .iter()
            .all(|m| &*m.session == "6a6a6a6a-0000-4000-8000-000000000001"));
        assert_eq!(&*messages[0].model, "gemini-3-pro-preview");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "write_todos");
        assert_eq!(events[0].call_id, "todo-1");
        assert_eq!(events[0].output, "Updated 1 todo");
        assert!(events[0].input.contains("Port the yaml loader"));
        // 16 lines: metadata, $set, $rewindTo, two $patch lines and the info notice are meta;
        // g1's first write is replayed; u2, g3 (rewind) and r1 (removeIds) are unreferenced;
        // the preamble is a wrapper and the torn last line errs.
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!((seen, rows, agent_rows, errors), (16, 2, 2, 1));
        assert_eq!(seen, rows + agent_rows + skips + errors);
        assert_eq!(tally.test_record_counts(Skip::Meta).3, 6);
        assert_eq!(tally.test_record_counts(Skip::Replay).3, 1);
        assert_eq!(tally.test_record_counts(Skip::Unreferenced).3, 3);
        assert_eq!(tally.test_record_counts(Skip::Wrapper).3, 1);
    }

    #[test]
    fn rewind_to_an_unknown_id_and_a_set_checkpoint_replace_the_history() {
        let root = temp_root("checkpoint");
        let path = root.join("session-2026-04-21T09-00-6b6b6b6b.jsonl");
        let lines = [
            r#"{"sessionId":"6b6b6b6b-0000-4000-8000-000000000002","projectHash":"h"}"#,
            r#"{"id":"a","timestamp":"2026-04-21T09:00:01.000Z","type":"user","content":"first draft"}"#,
            r#"{"$rewindTo":"never-recorded"}"#,
            r#"{"id":"b","timestamp":"2026-04-21T09:00:02.000Z","type":"user","content":"second draft"}"#,
            r#"{"$set":{"sessionId":"6b6b6b6b-0000-4000-8000-00000000000b","messages":[{"id":"c","timestamp":"2026-04-21T09:00:03.000Z","type":"user","content":[{"text":"checkpointed prompt"}]},{"no":"id"}]}}"#,
        ];
        std::fs::write(&path, lines.join("\r\n")).unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        let rows: Vec<(&str, &str)> = messages.iter().map(|m| (&*m.session, &*m.text)).collect();
        assert_eq!(
            rows,
            vec![(
                "6b6b6b6b-0000-4000-8000-00000000000b",
                "checkpointed prompt"
            )]
        );
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!((seen, rows, errors), (7, 1, 0));
        assert_eq!(seen, rows + agent_rows + skips + errors);
    }

    #[test]
    fn legacy_documents_read_part_list_prompts_and_skip_tool_results() {
        let root = temp_root("parts");
        let path = root.join("session-2026-02-15T10-00-6c6c6c6c.json");
        std::fs::write(
            &path,
            r#"{"sessionId":"6c6c6c6c-0000-4000-8000-000000000003","projectHash":"h","messages":[
                {"id":"u1","timestamp":"2026-02-15T10:00:01.000Z","type":"user","content":[{"text":"rename the "},{"text":"config flag"}]},
                {"id":"g1","timestamp":"2026-02-15T10:00:02.000Z","type":"gemini","content":[{"text":"Renamed it."}],"model":"gemini-2.5-pro"},
                {"id":"r1","timestamp":"2026-02-15T10:00:03.000Z","type":"user","content":[{"functionResponse":{"name":"shell","response":{"output":"done"}}}]},
                {"id":"u2","timestamp":"2026-02-15T10:00:04.000Z","type":"user","content":[{"inlineData":{"mimeType":"image/png","data":"AA=="}}]}
            ]}"#,
        )
        .unwrap();
        let (messages, _, tally) = parse(&path);
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "rename the config flag");
        assert_eq!(&*messages[0].reply, "Renamed it.");
        let (seen, rows, agent_rows, skips, errors) = identity(&tally);
        assert_eq!((seen, rows, agent_rows, errors), (4, 1, 1, 0));
        assert_eq!(tally.test_record_counts(Skip::NonHuman).3, 1);
        assert_eq!(tally.test_record_counts(Skip::EmptyText).3, 1);
        assert_eq!(seen, rows + agent_rows + skips + errors);
    }

    #[test]
    fn a_jsonl_sibling_supersedes_its_legacy_json() {
        let root = temp_root("supersede");
        let chats = root.join("hash/chats");
        std::fs::create_dir_all(chats.join("parent-session")).unwrap();
        std::fs::create_dir_all(root.join("hash/logs")).unwrap();
        let migrated = chats.join("session-2026-03-10T08-00-6d6d6d6d.json");
        let current = chats.join("session-2026-03-10T08-00-6d6d6d6d.jsonl");
        let legacy = chats.join("session-2026-03-11T08-00-6e6e6e6e.json");
        for path in [&migrated, &current, &legacy] {
            std::fs::write(path, "{}").unwrap();
        }
        std::fs::write(chats.join("parent-session/6f6f6f6f.jsonl"), "{}").unwrap();
        let log = root.join("hash/logs/session-6d6d6d6d.jsonl");
        std::fs::write(&log, "{}").unwrap();

        let (mut files, retired) = session_files(&root);
        files.sort();
        assert_eq!(files, vec![current.clone(), legacy.clone()]);
        assert_eq!(
            retired.into_iter().collect::<Vec<_>>(),
            vec![migrated.clone()]
        );
        assert!(Gemini.store_content(&current));
        assert!(Gemini.store_content(&legacy));
        assert!(!Gemini.store_content(&migrated));
        assert!(!Gemini.store_content(&log));

        std::fs::remove_file(&current).unwrap();
        assert!(Gemini.store_content(&migrated));
        let (mut files, retired) = session_files(&root);
        files.sort();
        assert_eq!(files, vec![migrated, legacy]);
        assert!(retired.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[cfg(unix)]
    #[test]
    fn discovery_rejects_symlinked_projects_chats_and_sessions() {
        use std::os::unix::fs::symlink;
        let root = temp_root("links");
        let outside = root.with_extension("outside");
        std::fs::create_dir_all(root.join("safe/chats")).unwrap();
        std::fs::create_dir_all(outside.join("chats")).unwrap();
        std::fs::write(root.join("safe/chats/session-safe.json"), "{}").unwrap();
        let outside_file = outside.join("chats/session-outside.json");
        std::fs::write(&outside_file, "{}").unwrap();
        symlink(&outside, root.join("linked-project")).unwrap();
        std::fs::create_dir_all(root.join("linked-chats-project")).unwrap();
        symlink(
            outside.join("chats"),
            root.join("linked-chats-project/chats"),
        )
        .unwrap();
        symlink(&outside_file, root.join("safe/chats/session-linked.json")).unwrap();
        // a symlinked .jsonl must not retire the real .json it would shadow
        symlink(&outside_file, root.join("safe/chats/session-safe.jsonl")).unwrap();

        let (files, retired) = session_files(&root);
        assert_eq!(files, vec![root.join("safe/chats/session-safe.json")]);
        assert!(retired.is_empty());
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(outside);
    }
}
