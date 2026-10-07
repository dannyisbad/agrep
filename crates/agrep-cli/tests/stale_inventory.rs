//! A pass killed after it replaced the published rows but before its source snapshot published
//! leaves an inventory older than the generation: a rollout that pass added is unlisted there. No
//! later pass may take that silence for proof the rollout published nothing once it cannot be read.
#![cfg(unix)]

mod common;

use common::*;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const CHAT_SESSION: &str = "22222222-2222-4222-8222-222222222222";
const ADDED_SESSION: &str = "66666666-6666-4666-8666-666666666666";
const ADDED_PROMPT: &str = "a prompt only the killed pass published";

/// Append a turn to a claude chat of its own and return its text.
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

fn assert_published(output: &std::process::Output, context: &str) {
    assert!(
        output.status.success(),
        "{context}: exit {:?}\nstderr:\n{}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Index `home` into `data`, add a rollout, index again, then leave `data` as a kill between that
/// pass's derived writes and its source snapshot does: its rows published, the inventory and the
/// seal binding it still the previous pass's, its preflight still the pending marker.
fn killed_after_adding_a_rollout(home: &Path, data: &Path) -> PathBuf {
    copy_dir(&fixture_home("claude"), home);
    churn_claude(home, 0);
    assert_published(&ingest_output("all", home, data, false), "first index");
    let after_snapshot = [".source_snapshot.bin", ".source_snapshot.seal"];
    let previous = after_snapshot.map(|name| fs::read(data.join(name)).ok());
    let rollout = codex_rollout(home, "06", ADDED_SESSION, 12, &[ADDED_PROMPT]);
    assert_published(
        &ingest_output("all", home, data, false),
        "the pass that added the rollout",
    );
    assert!(normalize(data).contains(ADDED_PROMPT));
    let published = fs::read(data.join(".source_snapshot.bin")).unwrap();
    assert_ne!(
        Some(&published),
        previous[0].as_ref(),
        "the added rollout left the inventory unchanged"
    );
    assert!(!data.join(".ingest_pending.bin").exists());
    fs::write(data.join(".ingest_pending.bin"), published).unwrap();
    for (name, bytes) in after_snapshot.iter().zip(previous) {
        match bytes {
            Some(bytes) => fs::write(data.join(name), bytes).unwrap(),
            None => {
                let _ = fs::remove_file(data.join(name));
            }
        }
    }
    rollout
}

/// A pass keeps the rollout's rows, or refuses naming its store; never exit 0 without them.
fn assert_kept_or_refused(
    output: &std::process::Output,
    published: &str,
    store: &Path,
    context: &str,
) {
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        published.contains(ADDED_PROMPT),
        "{context}: dropped the rollout's published rows (exit {:?})\nstderr:\n{stderr}",
        output.status.code()
    );
    if !output.status.success() {
        let store = store.to_string_lossy();
        assert!(
            stderr.contains(store.as_ref()),
            "{context}: the refusal names no store\nstderr:\n{stderr}"
        );
    }
}

/// Churned passes, plain or `--full`, each with its output and what it left published.
fn passes(
    home: &Path,
    data: &Path,
    plan: &[(u32, bool)],
) -> Vec<(String, std::process::Output, String)> {
    plan.iter()
        .map(|&(minute, full)| {
            churn_claude(home, minute);
            let output = ingest_output("all", home, data, full);
            let context = format!("pass {minute}{}", if full { " (--full)" } else { "" });
            (context, output, normalize(data))
        })
        .collect()
}

fn assert_heals(home: &Path, data: &Path, minute: u32) {
    let churn = churn_claude(home, minute);
    assert_published(&ingest_output("all", home, data, false), "healed pass");
    let published = normalize(data);
    assert!(
        published.contains(ADDED_PROMPT) && published.contains(&churn),
        "the healed pass dropped the rollout's rows or froze"
    );
}

/// The rollout goes mode 000 while the parse cache still holds its rows: `--full` starts cold,
/// and the stale inventory, which never listed the rollout, must not vouch that it held nothing.
#[test]
fn full_after_a_kill_before_its_snapshot_keeps_the_rollout_it_published_once_unreadable() {
    let home = temp_dir("stale-inventory-file-home");
    let data = temp_dir("stale-inventory-file-data");
    let rollout = killed_after_adding_a_rollout(&home, &data);
    fs::set_permissions(&rollout, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&rollout).is_ok() {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    let day = rollout.parent().unwrap().to_path_buf();
    let results = passes(&home, &data, &[(1, true), (2, false), (3, true)]);
    fs::set_permissions(&rollout, fs::Permissions::from_mode(0o644)).unwrap();
    for (context, output, published) in &results {
        assert_kept_or_refused(output, published, &day, context);
    }
    assert_heals(&home, &data, 4);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The parse cache is lost and the rollout's day directory locked: no cache serves its rows, and
/// the stale inventory lists nothing under the directory. Plain passes and `--full` alike must
/// keep the rows or refuse.
#[test]
fn a_cacheless_pass_after_a_kill_before_its_snapshot_keeps_the_rollout_behind_a_locked_day() {
    let home = temp_dir("stale-inventory-locked-home");
    let data = temp_dir("stale-inventory-locked-data");
    let rollout = killed_after_adding_a_rollout(&home, &data);
    let _ = fs::remove_file(data.join(".ingest_cache.bin.journal"));
    fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
    let Some(locked) = lock_dir(rollout.parent().unwrap()) else {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    };
    let results = passes(&home, &data, &[(1, false), (2, true), (3, false)]);
    unlock_dir(&locked);
    for (context, output, published) in &results {
        assert_kept_or_refused(output, published, &locked, context);
    }
    assert_heals(&home, &data, 4);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}
