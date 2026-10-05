//! Upgrading over a data dir release 0.3.2 published beside a source it could not read (its parse
//! cache awaiting a reparse, maybe no published source snapshot): every pass publishes churn, keeps
//! that release's rows and discloses the source, and every pass after the first runs warm.

mod common;

use common::*;
use std::fs;
use std::path::{Path, PathBuf};

const CLAUDE_TEXT: &str = "how do i fix the flaky timer test";
const CRUSH_TEXT: &str = "convert the readme to asciidoc";
const KIMI_TEXT: &str = "port the script to argparse";
const CHAT_SESSION: &str = "22222222-2222-4222-8222-222222222222";

/// What release 0.3.2 left where the current build publishes its source snapshot.
#[derive(Clone, Copy, Debug)]
enum Snapshot {
    Published,
    /// Held back beside a source issue, or never taken by a `--emit-rows` first index.
    Withheld,
    /// Held back, with that pass's preflight left as the pending retry marker.
    Pending,
}

/// Rewrite `data` as release 0.3.2 left it: a cache-version-24 parse cache (the current entry
/// layout, superseded parser semantics) that build owned, no token-material record, and
/// `snapshot` in place of the source snapshot it published with its harness-policy snapshot.
fn age_to_release_0_3_2(data: &Path, snapshot: Snapshot) {
    assert!(!data.join(".ingest_cache.bin.journal").exists());
    let cache = data.join(".ingest_cache.bin");
    let wrapped = fs::read(&cache).unwrap();
    assert_eq!(&wrapped[12..20], b"AGRPCB01");
    assert_eq!(&wrapped[84..88], &0_u32.to_le_bytes());
    let mut legacy = wrapped[100..].to_vec();
    legacy[..4].copy_from_slice(&24_u32.to_le_bytes());
    fs::write(&cache, legacy).unwrap();
    fs::write(
        data.join(".derived-owner.json"),
        br#"{"version":1,"build_id":"03020302030203020302"}"#,
    )
    .unwrap();
    fs::remove_file(data.join(".token_material.json")).unwrap();
    let published = data.join(".source_snapshot.bin");
    match snapshot {
        Snapshot::Published => assert!(published.exists()),
        Snapshot::Withheld => fs::remove_file(published).unwrap(),
        Snapshot::Pending => fs::rename(published, data.join(".ingest_pending.bin")).unwrap(),
    }
    if !matches!(snapshot, Snapshot::Published) {
        fs::remove_file(data.join(".harness_prefixes.snapshot")).unwrap();
    }
}

/// A claude chat of its own, readable throughout, that every pass after the upgrade churns.
fn plant_chat(home: &Path) -> PathBuf {
    let project = home.join(".claude").join("projects").join("proj-beta");
    fs::create_dir_all(&project).unwrap();
    let path = project.join(format!("{CHAT_SESSION}.jsonl"));
    append_line(&path, 0, "an unrelated readable chat");
    path
}

fn append_line(chat: &Path, minute: u32, text: &str) {
    let row = serde_json::json!({
        "type": "user", "userType": "external", "sessionId": CHAT_SESSION,
        "timestamp": format!("2026-01-03T10:{minute:02}:00.000Z"), "cwd": "/work/beta",
        "message": {"role": "user", "content": text},
    });
    let mut body = fs::read_to_string(chat).unwrap_or_default();
    body.push_str(&format!("{row}\n"));
    fs::write(chat, body).unwrap();
}

fn issue_kinds(data: &Path, agent: &str, path: &Path) -> Vec<String> {
    let Ok(body) = fs::read(data.join(".source-health.json")) else {
        return Vec::new();
    };
    let health: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let path = path.to_string_lossy();
    health["issues"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|issue| issue["agent"] == agent && issue["path"] == path.as_ref())
        .filter_map(|issue| issue["kind"].as_str().map(str::to_owned))
        .collect()
}

fn assert_published(output: &std::process::Output, context: &str) {
    assert!(
        output.status.success(),
        "{context}: exit {:?}\nstderr:\n{}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Claude transcripts a pass found, and how many it reparsed, from its stats line.
fn claude_reparse(output: &std::process::Output, context: &str) -> (usize, usize) {
    let stdout = String::from_utf8_lossy(&output.stdout);
    stdout
        .lines()
        .find_map(|line| {
            let stats = line.trim_start().strip_prefix("[projects] ")?;
            let files: usize = stats.split(' ').next()?.parse().ok()?;
            let reparsed: usize = stats.split('(').nth(1)?.split(' ').next()?.parse().ok()?;
            Some((files, reparsed))
        })
        .unwrap_or_else(|| panic!("{context}: no claude stats line in\n{stdout}"))
}

/// The source an upgrade finds broken, and how its disclosure must read.
struct Broken<'a> {
    agent: &'a str,
    path: &'a Path,
    kinds: &'a [&'a str],
    /// A durable defect settles as on a fresh install: a published snapshot, no pending retry.
    durable: bool,
}

/// Index `home` as the current build does, age `data` to release 0.3.2 with `snapshot`, break a
/// source with `break_source`, then require three passes to publish churn while keeping `kept`;
/// every pass after the first upgrades nothing more, so it must run warm.
fn upgrade_publishes_churn(
    home: &Path,
    snapshot: Snapshot,
    break_source: impl Fn(),
    broken: Broken<'_>,
    kept: &str,
) {
    let chat = plant_chat(home);
    let data = temp_dir("release-upgrade-data");
    assert_published(&ingest_output("all", home, &data, false), "first index");
    assert!(normalize(&data).contains(kept));
    break_source();
    age_to_release_0_3_2(&data, snapshot);

    for minute in [1, 2, 3] {
        let churn = format!("upgrade churn {minute}");
        append_line(&chat, minute, &churn);
        // Its consumer deletes the changed-session delta once applied.
        let _ = fs::remove_file(data.join(".changed_sessions"));
        let policy_published = data.join(".harness_prefixes.snapshot").exists();
        let output = ingest_output("all", home, &data, false);
        let context = format!("{snapshot:?} snapshot, pass {minute} after the upgrade");
        assert_published(&output, &context);
        // Reparsing every transcript, unchanged ones included, is the complete pass a cache
        // awaiting reparse forces; skipping one proves the cache decoded current, so warm.
        let (files, reparsed) = claude_reparse(&output, &context);
        if minute == 1 {
            assert_eq!(
                reparsed, files,
                "{context}: the upgrade did not reparse whole"
            );
        } else {
            assert!(
                reparsed < files,
                "{context}: reparsed all {files} claude transcripts, unchanged ones included"
            );
            // Under an unchanged published harness policy only a complete pass writes `*`.
            if policy_published {
                let changed = fs::read_to_string(data.join(".changed_sessions")).unwrap();
                assert_ne!(changed, "*\n", "{context}: the pass was complete");
            }
        }
        let published = normalize(&data);
        assert!(
            published.contains(&churn),
            "{context}: churn was not published"
        );
        assert!(
            published.contains(kept),
            "{context}: published rows were dropped"
        );
        let disclosed = issue_kinds(&data, broken.agent, broken.path);
        assert!(
            disclosed
                .iter()
                .any(|kind| broken.kinds.contains(&kind.as_str())),
            "{context}: {} source at {} was not disclosed ({disclosed:?})",
            broken.agent,
            broken.path.display()
        );
        if broken.durable {
            assert!(
                data.join(".source_snapshot.bin").exists()
                    && !data.join(".ingest_pending.bin").exists(),
                "{context}: a durably broken source left the upgrade retrying"
            );
        }
    }
    let _ = fs::remove_dir_all(&data);
}

/// A crush store the release indexed, beside a second database path at `~/.crush/crush.db`.
fn crush_upgrade_home() -> (PathBuf, PathBuf) {
    let home = crush_home();
    copy_dir(&fixture_home("claude"), &home);
    let second = home.join(".crush").join("crush.db");
    fs::create_dir_all(second.parent().unwrap()).unwrap();
    (home, second)
}

#[test]
fn upgrade_publishes_beside_a_foreign_crush_database_that_release_never_read() {
    for snapshot in [Snapshot::Withheld, Snapshot::Pending] {
        let (home, foreign) = crush_upgrade_home();
        fs::write(&foreign, b"plain text where crush keeps its database\n").unwrap();
        upgrade_publishes_churn(
            &home,
            snapshot,
            || {},
            Broken {
                agent: "crush",
                path: &foreign,
                kinds: &["unsupported-file-type"],
                durable: true,
            },
            CRUSH_TEXT,
        );
        let _ = fs::remove_dir_all(&home);
    }
}

#[cfg(unix)]
#[test]
fn upgrade_publishes_beside_an_unreadable_crush_database_that_release_never_read() {
    use std::os::unix::fs::PermissionsExt;

    for snapshot in [Snapshot::Withheld, Snapshot::Pending] {
        let (home, denied) = crush_upgrade_home();
        fs::write(&denied, b"").unwrap();
        fs::set_permissions(&denied, fs::Permissions::from_mode(0o000)).unwrap();
        if fs::read(&denied).is_ok() {
            // Privileged runners ignore the mode bits; there is no denial to observe.
            fs::set_permissions(&denied, fs::Permissions::from_mode(0o600)).unwrap();
            let _ = fs::remove_dir_all(&home);
            return;
        }
        upgrade_publishes_churn(
            &home,
            snapshot,
            || {},
            Broken {
                agent: "crush",
                path: &denied,
                kinds: &["permission-denied"],
                durable: true,
            },
            CRUSH_TEXT,
        );
        fs::set_permissions(&denied, fs::Permissions::from_mode(0o600)).unwrap();
        let _ = fs::remove_dir_all(&home);
    }
}

#[cfg(unix)]
#[test]
fn upgrade_publishes_beside_an_unreadable_transcript_release_published() {
    use std::os::unix::fs::PermissionsExt;

    for snapshot in [Snapshot::Published, Snapshot::Pending] {
        let home = temp_dir("release-upgrade-claude-home");
        copy_dir(&fixture_home("claude"), &home);
        let transcript = home
            .join(".claude")
            .join("projects")
            .join("proj-alpha")
            .join("sess-claude-0001.jsonl");
        let deny = || {
            fs::set_permissions(&transcript, fs::Permissions::from_mode(0o000)).unwrap();
        };
        deny();
        let observable = fs::read(&transcript).is_err();
        fs::set_permissions(&transcript, fs::Permissions::from_mode(0o644)).unwrap();
        if !observable {
            // Privileged runners ignore the mode bits; there is no denial to observe.
            let _ = fs::remove_dir_all(&home);
            return;
        }
        upgrade_publishes_churn(
            &home,
            snapshot,
            deny,
            Broken {
                agent: "claude",
                path: &transcript,
                kinds: &["source-read-failed", "permission-denied"],
                durable: false,
            },
            CLAUDE_TEXT,
        );
        fs::set_permissions(&transcript, fs::Permissions::from_mode(0o644)).unwrap();
        let _ = fs::remove_dir_all(&home);
    }
}

#[test]
fn upgrade_publishes_beside_kimi_sessions_deleted_under_a_kept_config() {
    for snapshot in [Snapshot::Published, Snapshot::Pending] {
        let home = temp_dir("release-upgrade-kimi-home");
        copy_dir(&fixture_home("claude"), &home);
        copy_dir(&fixture_home("kimi"), &home);
        let store = home.join(".kimi");
        upgrade_publishes_churn(
            &home,
            snapshot,
            || fs::remove_dir_all(store.join("sessions")).unwrap(),
            Broken {
                agent: "kimi",
                path: &store,
                kinds: &["source-read-incomplete"],
                durable: false,
            },
            KIMI_TEXT,
        );
        let _ = fs::remove_dir_all(&home);
    }
}
