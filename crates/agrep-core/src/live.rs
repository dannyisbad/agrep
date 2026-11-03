//! Direct, bounded live observation. This module never opens the agrep index.
pub mod board;
mod journal;
mod privacy;
mod sqlite;
use crate::{ingest::registry, process};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs,
    io::{BufRead, BufReader, Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::{Arc, Condvar, Mutex},
    thread,
    time::Duration,
};

pub const AGENTS: &[&str] = &["claude", "codex", "opencode", "antigravity", "cursor", "pi"];
pub const EVENT_TYPES: &[&str] = &[
    "user",
    "reply",
    "tool",
    "tool_done",
    "done",
    "queued",
    "subagent_result",
];
pub const WINDOW_MS: i64 = 90_000;
const LINE_CAP: usize = 1024 * 1024;
const JSON_CAP: usize = 4 * 1024 * 1024;

struct BoundedLine {
    body: Vec<u8>,
    bytes: u64,
    complete: bool,
    oversized: bool,
}
fn bounded_line(reader: &mut impl BufRead, discarding: bool) -> std::io::Result<BoundedLine> {
    let mut line = BoundedLine {
        body: Vec::new(),
        bytes: 0,
        complete: false,
        oversized: discarding,
    };
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Ok(line);
        }
        let length = available
            .iter()
            .position(|&b| b == b'\n')
            .map_or(available.len(), |n| n + 1);
        line.complete = available[length - 1] == b'\n';
        line.bytes += length as u64;
        if !line.oversized && line.body.len().saturating_add(length) <= LINE_CAP {
            line.body.extend_from_slice(&available[..length]);
        } else {
            line.oversized = true;
            line.body.clear();
        }
        reader.consume(length);
        if line.complete {
            return Ok(line);
        }
    }
}
pub fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
pub fn normalize_agent(s: &str) -> Result<String> {
    let lower = s.trim().to_ascii_lowercase();
    let name = crate::history::normalize_agent(&lower);
    anyhow::ensure!(AGENTS.contains(&name), "unsupported live agent: {s}");
    Ok(name.into())
}
pub fn normalize_session(s: &str) -> Result<String> {
    crate::history::identity(s)
}
pub fn handle(s: &str) -> String {
    crate::history::target(s)
}
pub(crate) fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
pub(crate) fn cap(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
pub(crate) fn stamp(v: &Value) -> i64 {
    if let Some(n) = v.as_i64() {
        return if n > 0 && n < 100_000_000_000 {
            n * 1000
        } else {
            n
        };
    }
    chrono::DateTime::parse_from_rfc3339(text(v))
        .map(|v| v.timestamp_millis())
        .unwrap_or(0)
}

fn serialize_parent<S: serde::Serializer>(
    s: &str,
    serializer: S,
) -> std::result::Result<S::Ok, S::Error> {
    if s.is_empty() {
        serializer.serialize_none()
    } else {
        serializer.serialize_str(s)
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Session {
    pub agent: String,
    pub session: String,
    pub project: String,
    pub title: String,
    pub last_text: String,
    pub model: String,
    pub last_ts: i64,
    pub state: String,
    pub working: bool,
    pub active: bool,
    #[serde(serialize_with = "serialize_parent")]
    pub parent: String,
    pub sub: bool,
    pub state_ts: i64,
    pub queued: usize,
    pub queued_text: String,
    pub recent: Vec<Value>,
    #[serde(skip)]
    turn_n: usize,
    #[serde(skip)]
    pending: BTreeMap<String, (String, i64)>,
    #[serde(skip)]
    writer: bool,
}
#[derive(Clone, Debug, Serialize)]
pub struct SourceError {
    pub agent: String,
    pub path: String,
    pub error: String,
}
#[derive(Clone, Debug, Serialize)]
pub struct Snapshot {
    pub now: i64,
    pub window_s: i64,
    pub sessions: Vec<Session>,
    pub booting: bool,
    pub last_err: String,
    pub degraded_sources: Vec<SourceError>,
    pub poll_s: f64,
}
impl Default for Snapshot {
    fn default() -> Self {
        Self {
            now: now(),
            window_s: 90,
            sessions: vec![],
            booting: true,
            last_err: String::new(),
            degraded_sources: vec![],
            poll_s: 0.7,
        }
    }
}
impl Snapshot {
    pub fn scoped(&self, agents: &[String], session: Option<&str>) -> Value {
        let mut result = serde_json::to_value(self).expect("snapshot serialization");
        let rows: Vec<_> = self
            .sessions
            .iter()
            .filter(|r| {
                (agents.is_empty() || agents.contains(&r.agent))
                    && session.is_none_or(|s| r.session.contains(s))
            })
            .collect();
        result["sessions"] = json!(rows);
        result["counts"] = json!({"sessions": rows.len(), "working": rows.iter().filter(|r| r.working).count(), "recent_events": rows.iter().map(|r| r.recent.len()).sum::<usize>()});
        if !agents.is_empty() || session.is_some() {
            result.as_object_mut().unwrap().remove("last_err");
            if !agents.is_empty() {
                result["degraded_sources"] = json!(self
                    .degraded_sources
                    .iter()
                    .filter(|e| agents.contains(&e.agent))
                    .collect::<Vec<_>>());
            } else {
                result.as_object_mut().unwrap().remove("degraded_sources");
            }
        }
        result
    }
}
#[derive(Clone, Default)]
pub(crate) struct Source {
    offset: u64,
    skipped_records: u64,
    discarding_line: bool,
    oversized_json: bool,
    identity: String,
    pub meta: journal::Meta,
    pub seen: HashMap<String, String>,
    pub floor: i64,
    pub unfinished: HashSet<String>,
    fingerprint: String,
    metadata: HashMap<String, (String, Value)>,
}
pub struct Watcher {
    sources: HashMap<PathBuf, Source>,
    sessions: BTreeMap<(String, String), Session>,
    errors: BTreeMap<(String, String), SourceError>,
    booting: bool,
    last_process_scan: Option<std::time::Instant>,
}
impl Default for Watcher {
    fn default() -> Self {
        Self::new()
    }
}
impl Watcher {
    pub fn new() -> Self {
        Self {
            sources: HashMap::new(),
            sessions: BTreeMap::new(),
            errors: BTreeMap::new(),
            booting: true,
            last_process_scan: None,
        }
    }
    fn error(&mut self, agent: &str, path: &Path, error: impl std::fmt::Display) {
        let path = path.to_string_lossy().into_owned();
        self.errors.insert(
            (agent.into(), path.clone()),
            SourceError {
                agent: agent.into(),
                path,
                error: error.to_string(),
            },
        );
    }
    fn walk(
        &mut self,
        path: &Path,
        adapter: &dyn registry::Adapter,
        found: &mut Vec<(String, PathBuf)>,
    ) {
        let m = match fs::symlink_metadata(path) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return,
            Err(e) => {
                self.error(adapter.name(), path, e);
                return;
            }
        };
        if registry::metadata_is_link(&m) {
            self.error(
                adapter.name(),
                path,
                "source path is a link or reparse point",
            );
            return;
        }
        if m.is_file() {
            let journal = path
                .extension()
                .is_some_and(|e| e == "jsonl" || e == "json");
            let old = m
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .is_some_and(|t| now() - t.as_millis() as i64 > WINDOW_MS);
            if journal && old && !self.sources.contains_key(path) {
                return;
            }
            if adapter.store_content(path)
                && (path
                    .extension()
                    .is_some_and(|e| e == "jsonl" || e == "db" || e == "vscdb")
                    || (adapter.name() == "antigravity"
                        && path.extension().is_some_and(|e| e == "json")
                        && path.components().any(|c| c.as_os_str() == "messages")))
            {
                found.push((adapter.name().into(), path.into()));
            }
        } else if m.is_dir() {
            match fs::read_dir(path) {
                Ok(entries) => {
                    for entry in entries {
                        match entry {
                            Ok(e) => self.walk(&e.path(), adapter, found),
                            Err(e) => self.error(adapter.name(), path, e),
                        }
                    }
                }
                Err(e) => self.error(adapter.name(), path, e),
            }
        }
    }
    pub fn poll(&mut self, sink: &mut impl FnMut(&Value) -> Result<()>) -> Result<()> {
        // Health is recomputed on every census. Last-good session state survives a failed source.
        self.errors.clear();
        let mut files = vec![];
        for adapter in registry::ADAPTERS
            .iter()
            .filter(|a| AGENTS.contains(&a.name()))
        {
            for root in adapter.store_roots() {
                self.walk(&root, *adapter, &mut files);
            }
        }
        files.sort();
        files.dedup();
        for (agent, path) in files {
            let mut source = self.sources.remove(&path).unwrap_or_default();
            let result = if path.extension().is_some_and(|e| e == "db" || e == "vscdb") {
                let fingerprint = database_fingerprint(&path);
                if fingerprint == source.fingerprint {
                    Ok(())
                } else {
                    sqlite::poll(&agent, &path, &mut source, self, sink)
                        .map(|()| source.fingerprint = fingerprint)
                }
            } else {
                self.journal(&agent, &path, &mut source, sink)
            };
            if source.skipped_records > 0 {
                self.error(&agent, &path, format!("skipped {} oversized live record(s); limits: {LINE_CAP} bytes per line, {JSON_CAP} bytes per JSON file", source.skipped_records));
            }
            self.sources.insert(path.clone(), source);
            if let Err(e) = result {
                if e.downcast_ref::<SinkError>().is_some() {
                    return Err(e);
                }
                self.error(&agent, &path, e);
            }
        }
        if self.sessions.values().any(|s| s.working)
            && self
                .last_process_scan
                .is_none_or(|t| t.elapsed() >= Duration::from_secs(2))
        {
            self.last_process_scan = Some(std::time::Instant::now());
            match process::scan() {
                Ok(processes) => self.decay(Some(&processes), now(), sink)?,
                Err(e) => {
                    self.error("process", Path::new("process census"), e);
                    self.decay(None, now(), sink)?;
                }
            }
        }
        self.sessions
            .retain(|_, s| now() - s.last_ts < 900_000 || s.working);
        self.booting = false;
        Ok(())
    }
    fn decay(
        &mut self,
        processes: Option<&[process::Process]>,
        time: i64,
        sink: &mut impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        let mut ended = vec![];
        for ((agent, id), s) in &mut self.sessions {
            if !s.working {
                continue;
            }
            let matched = processes.is_some_and(|ps| {
                ps.iter()
                    .any(|p| &p.agent == agent && p.session.as_ref() == Some(id))
            });
            if matched {
                s.writer = true;
            } else if s.writer && processes.is_some() {
                ended.push(json!({"type":"done", "agent":agent,"session":id,"ts":s.last_ts + 1,"why":"interrupted"}));
            } else if matches!(s.state.as_str(), "thinking" | "replying")
                && time - s.last_ts > 240_000
            {
                ended.push(json!({"type":"done", "agent":agent,"session":id,"ts":s.last_ts + 1,"why":"stalled"}));
            }
        }
        for e in ended {
            self.emit(e, sink)?;
        }
        Ok(())
    }
    pub(crate) fn emit(
        &mut self,
        mut e: Value,
        sink: &mut impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        let key = (text(&e["agent"]).to_owned(), text(&e["session"]).to_owned());
        if key.1.is_empty() {
            return Ok(());
        }
        let mut s = self.sessions.get(&key).cloned().unwrap_or_else(|| Session {
            agent: key.0.clone(),
            session: key.1.clone(),
            ..Session::default()
        });
        let event_type = text(&e["type"]).to_owned();
        let kind = event_type.as_str();
        let ts = e["ts"].as_i64().filter(|t| *t > 0).unwrap_or_else(now);
        for field in ["project", "title", "model", "parent"] {
            let v = text(&e[field]);
            if v.is_empty() {
                continue;
            }
            match field {
                "project" => s.project = v.into(),
                "title" => s.title = v.into(),
                "model" => s.model = v.into(),
                "parent" => s.parent = v.into(),
                _ => {}
            }
        }
        s.sub |= e["sub_session"].as_bool().unwrap_or(false) || !s.parent.is_empty();
        s.last_ts = s.last_ts.max(ts);
        if kind == "user" {
            s.turn_n += 1;
        }
        if matches!(kind, "user" | "reply") && !text(&e["text"]).trim().is_empty() {
            s.last_text = cap(text(&e["text"]), 1000);
        }
        match kind {
            "tool" => {
                s.pending
                    .insert(text(&e["call_id"]).into(), (text(&e["name"]).into(), ts));
            }
            "tool_done" => {
                if let Some((name, start)) = s.pending.remove(text(&e["call_id"])) {
                    if text(&e["name"]).is_empty() {
                        e["name"] = json!(name)
                    }
                    e["dur"] = json!((ts - start).max(0));
                }
            }
            "user" | "done" => s.pending.clear(),
            _ => {}
        }
        match kind {
            "user" => s.state = "thinking".into(),
            "reply" => s.state = "replying".into(),
            "tool" | "tool_done" => {
                s.state = s
                    .pending
                    .values()
                    .next_back()
                    .map(|(name, _)| {
                        format!(
                            "⚙ {name}{}",
                            if s.pending.len() > 1 {
                                format!(" +{}", s.pending.len() - 1)
                            } else {
                                String::new()
                            }
                        )
                    })
                    .unwrap_or_else(|| "thinking".into())
            }
            "done" => {
                s.state = e["why"].as_str().unwrap_or("done").into();
                s.writer = false;
            }
            "queued" => {
                if text(&e["op"]) == "enqueue" {
                    s.queued += 1;
                    s.queued_text = text(&e["text"]).into()
                } else {
                    s.queued = s.queued.saturating_sub(1);
                    if s.queued == 0 {
                        s.queued_text.clear()
                    }
                }
            }
            _ => {}
        }
        if matches!(kind, "user" | "reply" | "tool" | "tool_done") {
            s.working = true;
            s.state_ts = ts;
        }
        if kind == "done" {
            s.working = false;
            s.state_ts = ts;
        }
        e["tn"] = json!(s.turn_n);
        e["sstate"] = json!(s.state);
        e["sworking"] = json!(s.working);
        e["sts"] = json!(s.state_ts);
        if !s.model.is_empty() {
            e["smodel"] = json!(s.model)
        }
        if kind == "queued" {
            e["squeued"] = json!(s.queued);
            e["squeued_text"] = json!(s.queued_text)
        }
        if s.sub && kind == "user" {
            e["who"] = json!("subagent")
        }
        // A failed consumer does not acknowledge the journal offset or state mutation.
        if !self.booting {
            sink(&e).map_err(|e| anyhow::Error::new(SinkError(e)))?;
        }
        if !matches!(kind, "done" | "queued") {
            s.recent.push(e);
            if s.recent.len() > 200 {
                s.recent.remove(0);
            }
        }
        self.sessions.insert(key, s);
        Ok(())
    }
    fn journal(
        &mut self,
        agent: &str,
        path: &Path,
        source: &mut Source,
        sink: &mut impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        let mut options = fs::OpenOptions::new();
        options.read(true);
        registry::secure_read_options(&mut options);
        let file = options.open(path)?;
        let meta = file.metadata()?;
        anyhow::ensure!(meta.is_file(), "source is not a regular file");
        #[cfg(not(unix))]
        let identity = {
            use std::os::windows::fs::MetadataExt;
            format!("{}", meta.creation_time())
        };
        // Change token includes mtime: identity only describes replacement, not growth.
        #[cfg(unix)]
        let identity = {
            use std::os::unix::fs::MetadataExt;
            format!("{}:{}", meta.dev(), meta.ino())
        };
        let is_new = source.identity.is_empty();
        if is_new
            && now()
                - meta
                    .modified()?
                    .duration_since(std::time::UNIX_EPOCH)?
                    .as_millis() as i64
                > WINDOW_MS
        {
            return Ok(());
        }
        if source.identity != identity || source.offset > meta.len() {
            *source = Source {
                identity,
                ..Source::default()
            };
            source.meta = journal::Meta::from_path(agent, path);
            if path.extension().is_some_and(|e| e == "jsonl") {
                let mut head = BufReader::new(&file).take(meta.len());
                for _ in 0..12 {
                    let line = bounded_line(&mut head, false)?;
                    if line.bytes == 0 {
                        break;
                    }
                    if line.oversized {
                        source.skipped_records += 1;
                    } else if let Ok(row) = serde_json::from_slice(&line.body) {
                        source.meta.header(agent, &row);
                    }
                    if !line.complete {
                        break;
                    }
                }
                if meta.len() > 65_536 {
                    source.offset = meta.len() - 65_536;
                    let mut reader = BufReader::new(&file);
                    reader.seek(SeekFrom::Start(source.offset))?;
                    let mut reader = reader.take(meta.len().saturating_sub(source.offset));
                    let line = bounded_line(&mut reader, true)?;
                    source.offset += line.bytes;
                    source.discarding_line = !line.complete;
                }
            }
        }
        if path.extension().is_some_and(|e| e == "json") {
            if meta.len() > JSON_CAP as u64 {
                if !source.oversized_json {
                    source.skipped_records += 1;
                }
                source.oversized_json = true;
                source.offset = meta.len();
                return Ok(());
            }
            if source.offset == meta.len() {
                return Ok(());
            }
            let mut reader = BufReader::new(&file);
            reader.seek(SeekFrom::Start(0))?;
            let mut bytes = Vec::new();
            reader.take(JSON_CAP as u64 + 1).read_to_end(&mut bytes)?;
            if bytes.len() > JSON_CAP {
                if !source.oversized_json {
                    source.skipped_records += 1;
                }
                source.oversized_json = true;
                source.offset = meta.len();
                return Ok(());
            }
            source.oversized_json = false;
            let row: Value = serde_json::from_slice(&bytes)?;
            let sender = text(&row["sender"]);
            if sender.contains("/task-") {
                self.emit(json!({"agent":agent,"session":source.meta.session,"type":"subagent_result","ts":now(),"name":row["renderDetails"]["messageTitle"].as_str().unwrap_or(sender),"output":cap(text(&row["content"]),400)}),sink)?;
            }
            source.offset = meta.len();
            return Ok(());
        }
        let mut reader = BufReader::new(file);
        reader.seek(SeekFrom::Start(source.offset))?;
        let mut reader = reader.take(meta.len().saturating_sub(source.offset));
        loop {
            let line = bounded_line(&mut reader, source.discarding_line)?;
            if line.bytes == 0 {
                break;
            }
            if line.oversized {
                if !source.discarding_line {
                    source.skipped_records += 1;
                }
                source.discarding_line = !line.complete;
                source.offset += line.bytes;
                if !line.complete {
                    break;
                }
                continue;
            }
            if !line.complete {
                break;
            }
            if !line.body.iter().all(u8::is_ascii_whitespace) {
                let row: Value = serde_json::from_slice(&line.body)
                    .with_context(|| format!("invalid JSON at byte {}", source.offset))?;
                let mut parsed_meta = source.meta.clone();
                let events = journal::parse(agent, &mut parsed_meta, &row);
                for e in events {
                    self.emit(e, sink)?;
                }
                source.meta = parsed_meta;
            }
            source.offset += line.bytes;
        }
        Ok(())
    }
    pub fn snapshot(&self) -> Snapshot {
        let time = now();
        let mut sessions: Vec<_> = self
            .sessions
            .values()
            .cloned()
            .map(|mut s| {
                s.active = time - s.last_ts <= WINDOW_MS;
                s.recent = s
                    .recent
                    .into_iter()
                    .rev()
                    .take(60)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect();
                s
            })
            .collect();
        sessions.sort_by_key(|s| std::cmp::Reverse(s.last_ts));
        Snapshot {
            now: time,
            window_s: 90,
            sessions,
            booting: self.booting,
            last_err: self
                .errors
                .values()
                .next()
                .map(|e| format!("{} source unreadable: {}: {}", e.agent, e.path, e.error))
                .unwrap_or_default(),
            degraded_sources: self.errors.values().cloned().collect(),
            poll_s: 0.7,
        }
    }
}
fn database_fingerprint(path: &Path) -> String {
    [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
    ]
    .iter()
    .map(|p| {
        fs::metadata(p)
            .map(|m| format!("{}:{:?}", m.len(), m.modified().ok()))
            .unwrap_or_default()
    })
    .collect::<Vec<_>>()
    .join(";")
}

#[derive(Debug)]
struct SinkError(anyhow::Error);
impl std::fmt::Display for SinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(f)
    }
}
impl std::error::Error for SinkError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.0.as_ref())
    }
}

/// One foreground-owned watcher shared by every browser request / board frame.
pub struct Service {
    shared: Arc<(Mutex<(u64, Snapshot)>, Condvar)>,
    stop: Arc<std::sync::atomic::AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Default for Service {
    fn default() -> Self {
        Self::new()
    }
}
impl Service {
    pub fn new() -> Self {
        let shared = Arc::new((Mutex::new((0, Snapshot::default())), Condvar::new()));
        let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let out = shared.clone();
        let done = stop.clone();
        let thread = thread::spawn(move || {
            let mut watcher = Watcher::new();
            while !done.load(std::sync::atomic::Ordering::Relaxed) {
                let _ = watcher.poll(&mut |_| Ok(()));
                let (lock, cv) = &*out;
                let mut shared = lock.lock().unwrap();
                shared.0 += 1;
                shared.1 = watcher.snapshot();
                drop(shared);
                cv.notify_all();
                let guard = lock.lock().unwrap();
                let _ = cv.wait_timeout(guard, Duration::from_millis(700)).unwrap();
            }
        });
        Self {
            shared,
            stop,
            thread: Some(thread),
        }
    }
    pub fn snapshot(&self, wait: Duration) -> Snapshot {
        let (lock, cv) = &*self.shared;
        let guard = lock.lock().unwrap();
        let (guard, _) = cv.wait_timeout_while(guard, wait, |s| s.1.booting).unwrap();
        guard.1.clone()
    }
    pub fn snapshot_if_changed(&self, generation: &mut u64, snapshot: &mut Snapshot) -> bool {
        let guard = self.shared.0.lock().unwrap();
        if *generation == guard.0 {
            return false;
        }
        *generation = guard.0;
        *snapshot = guard.1.clone();
        true
    }
}
impl Drop for Service {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        self.shared.1.notify_all();
        // Cancellation never extends a caller's bounded one-shot wait. A detached
        // census finishes its current read, sees stop, and drops all owned state.
        self.thread.take();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn fresh_antigravity_json_is_read_from_start_once() {
        let t = tempfile::tempdir().unwrap();
        let p = t
            .path()
            .join("session/.system_generated/messages/child.json");
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(
            &p,
            json!({"sender":"agent/task-review", "content":"review complete"}).to_string(),
        )
        .unwrap();
        let mut w = Watcher::new();
        w.booting = false;
        let mut source = Source::default();
        let mut events = vec![];
        for _ in 0..2 {
            w.journal("antigravity", &p, &mut source, &mut |e| {
                events.push(e.clone());
                Ok(())
            })
            .unwrap();
        }
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["type"], "subagent_result");
    }
    #[test]
    fn journal_ack_follows_emit_and_partial_lines_wait() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("root.jsonl");
        fs::write(
            &p,
            b"{\"type\":\"user\",\"sessionId\":\"root\",\"message\":{\"content\":\"first\"}}\n",
        )
        .unwrap();
        let mut w = Watcher::new();
        w.booting = false;
        let mut source = Source::default();
        let failed = w.journal("claude", &p, &mut source, &mut |_| {
            anyhow::bail!("consumer failed")
        });
        assert!(failed.is_err());
        assert_eq!(source.offset, 0);
        let mut got = vec![];
        w.journal("claude", &p, &mut source, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 1);
        let offset = source.offset;
        let mut f = fs::OpenOptions::new().append(true).open(&p).unwrap();
        f.write_all(
            b"{\"type\":\"user\",\"sessionId\":\"root\",\"message\":{\"content\":\"next\"}}",
        )
        .unwrap();
        w.journal("claude", &p, &mut source, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(source.offset, offset);
        f.write_all(b"\n").unwrap();
        w.journal("claude", &p, &mut source, &mut |e| {
            got.push(e.clone());
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(got[1]["tn"], 2);
        fs::write(&p, b"bad json\n{\"type\":\"user\"}\n").unwrap();
        assert!(w
            .journal("claude", &p, &mut source, &mut |_| Ok(()))
            .is_err());
        assert_eq!(source.offset, 0);
    }
    #[test]
    fn sticky_working_parallel_tools_and_snapshot_scope() {
        let mut w = Watcher::new();
        w.booting = false;
        for e in [
            json!({"type":"user","text":"work"}),
            json!({"type":"tool","call_id":"a","name":"shell"}),
            json!({"type":"tool","call_id":"b","name":"read"}),
            json!({"type":"tool_done","call_id":"a"}),
        ] {
            let mut e = e;
            e["agent"] = json!("pi");
            e["session"] = json!("root");
            e["ts"] = json!(now());
            w.emit(e, &mut |_| Ok(())).unwrap();
        }
        let s = w.snapshot();
        assert!(s.sessions[0].working);
        assert_eq!(s.sessions[0].state, "⚙ read");
        w.error("claude", Path::new("broken"), "permission denied");
        let filtered = w.snapshot().scoped(&["pi".into()], None);
        assert_eq!(filtered["counts"]["sessions"], 1);
        assert_eq!(filtered["degraded_sources"], json!([]));
        assert!(filtered.get("last_err").is_none());
        w.emit(
            json!({"agent":"pi","session":"root","type":"done","ts":now()}),
            &mut |_| Ok(()),
        )
        .unwrap();
        assert!(!w.snapshot().sessions[0].working);
    }
    #[test]
    fn opaque_handles_and_live_aliases() {
        assert_eq!(normalize_agent("OMP").unwrap(), "pi");
        assert_eq!(normalize_agent("agy").unwrap(), "antigravity");
        assert!(normalize_agent("crush").is_err());
        assert_eq!(normalize_session("@root:3.abcd").unwrap(), "root");
        assert_eq!(normalize_session("@~YS9i:0.abcd").unwrap(), "a/b");
        assert_eq!(handle("a/b"), "@~YS9i");
    }
}

#[cfg(test)]
mod queue_tests {
    use super::*;
    #[test]
    fn queue_and_child_result_do_not_change_busy_state() {
        let mut w = Watcher::new();
        w.booting = false;
        for op in ["enqueue", "enqueue", "dequeue"] {
            w.emit(
                json!({"agent":"claude","session":"root","type":"queued","op":op,"text":"next"}),
                &mut |_| Ok(()),
            )
            .unwrap();
        }
        let snap = w.snapshot();
        assert_eq!(snap.sessions[0].queued, 1);
        assert!(!snap.sessions[0].working);
        assert!(snap.sessions[0].recent.is_empty());
        assert!(serde_json::to_value(&snap).unwrap()["sessions"][0]["parent"].is_null());
        w.emit(json!({"agent":"claude","session":"root","type":"subagent_result","name":"review","output":"looks good"}),&mut |_|Ok(())).unwrap();
        assert_eq!(w.snapshot().sessions[0].recent.len(), 1);
    }
}

#[cfg(test)]
mod retry_tests {
    use super::*;
    #[test]
    fn failed_codex_anchor_rolls_back_parser_deduplication() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("child.jsonl");
        fs::write(&p,format!("{}\n{}\n",json!({"type":"session_meta","payload":{"id":"child","thread_source":"subagent","agent_path":"/root/child","parent_thread_id":"parent"}}),json!({"type":"response_item","payload":{"type":"agent_message","recipient":"/root/child","content":[{"type":"text","text":"Message Type: NEW_TASK\nPayload:\nchild task"}]}}))).unwrap();
        let mut source = Source::default();
        let mut watcher = Watcher::new();
        watcher.booting = false;
        assert!(watcher
            .journal("codex", &p, &mut source, &mut |_| anyhow::bail!(
                "consumer failed"
            ))
            .is_err());
        let mut events = vec![];
        watcher
            .journal("codex", &p, &mut source, &mut |e| {
                events.push(e.clone());
                Ok(())
            })
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["text"], "[subagent task] child task");
    }
}

#[cfg(test)]
mod horizon_tests {
    use super::*;
    #[test]
    fn discovery_does_not_open_old_untracked_journals() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("history.jsonl");
        fs::write(&p, "not valid JSON").unwrap();
        fs::OpenOptions::new()
            .write(true)
            .open(&p)
            .unwrap()
            .set_modified(std::time::UNIX_EPOCH)
            .unwrap();
        let mut watcher = Watcher::new();
        let mut files = vec![];
        watcher.walk(t.path(), &crate::ingest::claude::Claude, &mut files);
        assert!(files.is_empty());
        watcher.sources.insert(p.clone(), Source::default());
        watcher.walk(t.path(), &crate::ingest::claude::Claude, &mut files);
        assert_eq!(files.len(), 1);
    }
}

#[cfg(test)]
mod decay_tests {
    use super::*;
    #[test]
    fn only_thinking_and_replying_stall_without_making_old_sessions_fresh() {
        let mut w = Watcher::new();
        w.booting = false;
        let old = now() - 300_000;
        for (session, kind) in [
            ("thinking", "user"),
            ("replying", "reply"),
            ("tool", "tool"),
        ] {
            w.emit(json!({"agent":"pi","session":session,"type":kind,"ts":old,"name":"build","call_id":"call"}),&mut |_| Ok(())).unwrap();
        }
        w.decay(Some(&[]), now(), &mut |_| Ok(())).unwrap();
        let snapshot = w.snapshot();
        for s in &snapshot.sessions {
            if s.session == "tool" {
                assert!(s.working);
                assert_eq!(s.last_ts, old);
            } else {
                assert!(!s.working);
                assert_eq!(s.state, "stalled");
                assert_eq!(s.last_ts, old + 1);
                assert!(!s.active);
            }
        }
    }
    #[test]
    fn observed_process_disappearance_interrupts_even_a_tool() {
        let mut w = Watcher::new();
        w.booting = false;
        w.emit(
            json!({"agent":"codex","session":"example","type":"tool","ts":now(),"name":"build"}),
            &mut |_| Ok(()),
        )
        .unwrap();
        let p = process::Process {
            pid: 123,
            agent: "codex".into(),
            session: Some("example".into()),
            cwd: String::new(),
        };
        w.decay(Some(&[p]), now(), &mut |_| Ok(())).unwrap();
        assert!(w.snapshot().sessions[0].working);
        w.decay(Some(&[]), now(), &mut |_| Ok(())).unwrap();
        assert_eq!(w.snapshot().sessions[0].state, "interrupted");
    }
    #[test]
    fn database_fingerprint_includes_wal_changes() {
        let t = tempfile::tempdir().unwrap();
        let p = t.path().join("store.db");
        fs::write(&p, "db").unwrap();
        let first = database_fingerprint(&p);
        fs::write(t.path().join("store.db-wal"), "committed change").unwrap();
        assert_ne!(first, database_fingerprint(&p));
    }
}

#[cfg(test)]
mod read_limit_tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn oversized_append_is_skipped_once_and_following_events_survive() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("session.jsonl");
        fs::write(
            &path,
            "{\"type\":\"user\",\"sessionId\":\"example\",\"message\":{\"content\":\"first\"}}\n",
        )
        .unwrap();
        let mut watcher = Watcher::new();
        watcher.booting = false;
        let mut source = Source::default();
        watcher
            .journal("claude", &path, &mut source, &mut |_| Ok(()))
            .unwrap();
        let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
        file.write_all(&vec![b'x'; LINE_CAP + 1]).unwrap();
        watcher
            .journal("claude", &path, &mut source, &mut |_| {
                panic!("oversized record emitted")
            })
            .unwrap();
        assert!(source.discarding_line);
        assert_eq!(source.skipped_records, 1);
        assert_eq!(source.offset, fs::metadata(&path).unwrap().len());
        file.write_all(b"end\n").unwrap();
        writeln!(
            file,
            "{}",
            json!({"type":"user","sessionId":"example","message":{"content":"after"}})
        )
        .unwrap();
        let mut events = vec![];
        watcher
            .journal("claude", &path, &mut source, &mut |e| {
                events.push(e.clone());
                Ok(())
            })
            .unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["text"], "after");
        assert_eq!(source.skipped_records, 1);
        assert!(!source.discarding_line);
    }
    #[test]
    fn header_and_json_file_sizes_are_bounded() {
        let bytes = vec![b'x'; LINE_CAP + 1];
        let line = bounded_line(&mut std::io::Cursor::new(&bytes), false).unwrap();
        assert!(line.oversized);
        assert!(line.body.len() <= LINE_CAP);
        let temp = tempfile::tempdir().unwrap();
        let path = temp
            .path()
            .join("session/.system_generated/messages/child.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            &path,
            json!({"sender":"agent/task-review","content":"x".repeat(JSON_CAP)}).to_string(),
        )
        .unwrap();
        let mut watcher = Watcher::new();
        watcher.booting = false;
        let mut source = Source::default();
        for _ in 0..2 {
            watcher
                .journal("antigravity", &path, &mut source, &mut |_| {
                    panic!("oversized JSON emitted")
                })
                .unwrap();
        }
        assert_eq!(source.skipped_records, 1);
    }
}
