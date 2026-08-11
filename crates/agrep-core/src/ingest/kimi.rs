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

use crate::ingest::registry::{metadata_is_link, plain_metadata};
use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, project_name,
    summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::model::{Event, Message, RawMessage};

const REGISTRY_MAX_BYTES: u64 = 8 * 1024 * 1024;

/// Concatenated text parts of a kosong content value (string, or array of typed parts).
/// `think` parts are the model's reasoning, not its reply - excluded.
fn text_of(content: &serde_json::Value) -> String {
    match content {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(parts) => {
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

/// What the wire log contributes: ordered user-turn timestamps, per-call timestamps,
/// and per-call results (output + is_error, the only honest ok signal kimi records).
#[derive(Default)]
struct Wire {
    turn_ts: Vec<i64>,
    call_ts: HashMap<String, i64>,
    call_result: HashMap<String, (String, usize, usize, bool)>,
}

fn parse_wire(path: &Path) -> (Wire, bool) {
    let mut w = Wire::default();
    // seen = non-blank lines of wire.jsonl
    let tally = crate::intake::file("kimi", path);
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            crate::ingest::warn_source_skip("kimi", path, &e);
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (w, false);
        }
    };
    let healthy = true;
    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        tally.seen();
        let v: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(line, 80)));
                continue;
            }
        };
        let ts = crate::ingest::parse_timestamp::epoch_secs_f64(
            v.get("timestamp").and_then(|t| t.as_f64()).unwrap_or(0.0),
        );
        let msg = match v.get("message") {
            Some(m) => m,
            None => {
                tally.skip(crate::intake::Skip::Meta);
                continue;
            }
        };
        let ty = msg.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let payload = msg.get("payload").unwrap_or(&serde_json::Value::Null);
        match ty {
            "TurnBegin" | "SteerInput" => {
                w.turn_ts.push(ts);
                tally.skip(crate::intake::Skip::Meta);
            }
            "ToolCall" => {
                if let Some(id) = payload.get("id").and_then(|i| i.as_str()) {
                    w.call_ts.insert(id.to_string(), ts);
                }
                tally.agent_row();
            }
            "ToolResult" => {
                let id = payload.get("tool_call_id").and_then(|i| i.as_str());
                if let (Some(id), Some(rv)) = (id, payload.get("return_value")) {
                    let (out, output_chars, output_bytes) = rv
                        .get("output")
                        .map(|o| match o {
                            serde_json::Value::String(s) => cap_event_output(s),
                            other => cap_event_output(&text_of(other)),
                        })
                        .unwrap_or_default();
                    let is_err = rv
                        .get("is_error")
                        .and_then(|e| e.as_bool())
                        .unwrap_or(false);
                    w.call_result
                        .insert(id.to_string(), (out, output_chars, output_bytes, is_err));
                }
                tally.agent_row();
            }
            _ => tally.skip(crate::intake::Skip::NonMessage),
        }
    }
    (w, healthy)
}

fn parse_session(dir: &Path, project: &str, parent: &str) -> (Vec<Message>, Vec<Event>, bool) {
    let session = dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let context = dir.join("context.jsonl");
    match fs::symlink_metadata(&context) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => {}
        Ok(_) => return (Vec::new(), Vec::new(), true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (Vec::new(), Vec::new(), true)
        }
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &context, &error);
            return (Vec::new(), Vec::new(), false);
        }
    }
    // seen = non-blank lines of context.jsonl
    let tally = crate::intake::file("kimi", &context);
    let data = match crate::ingest::read_lossy(&context) {
        Ok(d) => d,
        Err(e) => {
            crate::ingest::warn_source_skip("kimi", &context, &e);
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };
    let wire_path = dir.join("wire.jsonl");
    let (wire, healthy) = match fs::symlink_metadata(&wire_path) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => parse_wire(&wire_path),
        Ok(_) => (Wire::default(), true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (Wire::default(), true),
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &wire_path, &error);
            (Wire::default(), false)
        }
    };

    let mut out: Vec<RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    // call_id -> `events` index, so a tool-role context line can backfill a result the wire missed.
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;
    let mut n_user = 0usize;
    let mut last_ts = 0i64;

    // Parse fully first: user timestamps align against the TAIL of the wire turn
    // stream — /clear rotates context.jsonl but wire.jsonl keeps the whole session,
    // so head alignment would stamp post-clear turns with pre-clear times.
    let mut records: Vec<(usize, serde_json::Value)> = Vec::new();
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
    let n_users = records
        .iter()
        .filter(|(_, v)| v.get("role").and_then(|r| r.as_str()) == Some("user"))
        .count();
    let ts_offset = wire.turn_ts.len().saturating_sub(n_users);

    for (record_ordinal, v) in &records {
        let role = v.get("role").and_then(|r| r.as_str()).unwrap_or("");
        match role {
            "user" => {
                let ts = wire
                    .turn_ts
                    .get(ts_offset + n_user)
                    .copied()
                    .unwrap_or(last_ts);
                n_user += 1;
                last_ts = ts;
                let text = v.get("content").map(text_of).unwrap_or_default();
                if text.trim().is_empty() {
                    tally.skip(crate::intake::Skip::EmptyText);
                    continue;
                }
                if is_wrapper(&text) {
                    tally.skip(crate::intake::Skip::Wrapper);
                    continue;
                }
                tally.row();
                out.push(RawMessage {
                    agent: "kimi",
                    project: project.to_string(),
                    session: session.clone(),
                    ts,
                    turn,
                    text,
                    model: String::new(),
                    reply: String::new(),
                    reply_chars: 0,
                    side: !parent.is_empty(),
                    parent: parent.to_string(),
                });
                turn += 1;
            }
            "assistant" => {
                if let Some(content) = v.get("content") {
                    let txt = text_of(content);
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
                }
                if let Some(calls) = v.get("tool_calls").and_then(|c| c.as_array()) {
                    for (call_ordinal, c) in calls.iter().enumerate() {
                        let f = c.get("function").unwrap_or(&serde_json::Value::Null);
                        let name = f
                            .get("name")
                            .and_then(|n| n.as_str())
                            .unwrap_or("?")
                            .to_string();
                        let native_call_id = c
                            .get("id")
                            .and_then(|i| i.as_str())
                            .filter(|id| !id.trim().is_empty());
                        let call_id = native_call_id.map(str::to_string).unwrap_or_else(|| {
                            format!("kimi:{session}:{record_ordinal}:{call_ordinal}")
                        });
                        // arguments is a JSON-encoded string
                        let (input, input_chars) = f
                            .get("arguments")
                            .and_then(|a| a.as_str())
                            .map(|args| {
                                serde_json::from_str::<serde_json::Value>(args)
                                    .map(|v| summarize_tool_input_with_chars(&v))
                                    .unwrap_or_else(|_| cap_str_with_chars(args, EVENT_CAP))
                            })
                            .unwrap_or_default();
                        let (output, output_chars, output_bytes, ok) =
                            match wire.call_result.get(&call_id) {
                                Some((out, chars, bytes, is_err)) => {
                                    (out.clone(), *chars, *bytes, Some(!is_err))
                                }
                                None => (String::new(), 0, 0, None),
                            };
                        let ts = wire.call_ts.get(&call_id).copied().unwrap_or(last_ts);
                        if native_call_id.is_some() && ok.is_none() {
                            pending.insert(call_id.clone(), events.len());
                        }
                        tally.event();
                        events.push(Event {
                            agent: "kimi",
                            session: session.clone(),
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
                        });
                    }
                }
                tally.agent_row();
            }
            "tool" => {
                // wire recorded no result: take output from the tool message;
                // a leading <system>ERROR: tag flags failure
                let id = v.get("tool_call_id").and_then(|i| i.as_str()).unwrap_or("");
                if let Some(&i) = pending.get(id) {
                    let txt = v.get("content").map(text_of).unwrap_or_default();
                    let ev = &mut events[i];
                    (ev.output, ev.output_chars, ev.output_bytes) = cap_event_output(&txt);
                    ev.ok = Some(!txt.trim_start().starts_with("<system>ERROR"));
                    pending.remove(id);
                }
                tally.agent_row();
            }
            role if role.starts_with('_') => tally.skip(crate::intake::Skip::Meta),
            "" => tally.skip(crate::intake::Skip::NonMessage),
            _ => tally.skip(crate::intake::Skip::NonHuman),
        }
    }
    (
        out.into_iter().map(RawMessage::freeze).collect(),
        events,
        healthy,
    )
}

/// Map each `sessions/<md5>/` dir to its real workdir via the kimi.json registry.
fn workdir_registry(root: &Path) -> (HashMap<String, String>, bool) {
    let mut map = HashMap::new();
    let path = root.join("kimi.json");
    match fs::symlink_metadata(&path) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => {}
        Ok(_) => return (map, true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return (map, true),
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
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&data) else {
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

/// Walk every `sessions/<md5(workdir)>/<uuid>/` and collect kimi messages + tool events.
fn collect_from_root(root: &Path) -> (Vec<Message>, Vec<Event>, bool) {
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => return (Vec::new(), Vec::new(), true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (Vec::new(), Vec::new(), true)
        }
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", root, &error);
            return (Vec::new(), Vec::new(), false);
        }
    }
    let sessions = root.join("sessions");
    match fs::symlink_metadata(&sessions) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => return (Vec::new(), Vec::new(), true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (Vec::new(), Vec::new(), true)
        }
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &sessions, &error);
            return (Vec::new(), Vec::new(), false);
        }
    }
    let rd = match fs::read_dir(&sessions) {
        Ok(d) => d,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (Vec::new(), Vec::new(), true)
        }
        Err(error) => {
            crate::ingest::warn_source_skip("kimi", &sessions, &error);
            return (Vec::new(), Vec::new(), false);
        }
    };
    let (registry, mut healthy) = workdir_registry(root);

    // (session dir, project, parent); project via the workdir registry, else "kimi" for unregistered dirs.
    let mut work: Vec<(PathBuf, String, String)> = Vec::new();
    for entry in rd {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", &sessions, &error);
                healthy = false;
                continue;
            }
        };
        let dir = entry.path();
        let is_dir = match plain_metadata(&dir) {
            Ok(Some(meta)) => meta.is_dir(),
            Ok(None) => false,
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", &dir, &error);
                healthy = false;
                continue;
            }
        };
        if !is_dir {
            continue;
        }
        let key = entry.file_name().to_string_lossy().to_string();
        let project = registry
            .get(&key)
            .map(|p| project_name(p))
            .unwrap_or_else(|| "kimi".to_string());
        match fs::read_dir(&dir) {
            Ok(sess) => {
                for s in sess {
                    let s = match s {
                        Ok(s) => s,
                        Err(error) => {
                            crate::ingest::warn_source_skip("kimi", &dir, &error);
                            healthy = false;
                            continue;
                        }
                    };
                    let sd = s.path();
                    let is_dir = match plain_metadata(&sd) {
                        Ok(Some(meta)) => meta.is_dir(),
                        Ok(None) => false,
                        Err(error) => {
                            crate::ingest::warn_source_skip("kimi", &sd, &error);
                            healthy = false;
                            continue;
                        }
                    };
                    if is_dir {
                        let parent = s.file_name().to_string_lossy().to_string();
                        work.push((sd.clone(), project.clone(), String::new()));
                        let subagents = sd.join("subagents");
                        match fs::symlink_metadata(&subagents) {
                            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
                            Ok(_) => continue,
                            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                            Err(error) => {
                                crate::ingest::warn_source_skip("kimi", &subagents, &error);
                                healthy = false;
                                continue;
                            }
                        }
                        match fs::read_dir(&subagents) {
                            Ok(children) => {
                                for child in children {
                                    let child = match child {
                                        Ok(child) => child,
                                        Err(error) => {
                                            crate::ingest::warn_source_skip(
                                                "kimi", &subagents, &error,
                                            );
                                            healthy = false;
                                            continue;
                                        }
                                    };
                                    let child_dir = child.path();
                                    let is_dir = match plain_metadata(&child_dir) {
                                        Ok(Some(meta)) => meta.is_dir(),
                                        Ok(None) => false,
                                        Err(error) => {
                                            crate::ingest::warn_source_skip(
                                                "kimi", &child_dir, &error,
                                            );
                                            healthy = false;
                                            continue;
                                        }
                                    };
                                    if is_dir {
                                        work.push((child_dir, project.clone(), parent.clone()));
                                    }
                                }
                            }
                            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                            Err(error) => {
                                crate::ingest::warn_source_skip("kimi", &subagents, &error);
                                healthy = false;
                            }
                        }
                    }
                }
            }
            Err(error) => {
                crate::ingest::warn_source_skip("kimi", &dir, &error);
                healthy = false;
            }
        }
    }

    let pairs: Vec<(Vec<Message>, Vec<Event>, bool)> = work
        .par_iter()
        .map(|(d, proj, parent)| parse_session(d, proj, parent))
        .collect();
    let mut msgs = Vec::new();
    let mut evts = Vec::new();
    for (m, e, session_healthy) in pairs {
        if session_healthy {
            msgs.extend(m);
            evts.extend(e);
        } else {
            healthy = false;
        }
    }
    (msgs, evts, healthy)
}

pub fn collect() -> (Vec<Message>, Vec<Event>, bool) {
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
    fn collect(&self, _cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        let (messages, events, _) = collect();
        (messages, events)
    }
    fn collect_checked(
        &self,
        _cache: &mut crate::ingest_cache::IngestCache,
    ) -> (
        Vec<Message>,
        Vec<Event>,
        crate::ingest_cache::ReadOutcome,
        Vec<crate::ingest_cache::SourceReadIssue>,
    ) {
        let (messages, events, healthy) = collect();
        let outcome = if healthy {
            crate::ingest_cache::ReadOutcome::Complete
        } else {
            crate::ingest_cache::ReadOutcome::Skipped
        };
        (messages, events, outcome, Vec::new())
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

#[cfg(test)]
mod tests {
    use super::parse_session;

    fn session_dir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "agrep-kimi-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn malformed_multibyte_line_is_an_error_not_a_panic() {
        let dir = session_dir("clip");
        // 79 ASCII bytes then a 3-byte char spanning bytes 79..82: a fixed byte-80
        // slice in the error path is not a char boundary.
        let bad = format!("{}汉字 truncated mid-write", "x".repeat(79));
        std::fs::write(
            dir.join("context.jsonl"),
            format!(
                "{bad}\n{}\n",
                r#"{"role":"user","content":"still ingested"}"#
            ),
        )
        .unwrap();
        let (messages, _, _) = parse_session(&dir, "proj", "");
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "still ingested");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn rotated_context_aligns_timestamps_to_the_wire_tail() {
        let dir = session_dir("rotate");
        // wire.jsonl spans the whole session: three pre-/clear turns, then the two
        // turns the current (post-/clear) context.jsonl actually contains.
        let wire: String = [1000, 2000, 3000, 4000, 5000]
            .iter()
            .map(|ts| {
                format!("{{\"timestamp\":{ts}.0,\"message\":{{\"type\":\"TurnBegin\",\"payload\":{{}}}}}}\n")
            })
            .collect();
        std::fs::write(dir.join("wire.jsonl"), wire).unwrap();
        std::fs::write(
            dir.join("context.jsonl"),
            concat!(
                "{\"role\":\"user\",\"content\":\"post-clear question\"}\n",
                "{\"role\":\"assistant\",\"content\":\"answer\"}\n",
                "{\"role\":\"user\",\"content\":\"follow-up\"}\n",
            ),
        )
        .unwrap();
        let (messages, _, healthy) = parse_session(&dir, "proj", "");
        assert!(healthy);
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[0].ts, 4_000_000);
        assert_eq!(messages[1].ts, 5_000_000);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(unix)]
    #[test]
    fn parse_rejects_symlinked_context_and_wire_files() {
        use std::os::unix::fs::symlink;

        let dir = session_dir("linked-leaves");
        let outside = dir.with_extension("outside");
        std::fs::create_dir_all(&outside).unwrap();
        let context = outside.join("context.jsonl");
        std::fs::write(&context, "{\"role\":\"user\",\"content\":\"outside\"}\n").unwrap();
        symlink(&context, dir.join("context.jsonl")).unwrap();
        let (messages, events, healthy) = parse_session(&dir, "proj", "");
        assert!(healthy);
        assert!(messages.is_empty());
        assert!(events.is_empty());

        std::fs::remove_file(dir.join("context.jsonl")).unwrap();
        std::fs::write(
            dir.join("context.jsonl"),
            "{\"role\":\"user\",\"content\":\"inside\"}\n",
        )
        .unwrap();
        let wire = outside.join("wire.jsonl");
        std::fs::write(
            &wire,
            "{\"timestamp\":42.0,\"message\":{\"type\":\"TurnBegin\",\"payload\":{}}}\n",
        )
        .unwrap();
        symlink(&wire, dir.join("wire.jsonl")).unwrap();
        let (messages, _, healthy) = parse_session(&dir, "proj", "");
        assert!(healthy);
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].ts, 0);
        let _ = std::fs::remove_dir_all(dir);
        let _ = std::fs::remove_dir_all(outside);
    }

    #[cfg(unix)]
    #[test]
    fn discovery_rejects_symlinked_project_session_and_subagent_dirs() {
        use std::os::unix::fs::symlink;

        let root = session_dir("discovery-root");
        let project = root.join("sessions/project");
        let safe = project.join("safe");
        std::fs::create_dir_all(&safe).unwrap();
        std::fs::write(
            safe.join("context.jsonl"),
            "{\"role\":\"user\",\"content\":\"inside\"}\n",
        )
        .unwrap();
        let outside = root.with_extension("outside");
        let outside_session = outside.join("session");
        let outside_child = outside.join("children/child");
        std::fs::create_dir_all(&outside_session).unwrap();
        std::fs::create_dir_all(&outside_child).unwrap();
        for dir in [&outside_session, &outside_child] {
            std::fs::write(
                dir.join("context.jsonl"),
                "{\"role\":\"user\",\"content\":\"outside\"}\n",
            )
            .unwrap();
        }
        symlink(&outside_session, project.join("linked-session")).unwrap();
        symlink(outside.join("children"), safe.join("subagents")).unwrap();
        symlink(&outside, root.join("sessions/linked-project")).unwrap();

        let (messages, _, healthy) = super::collect_from_root(&root);
        assert!(healthy);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "inside");
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(outside);
    }
}
