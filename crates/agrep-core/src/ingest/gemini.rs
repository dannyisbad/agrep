//! Gemini CLI adapter: ~/.gemini/tmp/<projectHash>/chats/session-*.json
//!
//! The fixture-backed store contract uses one JSON object per session:
//!   { sessionId, projectHash, startTime, lastUpdated, messages: [ ... ] }
//! each message is `{ id, timestamp (RFC3339), type, content, ... }`:
//!   type=="user"   -> the human's typed prompt (content is a string)
//!   type=="gemini" -> the model's turn: `content` prose reply, `model`, `toolCalls[]`,
//!                     plus `thoughts` (reasoning, excluded) and `tokens`
//!   type=="info"   -> CLI system notices (auth flow, etc.) - not the user, skipped
//! a toolCall is `{ id, name, args, result: [{functionResponse:{response:{output}}}],
//! status }`. projectHash is a SHA256 of the project dir with no reverse map in the store,
//! so project attribution falls back to "gemini" (see collect).

use std::fs;
use std::path::Path;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::ingest::{cap_event_output, is_wrapper, summarize_tool_input_with_chars};
use crate::model::{Event, Message};

/// A toolCall's captured output + outcome: `result[*].functionResponse.response.output`,
/// with `status` giving the ok signal when the store recorded one.
fn tool_result(tc: &serde_json::Value) -> (String, usize, usize, Option<bool>) {
    let mut out = String::new();
    if let Some(arr) = tc.get("result").and_then(|r| r.as_array()) {
        for r in arr {
            if let Some(o) = r
                .get("functionResponse")
                .and_then(|f| f.get("response"))
                .and_then(|r| r.get("output"))
                .and_then(|o| o.as_str())
            {
                if !out.is_empty() {
                    out.push('\n');
                }
                out.push_str(o);
            }
        }
    }
    let ok = match tc.get("status").and_then(|s| s.as_str()) {
        Some("success") => Some(true),
        Some("error") | Some("failed") => Some(false),
        _ => None,
    };
    let (output, output_chars, output_bytes) = cap_event_output(&out);
    (output, output_chars, output_bytes, ok)
}

fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    // seen = elements of the session's messages array
    let tally = crate::intake::file("gemini", path);
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
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Skipped,
            );
        }
    };
    let root: serde_json::Value = match serde_json::from_str(&data) {
        Ok(v) => v,
        Err(e) => {
            // whole-file failure: the file itself is the one seen-and-errored record
            tally.seen();
            tally.error(&format!("{e}: {}", crate::intake::clip(&data, 80)));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Complete,
            );
        }
    };
    let session = root
        .get("sessionId")
        .and_then(|s| s.as_str())
        .map(str::to_string)
        .unwrap_or_else(|| {
            path.file_stem()
                .map(|s| s.to_string_lossy().to_string())
                .unwrap_or_default()
        });
    let messages = match root.get("messages").and_then(|m| m.as_array()) {
        Some(m) => m,
        None => {
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Complete,
            )
        }
    };

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut turn = 0u32;
    for (message_ordinal, m) in messages.iter().enumerate() {
        tally.seen();
        let ty = m.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let ts = parse_timestamp::rfc3339(m.get("timestamp").and_then(|t| t.as_str()));
        match ty {
            "user" => {
                let text = m.get("content").and_then(|c| c.as_str()).unwrap_or("");
                if text.trim().is_empty() {
                    tally.skip(crate::intake::Skip::EmptyText);
                    continue;
                }
                if is_wrapper(text) {
                    tally.skip(crate::intake::Skip::Wrapper);
                    continue;
                }
                tally.row();
                out.push(crate::model::RawMessage {
                    agent: "gemini",
                    project: "gemini".to_string(),
                    session: session.clone(),
                    ts,
                    turn,
                    text: text.to_string(),
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
                if let Some(txt) = m.get("content").and_then(|c| c.as_str()) {
                    if !txt.trim().is_empty() {
                        if let Some(last) = out.last_mut() {
                            let chars = crate::ingest::append_capped(
                                &mut last.reply,
                                txt,
                                crate::ingest::REPLY_CAP,
                            );
                            last.reply_chars += chars;
                        }
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
                            session: session.clone(),
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
            "info" => tally.skip(crate::intake::Skip::Meta),
            _ => tally.skip(crate::intake::Skip::NonHuman),
        }
    }
    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        events,
        crate::ingest_cache::ReadOutcome::Complete,
    )
}

/// Walk every `~/.gemini/tmp/<projectHash>/chats/session-*.json` and collect the user's
/// Gemini CLI turns + tool events. Project attribution is the constant "gemini": the store
/// keys sessions by an opaque SHA256 projectHash with no path recorded, so unlike the other
/// adapters there is no readable working dir to bucket by (a hash->path map is a follow-up).
fn session_files(root: &Path) -> Vec<std::path::PathBuf> {
    let mut files: Vec<std::path::PathBuf> = Vec::new();
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("gemini", root, "source root is not a plain directory");
            return files;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return files,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            return files;
        }
    }
    let dirs = match fs::read_dir(root) {
        Ok(dirs) => dirs,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            return files;
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
            if name.starts_with("session-") && name.ends_with(".json") {
                files.push(path);
            }
        }
    }
    files
}

pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".gemini").join("tmp");
    let files = session_files(&root);
    let pass = crate::ingest_cache::collect_cached_for(cache, "gemini", &root, &files, parse_file);
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
        // chats/session-*.json is the conversation; logs.json is telemetry
        path.file_name()
            .and_then(|x| x.to_str())
            .is_some_and(|n| n.starts_with("session-") && n.ends_with(".json"))
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::session_files;
    use std::os::unix::fs::symlink;

    #[test]
    fn discovery_rejects_symlinked_projects_chats_and_sessions() {
        let root = std::env::temp_dir().join(format!(
            "agrep-gemini-links-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
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

        let files = session_files(&root);
        assert_eq!(files, vec![root.join("safe/chats/session-safe.json")]);
        let _ = std::fs::remove_dir_all(root);
        let _ = std::fs::remove_dir_all(outside);
    }
}
