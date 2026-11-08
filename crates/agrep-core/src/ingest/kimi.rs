//! kimi adapter: ~/.kimi/sessions/<md5(workdir)>/<session-uuid>/{context,wire}.jsonl.
//!
//! context.jsonl is the model-facing transcript (kosong Message records, roles user/
//! assistant/tool plus `_`-prefixed internal directives) - canonical for WHO SAID WHAT,
//! but it carries no timestamps. wire.jsonl is the UI event log - every line is
//! `{"timestamp": <epoch secs float>, "message": {"type", "payload"}}` - canonical for
//! WHEN and for tool outcomes (ToolResult.return_value.is_error). Messages come from
//! context, with user-turn timestamps aligned in order against the wire's TurnBegin/
//! SteerInput events (steers land in context as plain user messages since kimi 1.21).
//!
//! The session dir's PARENT is md5(workdir path); ~/.kimi/kimi.json registers the real
//! paths, so hashing each registered path recovers the project. Rotated context_N.jsonl
//! generations (pre-/clear history) are not ingested. Direct subagents/<child>/
//! session dirs are ingested as side sessions linked to the parent UUID dir.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use rayon::prelude::*;
use serde_json::Value;

use crate::ingest::registry::{metadata_is_link, plain_metadata};
use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, project_name,
    summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::intake::{Skip, Tally};
use crate::intake_batch::SourceReadIssue;
use crate::model::{Event, Message, RawMessage};

const REGISTRY_MAX_BYTES: u64 = 8 * 1024 * 1024;

/// Concatenated text parts of a kosong content value (string, or array of typed parts).
/// `think` parts are the model's reasoning, not its reply - excluded.
fn text_of(content: &Value) -> String {
    match content {
        Value::String(s) => s.clone(),
        Value::Array(parts) => {
            let mut out = String::new();
            for p in parts {
                if p.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(t) = p.get("text").and_then(|t| t.as_str()) {
                        if !out.is_empty() {
                            out.push('\n');
                        }
                        out.push_str(t);
                    }
                }
            }
            out
        }
        _ => String::new(),
    }
}

/// Whether `path` is a plain (not symlinked) entry of the wanted kind; a missing one is not.
fn is_plain(path: &Path, wanted: fn(&fs::Metadata) -> bool) -> std::io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(meta) => Ok(wanted(&meta) && !metadata_is_link(&meta)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

/// What the wire log contributes: ordered user-turn timestamps, per-call timestamps,
/// and per-call results (output + is_error, the only honest ok signal kimi records).
#[derive(Default)]
struct Wire {
    turn_ts: Vec<i64>,
    call_ts: HashMap<String, i64>,
    call_result: HashMap<String, (String, usize, usize, bool)>,
}

impl Wire {
    /// The session's wire log when it is a plain file; an unreadable one leaves it unhealthy.
    fn load(path: &Path) -> (Self, bool) {
        match is_plain(path, fs::Metadata::is_file) {
            Ok(true) => Self::parse(path),
            Ok(false) => (Self::default(), true),
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", path, &error);
                (Self::default(), false)
            }
        }
    }

    fn parse(path: &Path) -> (Self, bool) {
        let mut w = Self::default();
        // seen = non-blank lines of wire.jsonl
        let tally = crate::intake::tally();
        let data = match crate::ingest::read_lossy(path) {
            Ok(d) => d,
            Err(e) => {
                crate::ingest::warn_source_skip("kimi", path, &e);
                tally.seen();
                tally.error(&format!("cannot read: {e}"));
                return (w, false);
            }
        };
        for line in data.lines() {
            if line.is_empty() {
                continue;
            }
            tally.seen();
            match serde_json::from_str(line) {
                Ok(v) => w.record(&v, &tally),
                Err(e) => tally.error(&format!("{e}: {}", crate::intake::clip(line, 80))),
            }
        }
        (w, true)
    }

    fn record(&mut self, v: &Value, tally: &Tally) {
        let ts = crate::ingest::parse_timestamp::epoch_secs_f64(
            v.get("timestamp").and_then(|t| t.as_f64()).unwrap_or(0.0),
        );
        let Some(msg) = v.get("message") else {
            tally.skip(Skip::Meta);
            return;
        };
        let ty = msg.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let payload = msg.get("payload").unwrap_or(&Value::Null);
        match ty {
            "TurnBegin" | "SteerInput" => {
                self.turn_ts.push(ts);
                tally.skip(Skip::Meta);
            }
            "ToolCall" => {
                if let Some(id) = payload.get("id").and_then(|i| i.as_str()) {
                    self.call_ts.insert(id.to_string(), ts);
                }
                tally.agent_row();
            }
            "ToolResult" => {
                self.record_result(payload);
                tally.agent_row();
            }
            _ => tally.skip(Skip::NonMessage),
        }
    }

    fn record_result(&mut self, payload: &Value) {
        let id = payload.get("tool_call_id").and_then(|i| i.as_str());
        let (Some(id), Some(rv)) = (id, payload.get("return_value")) else {
            return;
        };
        let (out, output_chars, output_bytes) = rv
            .get("output")
            .map(|o| match o {
                Value::String(s) => cap_event_output(s),
                other => cap_event_output(&text_of(other)),
            })
            .unwrap_or_default();
        let is_err = rv
            .get("is_error")
            .and_then(|e| e.as_bool())
            .unwrap_or(false);
        self.call_result
            .insert(id.to_string(), (out, output_chars, output_bytes, is_err));
    }
}

/// One session directory to parse: its project and, for a subagent's, the parent session.
struct SessionDir {
    dir: PathBuf,
    project: String,
    parent: String,
}

fn parse_session(dir: &Path, project: &str, parent: &str) -> (Vec<Message>, Vec<Event>, bool) {
    let session = dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let context = dir.join("context.jsonl");
    match is_plain(&context, fs::Metadata::is_file) {
        Ok(true) => {}
        Ok(false) => return (Vec::new(), Vec::new(), true),
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &context, &error);
            return (Vec::new(), Vec::new(), false);
        }
    }
    // seen = non-blank lines of context.jsonl
    let tally = crate::intake::tally();
    let data = match crate::ingest::read_lossy(&context) {
        Ok(d) => d,
        Err(e) => {
            crate::ingest::warn_source_skip("kimi", &context, &e);
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };
    let (wire, healthy) = Wire::load(&dir.join("wire.jsonl"));

    // Parse fully first: user timestamps align against the TAIL of the wire turn
    // stream — /clear rotates context.jsonl but wire.jsonl keeps the whole session,
    // so head alignment would stamp post-clear turns with pre-clear times.
    let records = context_records(&data, &tally);
    let n_users = records
        .iter()
        .filter(|(_, v)| v.get("role").and_then(|r| r.as_str()) == Some("user"))
        .count();
    let mut parse = ContextParse {
        session: &session,
        project,
        parent,
        wire: &wire,
        tally: &tally,
        ts_offset: wire.turn_ts.len().saturating_sub(n_users),
        out: Vec::new(),
        events: Vec::new(),
        pending: HashMap::new(),
        turn: 0,
        n_user: 0,
        last_ts: 0,
        worked: false,
    };
    for (record_ordinal, v) in &records {
        parse.record(*record_ordinal, v);
    }
    (
        parse.out.into_iter().map(RawMessage::freeze).collect(),
        parse.events,
        healthy,
    )
}

/// The parsed lines of context.jsonl with their line ordinals; unparsable lines are errors.
fn context_records(data: &str, tally: &Tally) -> Vec<(usize, Value)> {
    let mut records: Vec<(usize, Value)> = Vec::new();
    for (record_ordinal, line) in data.lines().enumerate() {
        if line.is_empty() {
            continue;
        }
        tally.seen();
        match serde_json::from_str(line) {
            Ok(v) => records.push((record_ordinal, v)),
            Err(e) => tally.error(&format!("{e}: {}", crate::intake::clip(line, 80))),
        }
    }
    records
}

/// One session's context records read in order, with the wire supplying times and results.
struct ContextParse<'a> {
    session: &'a str,
    project: &'a str,
    parent: &'a str,
    wire: &'a Wire,
    tally: &'a Tally,
    /// Wire turns before the first user record still in context (see `parse_session`).
    ts_offset: usize,
    out: Vec<RawMessage>,
    events: Vec<Event>,
    /// call_id -> `events` index, so a tool-role context line can backfill a result the wire missed.
    pending: HashMap<String, usize>,
    turn: u32,
    n_user: usize,
    last_ts: i64,
    /// An assistant record's content precedes its tool_calls: the calls join it to the next block.
    worked: bool,
}

impl ContextParse<'_> {
    fn record(&mut self, record_ordinal: usize, v: &Value) {
        let role = v.get("role").and_then(|r| r.as_str()).unwrap_or("");
        match role {
            "user" => self.user(v),
            "assistant" => {
                self.assistant(v, record_ordinal);
                self.tally.agent_row();
            }
            "tool" => {
                self.tool(v);
                self.tally.agent_row();
            }
            role if role.starts_with('_') => self.tally.skip(Skip::Meta),
            "" => self.tally.skip(Skip::NonMessage),
            _ => self.tally.skip(Skip::NonHuman),
        }
    }

    fn user(&mut self, v: &Value) {
        let ts = self
            .wire
            .turn_ts
            .get(self.ts_offset + self.n_user)
            .copied()
            .unwrap_or(self.last_ts);
        self.n_user += 1;
        self.last_ts = ts;
        let text = v.get("content").map(text_of).unwrap_or_default();
        if text.trim().is_empty() {
            self.tally.skip(Skip::EmptyText);
            return;
        }
        if is_wrapper(&text) {
            self.tally.skip(Skip::Wrapper);
            return;
        }
        self.tally.row();
        self.worked = false;
        self.out.push(RawMessage {
            agent: "kimi",
            project: self.project.to_string(),
            session: self.session.to_string(),
            ts,
            turn: self.turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: !self.parent.is_empty(),
            parent: self.parent.to_string(),
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }

    fn assistant(&mut self, v: &Value, record_ordinal: usize) {
        if let Some(content) = v.get("content") {
            let txt = text_of(content);
            if !txt.trim().is_empty() {
                if let Some(last) = self.out.last_mut() {
                    crate::ingest::append_reply(last, &txt);
                    crate::ingest::ReplyOrder::joined_known(&mut last.reply_shape, self.worked);
                    self.worked = false;
                }
            }
        }
        if let Some(calls) = v.get("tool_calls").and_then(|c| c.as_array()) {
            for (call_ordinal, c) in calls.iter().enumerate() {
                self.tool_call(c, record_ordinal, call_ordinal);
            }
        }
    }

    fn tool_call(&mut self, c: &Value, record_ordinal: usize, call_ordinal: usize) {
        let f = c.get("function").unwrap_or(&Value::Null);
        let name = f
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("?")
            .to_string();
        self.worked |= !crate::ingest::is_bookkeeping_tool(&name);
        let native_call_id = c
            .get("id")
            .and_then(|i| i.as_str())
            .filter(|id| !id.trim().is_empty());
        let session = self.session;
        let call_id = native_call_id
            .map(str::to_string)
            .unwrap_or_else(|| format!("kimi:{session}:{record_ordinal}:{call_ordinal}"));
        let ((input, input_chars), state) = call_input(&name, f);
        let (output, output_chars, output_bytes, ok) = match self.wire.call_result.get(&call_id) {
            Some((out, chars, bytes, is_err)) => (out.clone(), *chars, *bytes, Some(!is_err)),
            None => (String::new(), 0, 0, None),
        };
        let ts = self
            .wire
            .call_ts
            .get(&call_id)
            .copied()
            .unwrap_or(self.last_ts);
        if native_call_id.is_some() && ok.is_none() {
            self.pending.insert(call_id.clone(), self.events.len());
        }
        self.tally.event();
        self.events.push(Event {
            turn: None,
            agent: "kimi",
            session: session.to_string(),
            ts,
            kind: "tool",
            name,
            input,
            output,
            input_chars,
            output_chars,
            output_bytes,
            ok,
            call_id,
            child_session: String::new(),
            meta: String::new(),
            state,
        });
    }

    /// The wire recorded no result: take output from the tool message; a leading
    /// <system>ERROR: tag flags failure.
    fn tool(&mut self, v: &Value) {
        let id = v.get("tool_call_id").and_then(|i| i.as_str()).unwrap_or("");
        if let Some(&i) = self.pending.get(id) {
            let txt = v.get("content").map(text_of).unwrap_or_default();
            let ev = &mut self.events[i];
            (ev.output, ev.output_chars, ev.output_bytes) = cap_event_output(&txt);
            ev.ok = Some(!txt.trim_start().starts_with("<system>ERROR"));
            self.pending.remove(id);
        }
    }
}

/// A call's input summary and task state; `arguments` is a JSON-encoded string, kept capped
/// as text when it does not parse.
fn call_input(name: &str, function: &Value) -> ((String, usize), String) {
    let mut state = String::new();
    let input = function
        .get("arguments")
        .and_then(|a| a.as_str())
        .map(|args| {
            serde_json::from_str::<Value>(args)
                .map(|v| {
                    state = crate::ingest::task_state(name, Some(&v));
                    summarize_tool_input_with_chars(&v)
                })
                .unwrap_or_else(|_| cap_str_with_chars(args, EVENT_CAP))
        })
        .unwrap_or_default();
    (input, state)
}

/// Map each `sessions/<md5>/` dir to its real workdir via the kimi.json registry.
fn workdir_registry(root: &Path) -> (HashMap<String, String>, bool) {
    let mut map = HashMap::new();
    let path = root.join("kimi.json");
    match is_plain(&path, fs::Metadata::is_file) {
        Ok(true) => {}
        Ok(false) => return (map, true),
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &path, &error);
            return (map, false);
        }
    }
    let bytes = match crate::ingest::registry::read_bounded_regular_file(&path, REGISTRY_MAX_BYTES)
    {
        Ok(Some(bytes)) => bytes,
        Ok(None) => return (map, true),
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &path, &error);
            return (map, false);
        }
    };
    let data = match String::from_utf8(bytes) {
        Ok(data) => data,
        Err(error) => {
            let error = std::io::Error::new(std::io::ErrorKind::InvalidData, error);
            crate::ingest::warn_source_skip("kimi", &path, &error);
            return (map, false);
        }
    };
    let Ok(v) = serde_json::from_str::<Value>(&data) else {
        return (map, true);
    };
    if let Some(dirs) = v.get("work_dirs").and_then(|d| d.as_array()) {
        for wd in dirs {
            if let Some(p) = wd.get("path").and_then(|p| p.as_str()) {
                map.insert(format!("{:x}", md5::compute(p)), p.to_string());
            }
        }
    }
    (map, true)
}

type Collected = (Vec<Message>, Vec<Event>, bool, Vec<SourceReadIssue>);

/// Walk every `sessions/<md5(workdir)>/<uuid>/` and collect kimi messages + tool events.
fn collect_from_root(root: &Path) -> Collected {
    let rd = match read_sessions(root) {
        Ok(rd) => rd,
        Err(healthy) => return (Vec::new(), Vec::new(), healthy, Vec::new()),
    };
    let sessions = root.join("sessions");
    let (registry, healthy) = workdir_registry(root);
    let mut walk = Walk {
        registry: &registry,
        work: Vec::new(),
        healthy,
    };
    for entry in rd {
        walk.workdir(entry, &sessions);
    }
    parse_all(&walk.work, walk.healthy)
}

/// The `sessions/` listing, or whether the store is healthy without one.
fn read_sessions(root: &Path) -> Result<fs::ReadDir, bool> {
    for dir in [root.to_path_buf(), root.join("sessions")] {
        match is_plain(&dir, fs::Metadata::is_dir) {
            Ok(true) => {}
            Ok(false) => return Err(true),
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", &dir, &error);
                return Err(false);
            }
        }
    }
    let sessions = root.join("sessions");
    match fs::read_dir(&sessions) {
        Ok(d) => Ok(d),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Err(true),
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &sessions, &error);
            Err(false)
        }
    }
}

/// The session directories found under `sessions/`, each workdir's project via the registry,
/// else "kimi" for unregistered dirs; unreadable entries leave the walk unhealthy.
struct Walk<'a> {
    registry: &'a HashMap<String, String>,
    work: Vec<SessionDir>,
    healthy: bool,
}

impl Walk<'_> {
    /// One `sessions/<md5(workdir)>/` entry: every session directory in it.
    fn workdir(&mut self, entry: std::io::Result<fs::DirEntry>, sessions: &Path) {
        let Some(entry) = self.entry(entry, sessions) else {
            return;
        };
        let dir = entry.path();
        if !self.is_plain_dir(&dir) {
            return;
        }
        let key = entry.file_name().to_string_lossy().to_string();
        let project = self
            .registry
            .get(&key)
            .map(|p| project_name(p))
            .unwrap_or_else(|| "kimi".to_string());
        match fs::read_dir(&dir) {
            Ok(sess) => {
                for s in sess {
                    self.session(s, &dir, &project);
                }
            }
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", &dir, &error);
                self.healthy = false;
            }
        }
    }

    /// One session directory, then its direct subagents' as side sessions.
    fn session(&mut self, s: std::io::Result<fs::DirEntry>, workdir: &Path, project: &str) {
        let Some(s) = self.entry(s, workdir) else {
            return;
        };
        let sd = s.path();
        if !self.is_plain_dir(&sd) {
            return;
        }
        let parent = s.file_name().to_string_lossy().to_string();
        self.work.push(SessionDir {
            dir: sd.clone(),
            project: project.to_string(),
            parent: String::new(),
        });
        self.subagents(&sd.join("subagents"), project, &parent);
    }

    fn subagents(&mut self, subagents: &Path, project: &str, parent: &str) {
        match is_plain(subagents, fs::Metadata::is_dir) {
            Ok(true) => {}
            Ok(false) => return,
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", subagents, &error);
                self.healthy = false;
                return;
            }
        }
        let children = match fs::read_dir(subagents) {
            Ok(children) => children,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", subagents, &error);
                self.healthy = false;
                return;
            }
        };
        for child in children {
            let Some(child) = self.entry(child, subagents) else {
                continue;
            };
            let child_dir = child.path();
            if self.is_plain_dir(&child_dir) {
                self.work.push(SessionDir {
                    dir: child_dir,
                    project: project.to_string(),
                    parent: parent.to_string(),
                });
            }
        }
    }

    /// A listing's entry; a failed one is warned against the listed directory.
    fn entry(
        &mut self,
        entry: std::io::Result<fs::DirEntry>,
        listed: &Path,
    ) -> Option<fs::DirEntry> {
        match entry {
            Ok(entry) => Some(entry),
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", listed, &error);
                self.healthy = false;
                None
            }
        }
    }

    /// Whether `path` is a plain directory; one that cannot be read is warned.
    fn is_plain_dir(&mut self, path: &Path) -> bool {
        match plain_metadata(path) {
            Ok(Some(meta)) => meta.is_dir(),
            Ok(None) => false,
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", path, &error);
                self.healthy = false;
                false
            }
        }
    }
}

/// Parse every session directory in parallel, isolating panics, and merge in walk order.
fn parse_all(work: &[SessionDir], mut healthy: bool) -> Collected {
    let parsed: Vec<_> = work
        .par_iter()
        .map(
            |SessionDir {
                 dir: d,
                 project,
                 parent,
             }| {
                crate::ingest::parse_guard::isolate("kimi", d, || parse_session(d, project, parent))
                    .map_err(|panic| {
                        SourceReadIssue::new("kimi", d, "source-read-failed", panic.reason)
                    })
            },
        )
        .collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    let mut issues = Vec::new();
    for session in parsed {
        match session {
            Ok((m, e, true)) => {
                msgs.extend(m);
                evts.extend(e);
            }
            Ok((_, _, false)) => healthy = false,
            Err(issue) => {
                healthy = false;
                issues.push(issue);
            }
        }
    }
    (msgs, evts, healthy, issues)
}

pub fn collect() -> Collected {
    collect_from_root(&crate::ingest::home().join(".kimi"))
}

/// Registry entry (see ingest::registry). Full-parse: ignores the cache.
pub struct Kimi;
impl crate::ingest::registry::Adapter for Kimi {
    fn name(&self) -> &'static str {
        "kimi"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Always
    }
    fn collect(&self, _cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        let (messages, events, _, _) = collect();
        (messages, events)
    }
    fn collect_checked(
        &self,
        _cache: &mut crate::intake_batch::IntakeBatch,
    ) -> (
        Vec<Message>,
        Vec<Event>,
        crate::intake_batch::ReadOutcome,
        Vec<SourceReadIssue>,
    ) {
        let (messages, events, healthy, issues) = collect();
        let outcome = if healthy {
            crate::intake_batch::ReadOutcome::Complete
        } else {
            crate::intake_batch::ReadOutcome::Skipped
        };
        (messages, events, outcome, issues)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".kimi").join("sessions")]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        path.extension().and_then(|x| x.to_str()) == Some("jsonl")
    }
    fn freshness_roots(&self) -> Vec<std::path::PathBuf> {
        let root = crate::ingest::home().join(".kimi");
        vec![root.join("sessions"), root.join("kimi.json")]
    }
    fn freshness_content(&self, path: &std::path::Path) -> bool {
        path.extension().and_then(|x| x.to_str()) == Some("jsonl")
            || path.file_name().and_then(|x| x.to_str()) == Some("kimi.json")
    }
    fn runtime_issue_root(&self) -> std::path::PathBuf {
        crate::ingest::home().join(".kimi")
    }
}
