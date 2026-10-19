//! An event-only source (tool calls, no prompt row) publishes nothing but its event stream, and
//! the parse cache holds its event ownership, never its payload. Behind a locked directory, every
//! lane must keep that stream or refuse naming the directory: with the cache intact its ownership
//! is served, with the cache lost the published generation is retained until the source is
//! readable. Once it is, a quiet pass pair or `--full` heals; the event bytes exist only in the
//! event store and the source, so nothing can rebuild them while both are gone.
#![cfg(unix)]

mod common;

use common::*;
use std::fs;
use std::path::{Path, PathBuf};

const CHAT_SESSION: &str = "22222222-2222-4222-8222-222222222222";
const EVENT_SESSION: &str = "99999999-9999-4999-8999-999999999999";
const EVENT_CALL: &str = "call_event_only_7f1";

fn churn_claude(home: &Path, minute: u32) -> String {
    let chat = home
        .join(".claude")
        .join("projects")
        .join("proj-beta")
        .join(format!("{CHAT_SESSION}.jsonl"));
    fs::create_dir_all(chat.parent().unwrap()).unwrap();
    let text = format!("claude churn {minute}");
    let row = serde_json::json!({
        "type": "user", "userType": "external", "sessionId": CHAT_SESSION,
        "timestamp": format!("2026-01-03T10:{minute:02}:00.000Z"), "cwd": "/work/beta",
        "message": {"role": "user", "content": text},
    });
    let mut body = fs::read_to_string(&chat).unwrap_or_default();
    body.push_str(&format!("{row}\n"));
    fs::write(&chat, body).unwrap();
    text
}

fn event_only_rollout(home: &Path) -> PathBuf {
    let dir = join_native(home, ".codex/sessions/2026/01/06");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!("rollout-2026-01-06T12-00-00-{EVENT_SESSION}.jsonl"));
    let ts = "2026-01-06T12:30:00.000Z";
    let lines = [
        serde_json::json!({"type": "session_meta",
                           "payload": {"id": EVENT_SESSION, "cwd": "/work/split", "source": "cli"}}),
        serde_json::json!({"type": "turn_context", "payload": {"model": "gpt-5.5-codex"}}),
        serde_json::json!({"type": "response_item", "timestamp": ts,
                           "payload": {"type": "function_call", "name": "shell",
                                       "call_id": EVENT_CALL,
                                       "arguments": "{\"command\":[\"ls\"]}"}}),
        serde_json::json!({"type": "response_item", "timestamp": ts,
                           "payload": {"type": "function_call_output", "call_id": EVENT_CALL,
                                       "output": "ran ls"}}),
    ];
    let body: String = lines.iter().map(|line| format!("{line}\n")).collect();
    fs::write(&path, body).unwrap();
    path
}

fn holds_event(data: &Path) -> bool {
    event_rows(data)
        .iter()
        .any(|(_, body)| String::from_utf8_lossy(body).contains(EVENT_CALL))
}

#[derive(Clone, Copy, Debug)]
enum Cache {
    Intact,
    Lost,
}

#[derive(Clone, Copy, Debug)]
enum Store {
    Intact,
    ProofsLost,
    Corrupt,
}

#[derive(Clone, Copy, Debug)]
enum Lane {
    Warm,
    Full,
}

fn remove_proofs(data: &Path) {
    for entry in fs::read_dir(data).unwrap().flatten() {
        if entry
            .file_name()
            .to_string_lossy()
            .starts_with(".events_complete")
        {
            fs::remove_file(entry.path()).unwrap();
        }
    }
}

fn damage(data: &Path, cache: Cache, store: Store) {
    if let Cache::Lost = cache {
        fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
        let _ = fs::remove_file(data.join(".ingest_cache.bin.journal"));
    }
    match store {
        Store::Intact => {}
        Store::ProofsLost => remove_proofs(data),
        Store::Corrupt => {
            remove_proofs(data);
            let path = data
                .join("events")
                .join(agrep_core::cache::EVENT_STORE_NAME);
            let mut bytes = fs::read(&path).unwrap();
            assert!(bytes.len() > 4096 + 512, "event store too small to corrupt");
            bytes[4096..4096 + 512].fill(0xff);
            fs::write(&path, bytes).unwrap();
        }
    }
}

fn cleanup(home: &Path, data: &Path) {
    let _ = fs::remove_dir_all(home);
    let _ = fs::remove_dir_all(data);
}

/// Every cell of the matrix, each in its own home: the day locked after the damage, two churned
/// passes in the lane, then the day readable again and the recovery the lane documents.
#[test]
fn an_event_only_source_behind_a_locked_day_is_kept_or_refused_in_every_lane() {
    for cache in [Cache::Intact, Cache::Lost] {
        for store in [Store::Intact, Store::ProofsLost, Store::Corrupt] {
            for lane in [Lane::Warm, Lane::Full] {
                let tag = format!("cache={cache:?} store={store:?} lane={lane:?}");
                let home = temp_dir("event-only-home");
                let data = temp_dir("event-only-data");
                copy_dir(&fixture_home("claude"), &home);
                churn_claude(&home, 0);
                let rollout = event_only_rollout(&home);
                ingest_into("all", &home, &data, false);
                assert!(holds_event(&data), "{tag}: the event never published");
                damage(&data, cache, store);
                let Some(locked) = lock_dir(rollout.parent().unwrap()) else {
                    cleanup(&home, &data);
                    return;
                };
                let plan = match lane {
                    Lane::Warm => [(1, false), (2, false)],
                    Lane::Full => [(1, true), (2, false)],
                };
                let mut results = Vec::new();
                for (minute, full) in plan {
                    let churn = churn_claude(&home, minute);
                    let output = ingest_output("all", &home, &data, full);
                    results.push((minute, churn, output, holds_event(&data)));
                }
                unlock_dir(&locked);
                for (minute, churn, output, kept) in &results {
                    let stderr = String::from_utf8_lossy(&output.stderr);
                    let context = format!("{tag} pass {minute}");
                    if output.status.success() {
                        assert!(
                            *kept && normalize(&data).contains(churn),
                            "{context}: published without the event or the churn\nstderr:\n{stderr}"
                        );
                    } else {
                        assert!(
                            stderr.contains(locked.to_string_lossy().as_ref())
                                || stderr.contains("is corrupt"),
                            "{context}: the refusal names neither the day nor the store\n{stderr}"
                        );
                    }
                    if let (Cache::Intact, Store::Intact | Store::ProofsLost) = (cache, store) {
                        assert!(
                            output.status.success(),
                            "{context}: a served cache refused\nstderr:\n{stderr}"
                        );
                    }
                }
                // Readable again: a churned pass may still ask for a second stable snapshot,
                // which the quiet pass supplies; `--full` needs neither.
                churn_claude(&home, 8);
                let churned = ingest_output("all", &home, &data, false);
                let healed = if churned.status.success() {
                    churned
                } else {
                    let quiet = ingest_output("all", &home, &data, false);
                    if quiet.status.success() {
                        quiet
                    } else {
                        ingest_output("all", &home, &data, true)
                    }
                };
                assert!(
                    healed.status.success(),
                    "{tag}: never healed\nstderr:\n{}",
                    String::from_utf8_lossy(&healed.stderr)
                );
                assert!(
                    holds_event(&data) && normalize(&data).contains("claude churn 8"),
                    "{tag}: healed without the event or the churn"
                );
                cleanup(&home, &data);
            }
        }
    }
}
