//! The parse cache is a base plus a journal of frames; a pass commits every row it adds through
//! the journal until the next base rewrite. Losing only the journal leaves a base that decodes
//! cleanly but is older than the generation it fed: rows published from the lost frames are in
//! messages.jsonl and in no cache entry. Once their source is unreadable, no pass may take that
//! base for the last-good inventory and publish without them.
#![cfg(unix)]

mod common;

use common::*;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const CHAT_SESSION: &str = "22222222-2222-4222-8222-222222222222";
const ADDED_SESSION: &str = "66666666-6666-4666-8666-666666666666";
const SIBLING_SESSION: &str = "55555555-5555-4555-8555-555555555555";
const ADDED_PROMPT: &str = "a prompt only the lost journal ever cached";
const SIBLING_PROMPT: &str = "a sibling prompt the base still holds";
const EVENT_CALL: &str = "call_journal_loss_7f1";

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

/// A codex rollout of `session` under day 06 holding one tool call and no prompt: an event-only
/// source, whose only published material is its event stream.
fn event_only_rollout(home: &Path, session: &str) -> PathBuf {
    let dir = join_native(home, ".codex/sessions/2026/01/06");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!("rollout-2026-01-06T12-00-00-{session}.jsonl"));
    let ts = "2026-01-06T12:30:00.000Z";
    let lines = [
        serde_json::json!({"type": "session_meta",
                           "payload": {"id": session, "cwd": "/work/split", "source": "cli"}}),
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

fn assert_published(output: &std::process::Output, context: &str) {
    assert!(
        output.status.success(),
        "{context}: exit {:?}\nstderr:\n{}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// What the lost journal alone cached: a prompt row, or an event-only rollout's event stream.
#[derive(Clone, Copy)]
enum Added {
    Prompt,
    EventOnly,
}

/// Whether `data` still publishes what the lost journal cached.
fn holds_added(data: &Path, added: Added) -> bool {
    match added {
        Added::Prompt => normalize(data).contains(ADDED_PROMPT),
        Added::EventOnly => event_rows(data)
            .iter()
            .any(|(_, body)| String::from_utf8_lossy(body).contains(EVENT_CALL)),
    }
}

/// Index `home` into `data`, add a rollout (beside a sibling when `sibling`), index again, then
/// delete the journal alone: the base is valid and one commit behind the published rows.
fn lose_journal_after_adding(home: &Path, data: &Path, added: Added, sibling: bool) -> PathBuf {
    copy_dir(&fixture_home("claude"), home);
    churn_claude(home, 0);
    if sibling {
        codex_rollout(home, "06", SIBLING_SESSION, 11, &[SIBLING_PROMPT]);
    }
    assert_published(&ingest_output("all", home, data, false), "first index");
    let rollout = match added {
        Added::Prompt => codex_rollout(home, "06", ADDED_SESSION, 12, &[ADDED_PROMPT]),
        Added::EventOnly => event_only_rollout(home, ADDED_SESSION),
    };
    assert_published(
        &ingest_output("all", home, data, false),
        "the pass that added the rollout",
    );
    assert!(holds_added(data, added), "the rollout never published");
    let journal = data.join(".ingest_cache.bin.journal");
    assert!(
        fs::metadata(&journal).is_ok_and(|metadata| metadata.len() > 0),
        "the second pass did not journal its rows"
    );
    fs::remove_file(journal).unwrap();
    rollout
}

/// Every pass keeps the rollout's material, or refuses naming `scope`; never exit 0 without it.
fn assert_kept_or_refused(output: &std::process::Output, kept: bool, scope: &Path, context: &str) {
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        kept,
        "{context}: dropped what the lost journal cached (exit {:?})\nstderr:\n{stderr}",
        output.status.code()
    );
    if !output.status.success() {
        let scope = scope.to_string_lossy();
        assert!(
            stderr.contains(scope.as_ref()),
            "{context}: the refusal names no scope\nstderr:\n{stderr}"
        );
    }
}

fn assert_heals(home: &Path, data: &Path, added: Added, minute: u32) {
    let churn = churn_claude(home, minute);
    assert_published(&ingest_output("all", home, data, false), "healed pass");
    assert!(
        holds_added(data, added) && normalize(data).contains(&churn),
        "the healed pass dropped the rollout or froze"
    );
}

/// Churned passes against an unreadable `scope`, then the scope readable again: each pass keeps
/// the rollout's material or refuses, and the next pass heals.
fn run_plan(
    home: &Path,
    data: &Path,
    added: Added,
    scope: &Path,
    plan: &[(u32, bool)],
    unlock: impl FnOnce(),
) {
    let results: Vec<_> = plan
        .iter()
        .map(|&(minute, full)| {
            churn_claude(home, minute);
            let output = ingest_output("all", home, data, full);
            let context = format!("pass {minute}{}", if full { " (--full)" } else { "" });
            (context, output, holds_added(data, added))
        })
        .collect();
    unlock();
    for (context, output, kept) in &results {
        assert_kept_or_refused(output, *kept, scope, context);
    }
    assert_heals(home, data, added, 9);
}

fn cleanup(home: &Path, data: &Path) {
    let _ = fs::remove_dir_all(home);
    let _ = fs::remove_dir_all(data);
}

/// The rollout's day directory locked after the journal loss, nothing else of codex cached:
/// plain passes, then `--full`, and `--full` first.
#[test]
fn a_journal_only_loss_behind_a_locked_day_keeps_the_rollout_or_refuses() {
    for plan in [
        &[(1, false), (2, false), (3, true)][..],
        &[(1, true), (2, false)][..],
    ] {
        let home = temp_dir("journal-loss-day-home");
        let data = temp_dir("journal-loss-day-data");
        let rollout = lose_journal_after_adding(&home, &data, Added::Prompt, false);
        let Some(locked) = lock_dir(rollout.parent().unwrap()) else {
            cleanup(&home, &data);
            return;
        };
        run_plan(&home, &data, Added::Prompt, &locked, plan, || {
            unlock_dir(&locked)
        });
        cleanup(&home, &data);
    }
}

/// The rollout itself goes mode 000 while its day directory lists it: the preflight is complete
/// and the stale base holds no entry to serve, so the failed read must refuse, not publish.
#[test]
fn a_journal_only_loss_with_the_rollout_unreadable_keeps_it_or_refuses() {
    for plan in [
        &[(1, false), (2, false), (3, true)][..],
        &[(1, true), (2, false)][..],
    ] {
        let home = temp_dir("journal-loss-file-home");
        let data = temp_dir("journal-loss-file-data");
        let rollout = lose_journal_after_adding(&home, &data, Added::Prompt, false);
        fs::set_permissions(&rollout, fs::Permissions::from_mode(0o000)).unwrap();
        if fs::read(&rollout).is_ok() {
            cleanup(&home, &data);
            return;
        }
        run_plan(&home, &data, Added::Prompt, &rollout, plan, || {
            fs::set_permissions(&rollout, fs::Permissions::from_mode(0o644)).unwrap();
        });
        cleanup(&home, &data);
    }
}

/// The stale base still holds a sibling under the locked day, so the root is not empty and the
/// cache serves that sibling: the rollout it never cached must still not go silently.
#[test]
fn a_journal_only_loss_beside_a_cached_sibling_keeps_the_rollout_or_refuses() {
    for plan in [
        &[(1, false), (2, false), (3, true)][..],
        &[(1, true), (2, false)][..],
    ] {
        let home = temp_dir("journal-loss-sibling-home");
        let data = temp_dir("journal-loss-sibling-data");
        let rollout = lose_journal_after_adding(&home, &data, Added::Prompt, true);
        let Some(locked) = lock_dir(rollout.parent().unwrap()) else {
            cleanup(&home, &data);
            return;
        };
        run_plan(&home, &data, Added::Prompt, &locked, plan, || {
            unlock_dir(&locked)
        });
        assert!(normalize(&data).contains(SIBLING_PROMPT));
        cleanup(&home, &data);
    }
}

/// An event-only rollout has no prompt row to match: its event stream alone is what the lost
/// journal cached, and the published generation still holds it.
#[test]
fn a_journal_only_loss_of_an_event_only_rollout_keeps_its_events_or_refuses() {
    for plan in [
        &[(1, false), (2, false), (3, true)][..],
        &[(1, true), (2, false)][..],
    ] {
        let home = temp_dir("journal-loss-events-home");
        let data = temp_dir("journal-loss-events-data");
        let rollout = lose_journal_after_adding(&home, &data, Added::EventOnly, false);
        let Some(locked) = lock_dir(rollout.parent().unwrap()) else {
            cleanup(&home, &data);
            return;
        };
        run_plan(&home, &data, Added::EventOnly, &locked, plan, || {
            unlock_dir(&locked)
        });
        cleanup(&home, &data);
    }
}

/// With every source readable, the lost frames cost one reparse: the next pass discloses the
/// stale journal, rebuilds the cache, and publishes every row, churn included.
#[test]
fn a_journal_only_loss_with_readable_sources_rebuilds_the_cache_in_one_pass() {
    let home = temp_dir("journal-loss-readable-home");
    let data = temp_dir("journal-loss-readable-data");
    lose_journal_after_adding(&home, &data, Added::Prompt, false);
    let churn = churn_claude(&home, 1);
    let output = ingest_output("all", &home, &data, false);
    assert_published(&output, "the pass after the journal loss");
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("behind its last commit"),
        "the stale journal was not disclosed:\n{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let published = normalize(&data);
    assert!(published.contains(ADDED_PROMPT) && published.contains(&churn));
    assert!(data.join(".ingest_cache.bin.head").exists());
    let quiet = ingest_output("all", &home, &data, false);
    assert_published(&quiet, "the quiet pass after the rebuild");
    assert!(
        String::from_utf8_lossy(&quiet.stdout).contains("unchanged since last index"),
        "the rebuilt cache did not settle"
    );
    cleanup(&home, &data);
}

/// How the journal becomes unreadable rather than absent. A writer holds the index lock, so
/// none of these is a race: the frames are as lost as a deleted journal's.
#[derive(Clone, Copy, Debug)]
enum Damage {
    Mode000,
    Symlink,
    Directory,
}

/// Damage the journal in place; None where the mode bits are ignored (a privileged runner).
fn damage_journal(data: &Path, damage: Damage) -> Option<()> {
    let journal = data.join(".ingest_cache.bin.journal");
    match damage {
        Damage::Mode000 => {
            fs::set_permissions(&journal, fs::Permissions::from_mode(0o000)).unwrap();
            if fs::read(&journal).is_ok() {
                return None;
            }
        }
        Damage::Symlink => {
            let copy = data.join("journal-copy");
            fs::rename(&journal, &copy).unwrap();
            std::os::unix::fs::symlink(&copy, &journal).unwrap();
        }
        Damage::Directory => {
            fs::remove_file(&journal).unwrap();
            fs::create_dir(&journal).unwrap();
            fs::write(journal.join("keep.txt"), b"user content").unwrap();
        }
    }
    Some(())
}

/// A whole-store (cline) or token (crush) agent's rows that only a journal frame cached, the
/// journal then unreadable in place and the store locked: every lane keeps the rows or refuses
/// naming the store, and the rows return once it is readable.
#[test]
fn an_unreadable_journal_keeps_whole_store_and_token_rows_or_refuses() {
    const CLINE_TEXT: &str = "a cline task only the journal cached";
    const CRUSH_TEXT: &str = "a crush chat only the journal cached";
    for adapter in ["cline", "crush"] {
        for damage in [Damage::Mode000, Damage::Symlink, Damage::Directory] {
            for plan in [
                &[(1, false), (2, false), (3, true)][..],
                &[(1, true), (2, false)][..],
            ] {
                let tag = format!("{adapter} {damage:?} {plan:?}");
                let home = temp_dir("journal-unreadable-home");
                let data = temp_dir("journal-unreadable-data");
                copy_dir(&fixture_home("claude"), &home);
                churn_claude(&home, 0);
                let (store, text) = match adapter {
                    "cline" => {
                        let root = home.join(".cline").join("data");
                        cline_task(&root, "1767348000000", "a cline task the base holds");
                        (root.join("tasks"), CLINE_TEXT)
                    }
                    _ => {
                        let db = join_native(&home, ".local/share/crush/crush.db");
                        crush_db(&db, "sa", "a crush chat the base holds");
                        (db, CRUSH_TEXT)
                    }
                };
                assert_published(&ingest_output("all", &home, &data, false), &tag);
                match adapter {
                    "cline" => cline_task(&home.join(".cline").join("data"), "1767348900000", text),
                    _ => crush_db(&store, "sb", text),
                }
                assert_published(&ingest_output("all", &home, &data, false), &tag);
                assert!(normalize(&data).contains(text), "{tag}: never published");
                if damage_journal(&data, damage).is_none() {
                    cleanup(&home, &data);
                    return;
                }
                // Lock the store: a directory via its mode, the database via its own.
                let locked = if store.is_dir() {
                    lock_dir(&store)
                } else {
                    fs::set_permissions(&store, fs::Permissions::from_mode(0o000)).unwrap();
                    fs::read(&store).is_err().then(|| store.clone())
                };
                let Some(locked) = locked else {
                    cleanup(&home, &data);
                    return;
                };
                let results: Vec<_> = plan
                    .iter()
                    .map(|&(minute, full)| {
                        churn_claude(&home, minute);
                        let output = ingest_output("all", &home, &data, full);
                        (minute, output, normalize(&data).contains(text))
                    })
                    .collect();
                if store.is_dir() {
                    unlock_dir(&locked);
                } else {
                    fs::set_permissions(&store, fs::Permissions::from_mode(0o644)).unwrap();
                }
                for (minute, output, kept) in &results {
                    let stderr = String::from_utf8_lossy(&output.stderr);
                    assert!(
                        kept,
                        "{tag} pass {minute}: dropped the rows (exit {:?})\nstderr:\n{stderr}",
                        output.status.code()
                    );
                    assert!(
                        output.status.success()
                            || stderr.contains(locked.to_string_lossy().as_ref()),
                        "{tag} pass {minute}: the refusal names no store\nstderr:\n{stderr}"
                    );
                }
                let churn = churn_claude(&home, 9);
                let healed = ingest_output("all", &home, &data, false);
                assert_published(&healed, &format!("{tag} healed"));
                let published = normalize(&data);
                assert!(
                    published.contains(text) && published.contains(&churn),
                    "{tag}: the healed pass dropped the rows or froze"
                );
                cleanup(&home, &data);
            }
        }
    }
}

/// A directory in the journal's place, every source readable: the next plain pass rebuilds the
/// cache, moves the directory aside untouched, and settles; no pass errors until `--full`.
#[test]
fn a_directory_in_the_journals_place_is_moved_aside_and_the_cache_rebuilt() {
    let home = temp_dir("journal-directory-home");
    let data = temp_dir("journal-directory-data");
    lose_journal_after_adding(&home, &data, Added::Prompt, false);
    let journal = data.join(".ingest_cache.bin.journal");
    fs::create_dir(&journal).unwrap();
    fs::write(journal.join("keep.txt"), b"user content").unwrap();
    for minute in [1, 2] {
        let churn = churn_claude(&home, minute);
        let output = ingest_output("all", &home, &data, false);
        assert_published(
            &output,
            &format!("plain pass {minute} beside a directory journal"),
        );
        let published = normalize(&data);
        assert!(published.contains(ADDED_PROMPT) && published.contains(&churn));
    }
    assert!(
        fs::symlink_metadata(&journal).is_ok_and(|metadata| metadata.is_file()),
        "the journal is not a regular file again"
    );
    let aside: Vec<_> = fs::read_dir(&data)
        .unwrap()
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .is_some_and(|name| name.to_string_lossy().contains(".journal.displaced-"))
        })
        .collect();
    assert_eq!(
        aside.len(),
        1,
        "the directory was not moved aside exactly once"
    );
    assert_eq!(
        fs::read(aside[0].join("keep.txt")).unwrap(),
        b"user content"
    );
    let quiet = ingest_output("all", &home, &data, false);
    assert_published(&quiet, "the quiet pass after the rebuild");
    assert!(
        String::from_utf8_lossy(&quiet.stdout).contains("unchanged since last index"),
        "the rebuilt cache did not settle"
    );
    cleanup(&home, &data);
}

/// A directory in the head's place, the cache otherwise whole: every pass, plain and `--full`,
/// moves it aside untouched, records its head, and publishes the churn; nothing wedges.
#[test]
fn a_directory_in_the_heads_place_is_moved_aside_and_every_pass_publishes() {
    let home = temp_dir("head-directory-home");
    let data = temp_dir("head-directory-data");
    copy_dir(&fixture_home("claude"), &home);
    churn_claude(&home, 0);
    codex_rollout(&home, "06", ADDED_SESSION, 12, &[ADDED_PROMPT]);
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    let head = data.join(".ingest_cache.bin.head");
    fs::remove_file(&head).unwrap();
    fs::create_dir(&head).unwrap();
    fs::write(head.join("keep.txt"), b"user content").unwrap();
    for (minute, full) in [(1, false), (2, false), (3, true), (4, false)] {
        let churn = churn_claude(&home, minute);
        let output = ingest_output("all", &home, &data, full);
        assert_published(
            &output,
            &format!(
                "pass {minute}{} beside a directory head",
                if full { " (--full)" } else { "" }
            ),
        );
        let published = normalize(&data);
        assert!(published.contains(ADDED_PROMPT) && published.contains(&churn));
        assert!(
            fs::symlink_metadata(&head).is_ok_and(|metadata| metadata.is_file()),
            "pass {minute}: the head is not a regular file"
        );
    }
    let aside: Vec<_> = fs::read_dir(&data)
        .unwrap()
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .is_some_and(|name| name.to_string_lossy().contains(".head.displaced-"))
        })
        .collect();
    assert_eq!(
        aside.len(),
        1,
        "the directory was not moved aside exactly once"
    );
    assert_eq!(
        fs::read(aside[0].join("keep.txt")).unwrap(),
        b"user content"
    );
    let quiet = ingest_output("all", &home, &data, false);
    assert_published(&quiet, "the quiet pass after the head was restored");
    assert!(
        String::from_utf8_lossy(&quiet.stdout).contains("unchanged since last index"),
        "the cache did not settle"
    );
    cleanup(&home, &data);
}
