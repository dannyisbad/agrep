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
use crate::model::{Event, Message, RawMessage};

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

type DetailedRead = (
    Vec<Message>,
    Vec<Event>,
    crate::ingest_cache::ReadOutcome,
    Vec<crate::ingest_cache::SourceReadIssue>,
);

fn issue(
    path: &Path,
    kind: &'static str,
    reason: impl Into<String>,
) -> crate::ingest_cache::SourceReadIssue {
    crate::ingest_cache::SourceReadIssue::new("cline", path, kind, reason)
}

fn task_index(
    root: &Path,
) -> (
    HashMap<String, TaskMeta>,
    crate::ingest_cache::ReadOutcome,
    Vec<crate::ingest_cache::SourceReadIssue>,
) {
    let mut map = HashMap::new();
    let state = root.join("state");
    match fs::symlink_metadata(&state) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => return (map, crate::ingest_cache::ReadOutcome::Complete, Vec::new()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (map, crate::ingest_cache::ReadOutcome::Complete, Vec::new())
        }
        Err(error) => {
            crate::ingest::warn_source_skip("cline", &state, &error);
            let problem = issue(&state, "source-stat-failed", error.to_string());
            return (
                map,
                crate::ingest_cache::ReadOutcome::Skipped,
                vec![problem],
            );
        }
    }
    let p = state.join("taskHistory.json");
    match fs::symlink_metadata(&p) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => {}
        Ok(_) => return (map, crate::ingest_cache::ReadOutcome::Complete, Vec::new()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (map, crate::ingest_cache::ReadOutcome::Complete, Vec::new())
        }
        Err(error) => {
            crate::ingest::warn_source_skip("cline", &p, &error);
            let problem = issue(&p, "source-stat-failed", error.to_string());
            return (
                map,
                crate::ingest_cache::ReadOutcome::Skipped,
                vec![problem],
            );
        }
    }
    let data = match crate::ingest::read_lossy(&p) {
        Ok(d) => d,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (map, crate::ingest_cache::ReadOutcome::Complete, Vec::new())
        }
        Err(error) => {
            crate::ingest::warn_source_skip("cline", &p, &error);
            let problem = issue(&p, "source-read-failed", error.to_string());
            return (
                map,
                crate::ingest_cache::ReadOutcome::Skipped,
                vec![problem],
            );
        }
    };
    let Ok(serde_json::Value::Array(items)) = serde_json::from_str(&data) else {
        return (
            map,
            crate::ingest_cache::ReadOutcome::Invalid,
            vec![issue(
                &p,
                "source-invalid",
                "task history is not a valid JSON array",
            )],
        );
    };
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
    (map, crate::ingest_cache::ReadOutcome::Complete, Vec::new())
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
    match fs::symlink_metadata(&path) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Complete,
                Vec::new(),
            )
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Complete,
                Vec::new(),
            )
        }
        Err(error) => {
            crate::ingest::warn_source_skip("cline", &path, &error);
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Skipped,
                vec![issue(&path, "source-stat-failed", error.to_string())],
            );
        }
    }
    // seen = messages in api_conversation_history.json
    let tally = crate::intake::file("cline", &path);
    let data = match crate::ingest::read_lossy(&path) {
        Ok(d) => d,
        Err(e) => {
            crate::ingest::warn_source_skip("cline", &path, &e);
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Skipped,
                vec![issue(&path, "source-read-failed", e.to_string())],
            );
        }
    };
    let msgs: Vec<serde_json::Value> = match serde_json::from_str(&data) {
        Ok(serde_json::Value::Array(a)) => a,
        Err(e) => {
            // whole-file failure: the file itself is the one seen-and-errored record
            tally.seen();
            tally.error(&format!("{e}: {}", crate::intake::clip(&data, 80)));
            // an indexed-but-unparseable task is incomplete; an unindexed orphan may be
            // mid-write (its content hash moves on completion)
            let outcome = if meta.is_none() {
                crate::ingest_cache::ReadOutcome::Complete
            } else {
                crate::ingest_cache::ReadOutcome::Invalid
            };
            let problems = (outcome == crate::ingest_cache::ReadOutcome::Invalid)
                .then(|| issue(&path, "source-invalid", e.to_string()))
                .into_iter()
                .collect();
            return (Vec::new(), Vec::new(), outcome, problems);
        }
        Ok(_) => {
            tally.seen();
            tally.error("expected top-level message array");
            let outcome = if meta.is_none() {
                crate::ingest_cache::ReadOutcome::Complete
            } else {
                crate::ingest_cache::ReadOutcome::Invalid
            };
            let problems = (outcome == crate::ingest_cache::ReadOutcome::Invalid)
                .then(|| issue(&path, "source-invalid", "expected top-level message array"))
                .into_iter()
                .collect();
            return (Vec::new(), Vec::new(), outcome, problems);
        }
    };
    // taskId is Date.now().to_string(): the session-start fallback for pre-`ts` messages
    let start_ts: i64 = task_id.parse().unwrap_or(0);
    let project = meta
        .map(|m| m.cwd.as_str())
        .filter(|c| !c.is_empty())
        .map(project_name)
        .unwrap_or_else(|| "cline".to_string());
    let fallback_model = meta.map(|m| m.model.clone()).unwrap_or_default();

    let mut out: Vec<RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;

    for (message_ordinal, m) in msgs.iter().enumerate() {
        tally.seen();
        let role = m.get("role").and_then(|r| r.as_str()).unwrap_or("");
        let ts = m.get("ts").and_then(|t| t.as_i64()).unwrap_or(start_ts);
        let model = m
            .get("modelInfo")
            .and_then(|mi| mi.get("modelId"))
            .and_then(|id| id.as_str())
            .unwrap_or(&fallback_model)
            .to_string();
        // content: plain string or an array of typed blocks
        let blocks: Vec<&serde_json::Value> = match m.get("content") {
            Some(serde_json::Value::Array(a)) => a.iter().collect(),
            Some(serde_json::Value::String(_)) => Vec::new(),
            _ => Vec::new(),
        };
        let plain = m.get("content").and_then(|c| c.as_str());

        if role == "user" {
            // pair tool_result blocks to their events first; they share the message
            // with (or entirely replace) the user's typed text
            let mut text = plain.map(str::to_string).unwrap_or_default();
            let mut has_tool_result = false;
            for b in &blocks {
                match b.get("type").and_then(|t| t.as_str()) {
                    Some("tool_result") => {
                        has_tool_result = true;
                        let id = b.get("tool_use_id").and_then(|i| i.as_str()).unwrap_or("");
                        if let Some(&i) = pending.get(id) {
                            let body = match b.get("content") {
                                Some(serde_json::Value::String(s)) => s.clone(),
                                Some(serde_json::Value::Array(parts)) => parts
                                    .iter()
                                    .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                                    .collect::<Vec<_>>()
                                    .join("\n"),
                                _ => String::new(),
                            };
                            let ev = &mut events[i];
                            (ev.output, ev.output_chars, ev.output_bytes) = cap_event_output(&body);
                            ev.ok = b.get("is_error").and_then(|e| e.as_bool()).map(|e| !e);
                            pending.remove(id);
                        }
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
                if has_tool_result {
                    tally.agent_row();
                } else {
                    tally.skip(crate::intake::Skip::EmptyText);
                }
                continue;
            }
            if is_wrapper(&text) {
                if has_tool_result {
                    tally.agent_row();
                } else {
                    tally.skip(crate::intake::Skip::Wrapper);
                }
                continue;
            }
            tally.row();
            out.push(RawMessage {
                agent: "cline",
                project: project.clone(),
                session: task_id.clone(),
                ts,
                turn,
                text,
                model,
                reply: String::new(),
                reply_chars: 0,
                side: false,
                parent: String::new(),
            });
            turn += 1;
        } else if role == "assistant" {
            let mut reply = plain.map(str::to_string).unwrap_or_default();
            for (block_ordinal, b) in blocks.iter().enumerate() {
                match b.get("type").and_then(|t| t.as_str()) {
                    Some("text") => {
                        if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                            if !reply.is_empty() {
                                reply.push('\n');
                            }
                            reply.push_str(t);
                        }
                    }
                    Some("tool_use") => {
                        let name = b
                            .get("name")
                            .and_then(|n| n.as_str())
                            .unwrap_or("?")
                            .to_string();
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
                            pending.insert(call_id.clone(), events.len());
                        }
                        tally.event();
                        events.push(Event {
                            agent: "cline",
                            session: task_id.clone(),
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
                        });
                    }
                    _ => {}
                }
            }
            if !reply.trim().is_empty() {
                if let Some(last) = out.last_mut() {
                    let chars = crate::ingest::append_capped(
                        &mut last.reply,
                        &reply,
                        crate::ingest::REPLY_CAP,
                    );
                    last.reply_chars += chars;
                    if last.model.is_empty() && !model.is_empty() {
                        last.model = model;
                    }
                }
            }
            tally.agent_row();
        } else {
            tally.skip(crate::intake::Skip::NonHuman);
        }
    }
    (
        out.into_iter().map(RawMessage::freeze).collect(),
        events,
        crate::ingest_cache::ReadOutcome::Complete,
        Vec::new(),
    )
}

/// Walk every editor's cline globalStorage (plus ~/.cline/data) and collect tasks.
fn collect_from_roots_detailed(store_roots: Vec<PathBuf>) -> DetailedRead {
    let mut work: Vec<(PathBuf, Option<TaskMeta>)> = Vec::new();
    let mut outcome = crate::ingest_cache::ReadOutcome::Complete;
    let mut issues = Vec::new();
    for root in store_roots {
        match fs::symlink_metadata(&root) {
            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
            Ok(_) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("cline", &root, &error);
                outcome = outcome.merge(crate::ingest_cache::ReadOutcome::Skipped);
                issues.push(issue(&root, "source-stat-failed", error.to_string()));
                continue;
            }
        }
        let tasks = root.join("tasks");
        match fs::symlink_metadata(&tasks) {
            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
            Ok(_) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("cline", &tasks, &error);
                outcome = outcome.merge(crate::ingest_cache::ReadOutcome::Skipped);
                issues.push(issue(&tasks, "source-stat-failed", error.to_string()));
                continue;
            }
        }
        let rd = match fs::read_dir(&tasks) {
            Ok(d) => d,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("cline", &tasks, &error);
                outcome = outcome.merge(crate::ingest_cache::ReadOutcome::Skipped);
                issues.push(issue(&tasks, "source-read-failed", error.to_string()));
                continue;
            }
        };
        let (mut index, index_outcome, index_issues) = task_index(&root);
        outcome = outcome.merge(index_outcome);
        issues.extend(index_issues);
        for entry in rd {
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) => {
                    crate::ingest::warn_source_skip("cline", &tasks, &error);
                    outcome = outcome.merge(crate::ingest_cache::ReadOutcome::Skipped);
                    issues.push(issue(&tasks, "source-read-failed", error.to_string()));
                    continue;
                }
            };
            let dir = entry.path();
            let is_dir = match plain_metadata(&dir) {
                Ok(Some(meta)) => meta.is_dir(),
                Ok(None) => false,
                Err(error) => {
                    crate::ingest::warn_source_skip("cline", &dir, &error);
                    outcome = outcome.merge(crate::ingest_cache::ReadOutcome::Skipped);
                    issues.push(issue(&dir, "source-stat-failed", error.to_string()));
                    continue;
                }
            };
            if is_dir {
                let meta = index.remove(&entry.file_name().to_string_lossy().to_string());
                work.push((dir, meta));
            }
        }
    }

    let pairs: Vec<DetailedRead> = work
        .par_iter()
        .map(|(dir, meta)| parse_task(dir, meta.as_ref()))
        .collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e, task_outcome, task_issues) in pairs {
        if task_outcome == crate::ingest_cache::ReadOutcome::Complete {
            msgs.extend(m);
            evts.extend(e);
        }
        outcome = outcome.merge(task_outcome);
        issues.extend(task_issues);
    }
    (msgs, evts, outcome, issues)
}

fn collect_from_roots(
    store_roots: Vec<PathBuf>,
) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    let (messages, events, outcome, _) = collect_from_roots_detailed(store_roots);
    (messages, events, outcome)
}

pub fn collect() -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
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
    fn collect(&self, _cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        let (messages, events, _) = collect();
        (messages, events)
    }
    fn collect_checked(&self, _cache: &mut crate::ingest_cache::IngestCache) -> DetailedRead {
        collect_from_roots_detailed(roots())
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        roots().into_iter().map(|r| r.join("tasks")).collect()
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
    fn runtime_issue_root(&self) -> std::path::PathBuf {
        std::env::var_os("CLINE_DIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| crate::ingest::home().join(".cline"))
    }
}

#[cfg(all(test, unix))]
mod tests {
    use std::os::unix::fs::symlink;

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
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
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
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
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
        assert_eq!(outcome, crate::ingest_cache::ReadOutcome::Invalid);
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].path, history);
        let _ = std::fs::remove_dir_all(store);
    }
}
