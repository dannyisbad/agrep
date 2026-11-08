//! cline adapter: `<globalStorage>/saoudrizwan.claude-dev/tasks/<taskId>/` in every
//! VSCode-family editor, plus `~/.cline/data/` (the CLI / JetBrains root).
//!
//! Per task: `api_conversation_history.json` (Anthropic MessageParam array, the source
//! of truth for turns and native tool_use/tool_result blocks) and a sibling
//! `state/taskHistory.json` index at the root (HistoryItem[]: cwd, model, task title).
//! taskIds are `Date.now()` millisecond strings - that anchors timestamps for older
//! tasks whose messages predate the per-message `ts` field. Classic (non-native) tool
//! calls are XML inside assistant text and stay in the reply; only real tool_use
//! blocks become events.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use crate::ingest::registry::{metadata_is_link, plain_metadata};
use crate::ingest::{cap_event_output, is_wrapper, project_name, summarize_tool_input_with_chars};
use crate::intake::{Skip, Tally};
use crate::intake_batch::{ReadOutcome, SourceReadIssue};
use crate::model::{Event, Message, RawMessage};
use serde_json::Value;

/// Every globalStorage root cline can write on this machine. Which exist is checked by
/// the caller; missing editors simply don't contribute.
fn roots() -> Vec<PathBuf> {
    const PRODUCTS: &[&str] = &["Code", "Code - Insiders", "Cursor", "Windsurf", "VSCodium"];
    let mut out = Vec::new();
    // VSCode-family globalStorage, per OS convention
    if let Some(appdata) = std::env::var_os("APPDATA") {
        let base = PathBuf::from(appdata);
        for p in PRODUCTS {
            out.push(
                base.join(p)
                    .join("User")
                    .join("globalStorage")
                    .join("saoudrizwan.claude-dev"),
            );
        }
    } else {
        let home = crate::ingest::home();
        let mac = home.join("Library").join("Application Support");
        let linux = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".config"));
        for base in [mac, linux] {
            for p in PRODUCTS {
                out.push(
                    base.join(p)
                        .join("User")
                        .join("globalStorage")
                        .join("saoudrizwan.claude-dev"),
                );
            }
        }
    }
    // standalone root (CLI / JetBrains), env-overridable
    let cline_dir = std::env::var_os("CLINE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::ingest::home().join(".cline"));
    out.push(cline_dir.join("data"));
    out
}

/// What taskHistory.json knows about each task: cwd, model, last-activity ts.
struct TaskMeta {
    cwd: String,
    model: String,
}

type DetailedRead = (Vec<Message>, Vec<Event>, ReadOutcome, Vec<SourceReadIssue>);

fn issue(path: &Path, kind: &'static str, reason: impl Into<String>) -> SourceReadIssue {
    SourceReadIssue::new("cline", path, kind, reason)
}

fn unread(outcome: ReadOutcome, issues: Vec<SourceReadIssue>) -> DetailedRead {
    (Vec::new(), Vec::new(), outcome, issues)
}

/// Whether `path` is a plain (non-link) entry of the wanted kind. Anything else, missing
/// included, is absent; a stat failure is warned about and returned as its issue.
fn present(path: &Path, wanted: fn(&fs::Metadata) -> bool) -> Result<bool, SourceReadIssue> {
    match fs::symlink_metadata(path) {
        Ok(meta) => Ok(wanted(&meta) && !metadata_is_link(&meta)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => {
            crate::ingest::warn_source_skip("cline", path, &error);
            Err(issue(path, "source-stat-failed", error.to_string()))
        }
    }
}

fn task_index(root: &Path) -> (HashMap<String, TaskMeta>, ReadOutcome, Vec<SourceReadIssue>) {
    match task_history(root) {
        TaskHistory::Entries(items) => (index_entries(items), ReadOutcome::Complete, Vec::new()),
        TaskHistory::Absent => (HashMap::new(), ReadOutcome::Complete, Vec::new()),
        TaskHistory::Unread(outcome, problem) => (HashMap::new(), outcome, vec![problem]),
    }
}

/// What `<root>/state/taskHistory.json` yielded.
enum TaskHistory {
    Entries(Vec<Value>),
    /// The root keeps no history.
    Absent,
    Unread(ReadOutcome, SourceReadIssue),
}

fn task_history(root: &Path) -> TaskHistory {
    let state = root.join("state");
    match present(&state, fs::Metadata::is_dir) {
        Ok(true) => {}
        Ok(false) => return TaskHistory::Absent,
        Err(problem) => return TaskHistory::Unread(ReadOutcome::Skipped, problem),
    }
    let p = state.join("taskHistory.json");
    match present(&p, fs::Metadata::is_file) {
        Ok(true) => {}
        Ok(false) => return TaskHistory::Absent,
        Err(problem) => return TaskHistory::Unread(ReadOutcome::Skipped, problem),
    }
    let data = match crate::ingest::read_lossy(&p) {
        Ok(d) => d,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return TaskHistory::Absent,
        Err(error) => {
            crate::ingest::warn_source_skip("cline", &p, &error);
            let problem = issue(&p, "source-read-failed", error.to_string());
            return TaskHistory::Unread(ReadOutcome::Skipped, problem);
        }
    };
    let Ok(Value::Array(items)) = serde_json::from_str(&data) else {
        let problem = issue(
            &p,
            "source-invalid",
            "task history is not a valid JSON array",
        );
        return TaskHistory::Unread(ReadOutcome::Invalid, problem);
    };
    TaskHistory::Entries(items)
}

fn index_entries(items: Vec<Value>) -> HashMap<String, TaskMeta> {
    let mut map = HashMap::new();
    for it in items {
        let id = match it.get("id").and_then(|i| i.as_str()) {
            Some(i) => i.to_string(),
            None => continue,
        };
        map.insert(
            id,
            TaskMeta {
                cwd: it
                    .get("cwdOnTaskInitialization")
                    .and_then(|c| c.as_str())
                    .unwrap_or_default()
                    .to_string(),
                model: it
                    .get("modelId")
                    .and_then(|m| m.as_str())
                    .unwrap_or_default()
                    .to_string(),
            },
        );
    }
    map
}

/// The user's actual words from a cline user turn: drop the bulky injected
/// `<environment_details>` block, unwrap `<task>`/`<feedback>`/`<answer>` envelopes.
fn clean_user_text(raw: &str) -> String {
    let mut s = raw.to_string();
    if let (Some(a), Some(b)) = (
        s.find("<environment_details>"),
        s.rfind("</environment_details>"),
    ) {
        if a < b {
            s.replace_range(a..b + "</environment_details>".len(), "");
        }
    }
    for tag in ["task", "feedback", "answer", "user_message"] {
        let open = format!("<{tag}>");
        let close = format!("</{tag}>");
        if let (Some(a), Some(b)) = (s.find(&open), s.rfind(&close)) {
            if a < b {
                s = s[a + open.len()..b].to_string();
                break;
            }
        }
    }
    s.trim().to_string()
}

fn parse_task(dir: &Path, meta: Option<&TaskMeta>) -> DetailedRead {
    let task_id = dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let path = dir.join("api_conversation_history.json");
    match present(&path, fs::Metadata::is_file) {
        Ok(true) => {}
        Ok(false) => return unread(ReadOutcome::Complete, Vec::new()),
        Err(problem) => return unread(ReadOutcome::Skipped, vec![problem]),
    }
    // seen = messages in api_conversation_history.json
    let tally = crate::intake::tally();
    let msgs = match read_messages(&path, meta.is_some(), &tally) {
        Ok(msgs) => msgs,
        Err((outcome, problems)) => return unread(outcome, problems),
    };
    let mut task = TaskParse::new(task_id, meta, &tally);
    for (message_ordinal, m) in msgs.iter().enumerate() {
        task.message(message_ordinal, m);
    }
    (
        task.out.into_iter().map(RawMessage::freeze).collect(),
        task.events,
        ReadOutcome::Complete,
        Vec::new(),
    )
}

/// The task's message array, or the read's outcome when the file is unreadable or is not
/// a message array.
fn read_messages(
    path: &Path,
    indexed: bool,
    tally: &Tally,
) -> Result<Vec<Value>, (ReadOutcome, Vec<SourceReadIssue>)> {
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            crate::ingest::warn_source_skip("cline", path, &e);
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            let problem = issue(path, "source-read-failed", e.to_string());
            return Err((ReadOutcome::Skipped, vec![problem]));
        }
    };
    match serde_json::from_str::<Value>(&data) {
        Ok(Value::Array(a)) => Ok(a),
        Err(e) => {
            // whole-file failure: the file itself is the one seen-and-errored record
            tally.seen();
            tally.error(&format!("{e}: {}", crate::intake::clip(&data, 80)));
            Err(unparsed(path, indexed, e))
        }
        Ok(_) => {
            tally.seen();
            tally.error("expected top-level message array");
            Err(unparsed(path, indexed, "expected top-level message array"))
        }
    }
}

/// an indexed-but-unparseable task is incomplete; an unindexed orphan may be
/// mid-write (its content hash moves on completion)
fn unparsed(
    path: &Path,
    indexed: bool,
    reason: impl std::fmt::Display,
) -> (ReadOutcome, Vec<SourceReadIssue>) {
    if indexed {
        let problem = issue(path, "source-invalid", reason.to_string());
        (ReadOutcome::Invalid, vec![problem])
    } else {
        (ReadOutcome::Complete, Vec::new())
    }
}

/// What one message contributes, read once before its role decides the handling.
struct MessageParts<'m> {
    ordinal: usize,
    ts: i64,
    model: String,
    blocks: Vec<&'m Value>,
    plain: Option<&'m str>,
}

/// One task's message walk: user turns become rows, assistant text joins the last row as
/// its reply, and native tool_use blocks become events completed by their tool_result.
struct TaskParse<'t> {
    tally: &'t Tally,
    task_id: String,
    project: String,
    fallback_model: String,
    start_ts: i64,
    out: Vec<RawMessage>,
    events: Vec<Event>,
    pending: HashMap<String, usize>,
    turn: u32,
    // blocks are in order: a tool_use sets the flag the next text consumes
    worked: bool,
}

impl<'t> TaskParse<'t> {
    fn new(task_id: String, meta: Option<&TaskMeta>, tally: &'t Tally) -> Self {
        // taskId is Date.now().to_string(): the session-start fallback for pre-`ts` messages
        let start_ts: i64 = task_id.parse().unwrap_or(0);
        let project = meta
            .map(|m| m.cwd.as_str())
            .filter(|c| !c.is_empty())
            .map(project_name)
            .unwrap_or_else(|| "cline".to_string());
        let fallback_model = meta.map(|m| m.model.clone()).unwrap_or_default();
        Self {
            tally,
            task_id,
            project,
            fallback_model,
            start_ts,
            out: Vec::new(),
            events: Vec::new(),
            pending: HashMap::new(),
            turn: 0,
            worked: false,
        }
    }

    fn message(&mut self, ordinal: usize, m: &Value) {
        self.tally.seen();
        let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("");
        let ts = m
            .get("ts")
            .and_then(|t| t.as_i64())
            .unwrap_or(self.start_ts);
        let model = m
            .get("modelInfo")
            .and_then(|mi| mi.get("modelId"))
            .and_then(|id| id.as_str())
            .unwrap_or(&self.fallback_model)
            .to_string();
        // content: plain string or an array of typed blocks
        let blocks: Vec<&Value> = match m.get("content") {
            Some(Value::Array(a)) => a.iter().collect(),
            Some(Value::String(_)) => Vec::new(),
            _ => Vec::new(),
        };
        let plain = m.get("content").and_then(|c| c.as_str());
        let parts = MessageParts {
            ordinal,
            ts,
            model,
            blocks,
            plain,
        };
        if role == "user" {
            self.user(parts);
        } else if role == "assistant" {
            self.assistant(parts);
        } else {
            self.tally.skip(Skip::NonHuman);
        }
    }

    fn user(&mut self, parts: MessageParts<'_>) {
        // pair tool_result blocks to their events first; they share the message
        // with (or entirely replace) the user's typed text
        let mut text = parts.plain.map(str::to_string).unwrap_or_default();
        let mut has_tool_result = false;
        for b in &parts.blocks {
            match b.get("type").and_then(|t| t.as_str()) {
                Some("tool_result") => {
                    has_tool_result = true;
                    self.pair_result(b);
                }
                Some("text") => {
                    if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                        if !text.is_empty() {
                            text.push('\n');
                        }
                        text.push_str(t);
                    }
                }
                _ => {}
            }
        }
        let text = clean_user_text(&text);
        if text.is_empty() {
            self.drop_user(has_tool_result, Skip::EmptyText);
            return;
        }
        if is_wrapper(&text) {
            self.drop_user(has_tool_result, Skip::Wrapper);
            return;
        }
        self.tally.row();
        self.worked = false;
        self.out.push(RawMessage {
            agent: "cline",
            project: self.project.clone(),
            session: self.task_id.clone(),
            ts: parts.ts,
            turn: self.turn,
            text,
            model: parts.model,
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }

    /// A user message with no typed text is agent-side when it carried tool results.
    fn drop_user(&self, has_tool_result: bool, skip: Skip) {
        if has_tool_result {
            self.tally.agent_row();
        } else {
            self.tally.skip(skip);
        }
    }

    fn pair_result(&mut self, b: &Value) {
        let id = b.get("tool_use_id").and_then(|i| i.as_str()).unwrap_or("");
        let Some(&i) = self.pending.get(id) else {
            return;
        };
        let body = match b.get("content") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Array(parts)) => parts
                .iter()
                .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join("\n"),
            _ => String::new(),
        };
        let ev = &mut self.events[i];
        (ev.output, ev.output_chars, ev.output_bytes) = cap_event_output(&body);
        ev.ok = b.get("is_error").and_then(|e| e.as_bool()).map(|e| !e);
        self.pending.remove(id);
    }

    fn assistant(&mut self, parts: MessageParts<'_>) {
        let mut reply = parts.plain.map(str::to_string).unwrap_or_default();
        // the message's text blocks make one reply block: its join is what came before the
        // first of them, calls after that join it to the next message
        let mut worked_before = parts
            .plain
            .filter(|text| !text.trim().is_empty())
            .map(|_| std::mem::take(&mut self.worked));
        for (block_ordinal, b) in parts.blocks.iter().enumerate() {
            match b.get("type").and_then(|t| t.as_str()) {
                Some("text") => {
                    if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                        if worked_before.is_none() {
                            worked_before = Some(std::mem::take(&mut self.worked));
                        }
                        if !reply.is_empty() {
                            reply.push('\n');
                        }
                        reply.push_str(t);
                    }
                }
                Some("tool_use") => self.tool_use(b, parts.ordinal, block_ordinal, parts.ts),
                _ => {}
            }
        }
        if !reply.trim().is_empty() {
            if let Some(last) = self.out.last_mut() {
                crate::ingest::append_reply(last, &reply);
                crate::ingest::ReplyOrder::joined_known(
                    &mut last.reply_shape,
                    worked_before.unwrap_or(false),
                );

                if last.model.is_empty() && !parts.model.is_empty() {
                    last.model = parts.model;
                }
            }
        }
        self.tally.agent_row();
    }

    fn tool_use(&mut self, b: &Value, message_ordinal: usize, block_ordinal: usize, ts: i64) {
        let name = b
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("?")
            .to_string();
        self.worked |= !crate::ingest::is_bookkeeping_tool(&name);
        let native_call_id = b
            .get("id")
            .and_then(|i| i.as_str())
            .filter(|id| !id.trim().is_empty());
        let call_id = native_call_id
            .map(str::to_string)
            .unwrap_or_else(|| format!("cline:{message_ordinal}:{block_ordinal}"));
        let (input, input_chars) = b
            .get("input")
            .map(summarize_tool_input_with_chars)
            .unwrap_or_default();
        if native_call_id.is_some() {
            self.pending.insert(call_id.clone(), self.events.len());
        }
        self.tally.event();
        self.events.push(Event {
            turn: None,
            agent: "cline",
            session: self.task_id.clone(),
            ts,
            kind: "tool",
            name,
            input,
            output: String::new(),
            input_chars,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id,
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        });
    }
}

/// Read outcome and issues accumulated across roots and tasks.
struct Health {
    outcome: ReadOutcome,
    issues: Vec<SourceReadIssue>,
}

impl Health {
    fn skipped(&mut self, problem: SourceReadIssue) {
        self.outcome = self.outcome.merge(ReadOutcome::Skipped);
        self.issues.push(problem);
    }

    fn absorb(&mut self, outcome: ReadOutcome, issues: Vec<SourceReadIssue>) {
        self.outcome = self.outcome.merge(outcome);
        self.issues.extend(issues);
    }

    /// Whether `path` is a plain directory; a stat failure is recorded as skipped.
    fn plain_dir(&mut self, path: &Path) -> bool {
        match present(path, fs::Metadata::is_dir) {
            Ok(found) => found,
            Err(problem) => {
                self.skipped(problem);
                false
            }
        }
    }
}

/// Queue every task directory under `<root>/tasks` with its taskHistory.json entry.
fn discover_root(root: &Path, work: &mut Vec<(PathBuf, Option<TaskMeta>)>, health: &mut Health) {
    if !health.plain_dir(root) {
        return;
    }
    let tasks = root.join("tasks");
    if !health.plain_dir(&tasks) {
        return;
    }
    let rd = match fs::read_dir(&tasks) {
        Ok(d) => d,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
        Err(error) => {
            crate::ingest::warn_source_skip("cline", &tasks, &error);
            health.skipped(issue(&tasks, "source-read-failed", error.to_string()));
            return;
        }
    };
    let (mut index, index_outcome, index_issues) = task_index(root);
    health.absorb(index_outcome, index_issues);
    for entry in rd {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("cline", &tasks, &error);
                health.skipped(issue(&tasks, "source-read-failed", error.to_string()));
                continue;
            }
        };
        let dir = entry.path();
        let is_dir = match plain_metadata(&dir) {
            Ok(Some(meta)) => meta.is_dir(),
            Ok(None) => false,
            Err(error) => {
                crate::ingest::warn_source_skip("cline", &dir, &error);
                health.skipped(issue(&dir, "source-stat-failed", error.to_string()));
                continue;
            }
        };
        if is_dir {
            let meta = index.remove(&entry.file_name().to_string_lossy().to_string());
            work.push((dir, meta));
        }
    }
}

/// Walk every editor's cline globalStorage (plus ~/.cline/data) and collect tasks.
fn collect_from_roots_detailed(store_roots: Vec<PathBuf>) -> DetailedRead {
    let mut work: Vec<(PathBuf, Option<TaskMeta>)> = Vec::new();
    let mut health = Health {
        outcome: ReadOutcome::Complete,
        issues: Vec::new(),
    };
    for root in store_roots {
        discover_root(&root, &mut work, &mut health);
    }

    let pairs: Vec<DetailedRead> = work
        .par_iter()
        .map(|(dir, meta)| {
            crate::ingest::parse_guard::isolate("cline", dir, || parse_task(dir, meta.as_ref()))
                .unwrap_or_else(|panic| {
                    unread(
                        ReadOutcome::Skipped,
                        vec![issue(dir, "source-read-failed", panic.reason)],
                    )
                })
        })
        .collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e, task_outcome, task_issues) in pairs {
        if task_outcome == ReadOutcome::Complete {
            msgs.extend(m);
            evts.extend(e);
        }
        health.absorb(task_outcome, task_issues);
    }
    (msgs, evts, health.outcome, health.issues)
}

fn collect_from_roots(
    store_roots: Vec<PathBuf>,
) -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    let (messages, events, outcome, _) = collect_from_roots_detailed(store_roots);
    (messages, events, outcome)
}

pub fn collect() -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    collect_from_roots(roots())
}

/// Registry entry (see ingest::registry). Full-parse: ignores the cache.
pub struct Cline;
impl crate::ingest::registry::Adapter for Cline {
    fn name(&self) -> &'static str {
        "cline"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Always
    }
    fn collect(&self, _cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        let (messages, events, _) = collect();
        (messages, events)
    }
    fn collect_checked(&self, _cache: &mut crate::intake_batch::IntakeBatch) -> DetailedRead {
        collect_from_roots_detailed(roots())
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        roots().into_iter().map(|r| r.join("tasks")).collect()
    }
    fn archive_content(&self, path: &std::path::Path) -> bool {
        path.extension().is_some_and(|e| e == "json")
            && (path.components().any(|p| p.as_os_str() == "tasks")
                || path.file_name().is_some_and(|n| n == "taskHistory.json"))
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        // the conversation itself; task_metadata/ui_messages are auxiliary state
        path.file_name().and_then(|x| x.to_str()) == Some("api_conversation_history.json")
    }
    fn freshness_roots(&self) -> Vec<std::path::PathBuf> {
        // taskHistory.json supplies cwd/model attribution, so it is freshness-relevant
        // even though the doctor census counts only chats
        roots()
    }
    fn freshness_content(&self, path: &std::path::Path) -> bool {
        matches!(
            path.file_name().and_then(|x| x.to_str()),
            Some("api_conversation_history.json" | "taskHistory.json")
        )
    }
    fn unit_scoped_read_issue(&self, path: &std::path::Path) -> bool {
        // A task's own directory, never taskHistory.json, which attributes every task.
        roots().into_iter().any(|root| {
            path.strip_prefix(root.join("tasks"))
                .is_ok_and(|task| task.components().next().is_some())
        })
    }
    fn runtime_issue_root(&self) -> std::path::PathBuf {
        std::env::var_os("CLINE_DIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| crate::ingest::home().join(".cline"))
    }
}

#[cfg(test)]
mod tests {
    fn symlink(
        from: impl AsRef<std::path::Path>,
        to: impl AsRef<std::path::Path>,
    ) -> std::io::Result<()> {
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(from, to)
        }
        #[cfg(windows)]
        {
            if from.as_ref().is_dir() {
                std::os::windows::fs::symlink_dir(from, to)
            } else {
                std::os::windows::fs::symlink_file(from, to)
            }
        }
    }

    fn root(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "agrep-cline-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[test]
    fn discovery_rejects_symlinked_tasks_and_history_files() {
        let store = root("links");
        let outside = root("outside");
        let safe = store.join("tasks/safe");
        let linked_leaf = store.join("tasks/linked-leaf");
        let outside_task = outside.join("task");
        std::fs::create_dir_all(&safe).unwrap();
        std::fs::create_dir_all(&linked_leaf).unwrap();
        std::fs::create_dir_all(&outside_task).unwrap();
        let inside = r#"[{"role":"user","content":"inside","ts":1}]"#;
        let external = r#"[{"role":"user","content":"outside","ts":1}]"#;
        std::fs::write(safe.join("api_conversation_history.json"), inside).unwrap();
        let outside_history = outside_task.join("api_conversation_history.json");
        std::fs::write(&outside_history, external).unwrap();
        symlink(&outside_task, store.join("tasks/linked-task")).unwrap();
        symlink(
            &outside_history,
            linked_leaf.join("api_conversation_history.json"),
        )
        .unwrap();

        let (messages, _, healthy) = super::collect_from_roots(vec![store.clone()]);
        assert_eq!(healthy, crate::intake_batch::ReadOutcome::Complete);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "inside");
        let _ = std::fs::remove_dir_all(store);
        let _ = std::fs::remove_dir_all(outside);
    }

    #[test]
    fn discovery_rejects_symlinked_store_and_tasks_dirs() {
        let outside = root("outside-dirs");
        let outside_task = outside.join("tasks/task");
        std::fs::create_dir_all(&outside_task).unwrap();
        std::fs::write(
            outside_task.join("api_conversation_history.json"),
            r#"[{"role":"user","content":"outside","ts":1}]"#,
        )
        .unwrap();
        let store_link = root("store-link");
        symlink(&outside, &store_link).unwrap();
        let store = root("tasks-link");
        std::fs::create_dir_all(&store).unwrap();
        symlink(outside.join("tasks"), store.join("tasks")).unwrap();

        let (messages, events, healthy) =
            super::collect_from_roots(vec![store_link.clone(), store.clone()]);
        assert_eq!(healthy, crate::intake_batch::ReadOutcome::Complete);
        assert!(messages.is_empty());
        assert!(events.is_empty());
        let _ = std::fs::remove_file(store_link);
        let _ = std::fs::remove_dir_all(store);
        let _ = std::fs::remove_dir_all(outside);
    }

    #[test]
    fn invalid_indexed_task_reports_its_exact_source_path() {
        let store = root("invalid-path");
        let task = store.join("tasks/task");
        let history = task.join("api_conversation_history.json");
        std::fs::create_dir_all(&task).unwrap();
        std::fs::create_dir_all(store.join("state")).unwrap();
        std::fs::write(store.join("state/taskHistory.json"), r#"[{"id":"task"}]"#).unwrap();
        std::fs::write(&history, b"{broken").unwrap();

        let (_, _, outcome, issues) = super::collect_from_roots_detailed(vec![store.clone()]);
        assert_eq!(outcome, crate::intake_batch::ReadOutcome::Invalid);
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].path, history);
        let _ = std::fs::remove_dir_all(store);
    }
}
