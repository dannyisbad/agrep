//! A pass killed after it replaced the published rows but before its source snapshot published
//! leaves an inventory older than the generation: a rollout that pass added is unlisted there. No
//! later pass may take that silence for proof the rollout published nothing once it cannot be read.
//! An inventory no publication outran (none pending, or sealed to the rows) stays the proof it was.
#![cfg(unix)]

mod common;

use common::*;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const CACHE_FILES: [&str; 2] = [".ingest_cache.bin", ".ingest_cache.bin.journal"];
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

fn lose_cache(data: &Path) {
    for name in CACHE_FILES {
        let _ = fs::remove_file(data.join(name));
    }
}

/// Scopes no pass ever published, planted after `data` published: a claude project directory
/// locked (a durable denial) and a codex rollout gone mode 000 (a read that fails behind a good
/// stat). None where the mode bits are ignored.
fn plant_never_published_unreadable(home: &Path) -> Option<(PathBuf, PathBuf)> {
    let project = lock_claude_project(home)?;
    let never_session = "77777777-7777-4777-8777-777777777777";
    let rollout = codex_rollout(home, "07", never_session, 13, &["a prompt never read"]);
    fs::set_permissions(&rollout, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&rollout).is_ok() {
        unlock_dir(&project);
        return None;
    }
    Some((project, rollout))
}

fn release_never_published(scopes: &(PathBuf, PathBuf)) {
    unlock_dir(&scopes.0);
    fs::set_permissions(&scopes.1, fs::Permissions::from_mode(0o644)).unwrap();
}

/// Every churned pass publishes: the trusted inventory proves the never-published scopes empty
/// (the first pass, cold, would refuse otherwise), the churn lands, and the lost cache is rebuilt.
fn assert_publishes_beside_never_published(home: &Path, data: &Path, plan: &[(u32, bool)]) {
    let results = passes(home, data, plan);
    let rebuilt = data.join(".ingest_cache.bin").exists();
    for (context, output, published) in &results {
        assert_published(output, context);
        let minute = context.split(' ').nth(1).unwrap();
        assert!(
            published.contains(&format!("claude churn {minute}")),
            "{context}: churn unpublished"
        );
    }
    assert!(rebuilt, "the lost parse cache was never rebuilt");
}

/// A data dir an earlier build published records no seal. With its parse cache lost beside scopes
/// no pass ever published, nothing is at stake: every pass publishes and the cache is rebuilt.
#[test]
fn an_unsealed_inventory_with_no_publication_pending_still_proves_a_scope_empty() {
    let home = temp_dir("stale-inventory-unsealed-home");
    let data = temp_dir("stale-inventory-unsealed-data");
    copy_dir(&fixture_home("claude"), &home);
    churn_claude(&home, 0);
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    fs::remove_file(data.join(".source_snapshot.seal")).unwrap();
    assert!(!data.join(".ingest_pending.bin").exists());
    let Some(scopes) = plant_never_published_unreadable(&home) else {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    };
    lose_cache(&data);
    assert_publishes_beside_never_published(
        &home,
        &data,
        &[(1, false), (2, false), (3, true), (4, false)],
    );
    release_never_published(&scopes);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A pass killed after its snapshot published but before its seal leaves a current inventory under
/// an older seal. Straight away, or after a quiet pass took the unchanged shortcut and re-sealed it,
/// a lost cache beside never-published scopes costs nothing.
#[test]
fn a_kill_between_the_snapshot_and_its_seal_leaves_the_inventory_trusted() {
    for quiet_pass in [false, true] {
        let home = temp_dir("stale-inventory-seal-kill-home");
        let data = temp_dir("stale-inventory-seal-kill-data");
        copy_dir(&fixture_home("claude"), &home);
        churn_claude(&home, 0);
        assert_published(&ingest_output("all", &home, &data, false), "first index");
        let seal = data.join(".source_snapshot.seal");
        let older = fs::read(&seal).unwrap();
        codex_rollout(&home, "06", ADDED_SESSION, 12, &[ADDED_PROMPT]);
        assert_published(
            &ingest_output("all", &home, &data, false),
            "the pass that added the rollout",
        );
        let current = fs::read(&seal).unwrap();
        assert_ne!(current, older);
        fs::write(&seal, &older).unwrap();
        assert!(!data.join(".ingest_pending.bin").exists());
        if quiet_pass {
            let quiet = ingest_output("all", &home, &data, false);
            assert_published(&quiet, "quiet pass");
            assert!(
                String::from_utf8_lossy(&quiet.stdout).contains("unchanged since last index"),
                "the quiet pass did not take the shortcut"
            );
            assert_eq!(
                fs::read(&seal).unwrap(),
                current,
                "the shortcut did not re-seal the current snapshot"
            );
        }
        let Some(scopes) = plant_never_published_unreadable(&home) else {
            let _ = fs::remove_dir_all(&home);
            let _ = fs::remove_dir_all(&data);
            return;
        };
        lose_cache(&data);
        assert_publishes_beside_never_published(
            &home,
            &data,
            &[(1, false), (2, false), (3, true), (4, false)],
        );
        assert!(
            normalize(&data).contains(ADDED_PROMPT),
            "quiet pass {quiet_pass}: the readable rollout's rows were dropped"
        );
        release_never_published(&scopes);
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// Index `home` into `data`, add a rollout, then stream it with `--emit-rows`: that pass replaces
/// the published rows and commits its cache but publishes no snapshot, leaving the published bytes
/// as its pending residue. Returns the rollout.
fn emitted_after_adding_a_rollout(home: &Path, data: &Path) -> PathBuf {
    copy_dir(&fixture_home("claude"), home);
    churn_claude(home, 0);
    assert_published(&ingest_output("all", home, data, false), "first index");
    let snapshot = fs::read(data.join(".source_snapshot.bin")).unwrap();
    let rollout = codex_rollout(home, "06", ADDED_SESSION, 12, &[ADDED_PROMPT]);
    assert_published(&ingest_emit_output("all", home, data), "--emit-rows pass");
    assert!(normalize(data).contains(ADDED_PROMPT));
    assert_eq!(
        fs::read(data.join(".source_snapshot.bin")).unwrap(),
        snapshot
    );
    assert_eq!(
        fs::read(data.join(".ingest_pending.bin")).unwrap(),
        snapshot
    );
    rollout
}

/// The residue is the only sign the inventory is older than the rows the `--emit-rows` pass
/// published. With the cache lost and the rollout's day directory locked, no pass may take the
/// inventory's silence for proof: each keeps the rows or refuses naming the directory.
#[test]
fn a_cacheless_pass_after_an_emit_rows_index_keeps_the_rollout_behind_a_locked_day() {
    let home = temp_dir("stale-inventory-emit-locked-home");
    let data = temp_dir("stale-inventory-emit-locked-data");
    let rollout = emitted_after_adding_a_rollout(&home, &data);
    lose_cache(&data);
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

/// The one state in which an unchanged pass retires an `--emit-rows` residue over rows the
/// inventory never listed: the rollout's day directory was already denied when the inventory was
/// taken, opened for the stream, then denied again. The inventory is blind there, so retiring the
/// residue licenses nothing: with the cache lost, no pass drops the rows without refusing.
#[test]
fn retiring_an_emit_rows_residue_over_a_blind_directory_keeps_its_rows() {
    let home = temp_dir("stale-inventory-emit-blind-home");
    let data = temp_dir("stale-inventory-emit-blind-data");
    copy_dir(&fixture_home("claude"), &home);
    churn_claude(&home, 0);
    let day = join_native(&home, ".codex/sessions/2026/01/06");
    fs::create_dir_all(&day).unwrap();
    let Some(locked) = lock_dir(&day) else {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    };
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    unlock_dir(&locked);
    let rollout = codex_rollout(&home, "06", ADDED_SESSION, 12, &[ADDED_PROMPT]);
    assert_published(&ingest_emit_output("all", &home, &data), "--emit-rows pass");
    assert!(normalize(&data).contains(ADDED_PROMPT));
    assert!(data.join(".ingest_pending.bin").exists());
    lock_dir(&day).unwrap();
    let retiring = ingest_output("all", &home, &data, false);
    assert_published(&retiring, "the pass after the stream");
    let shortcut = String::from_utf8_lossy(&retiring.stdout).contains("unchanged since last index");
    assert!(
        normalize(&data).contains(ADDED_PROMPT),
        "the pass after the stream dropped the rollout's rows"
    );
    lose_cache(&data);
    let results = passes(&home, &data, &[(1, false), (2, true), (3, false)]);
    unlock_dir(&day);
    for (context, output, published) in &results {
        assert_kept_or_refused(
            output,
            published,
            &day,
            &format!("{context} (shortcut {shortcut})"),
        );
    }
    assert!(rollout.exists());
    assert_heals(&home, &data, 4);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}
