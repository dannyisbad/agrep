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
        Snapshot::Withheld => {
            let _ = fs::remove_file(published);
        }
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

/// Remove every event-completeness proof, as a crash between event writes and their proof does.
fn strip_event_proofs(data: &Path) {
    for proof in fs::read_dir(data).unwrap().flatten() {
        if proof
            .file_name()
            .to_string_lossy()
            .starts_with(".events_complete")
        {
            fs::remove_file(proof.path()).unwrap();
        }
    }
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

/// How the first pass after an upgrade beside a never-readable store begins.
#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
enum UpgradeStart {
    /// As the release left the data dir.
    AsReleased,
    /// The release's data dir had already lost its event-completeness proofs.
    ProofsLost,
    /// The first pass was killed inside its cache commit: it had taken the stores over (its
    /// cache is the release's, re-encoded with every entry awaiting reparse) and revoked the
    /// event proofs, but never renamed its own cache into place.
    KilledInCommit,
}

/// A store no pass could ever read, locked beside the release's data dir.
#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
enum NeverRead {
    ClaudeProject,
    /// The one adapter whose reads may be partial: its silence must not outlast a read of the
    /// published generation showing the release published none of its rows.
    OpencodeStore,
}

/// The release held its snapshot back beside a foreign crush database and a store no pass could
/// ever read. Its cache proves a claude directory held nothing; the published generation proves
/// opencode published nothing. Either way the first pass settles, event repair included.
#[cfg(unix)]
fn upgrade_settles_beside_a_store_release_never_read(start: UpgradeStart, store: NeverRead) {
    let (home, foreign) = crush_upgrade_home();
    fs::write(&foreign, b"plain text where crush keeps its database\n").unwrap();
    let locked = match store {
        NeverRead::ClaudeProject => lock_claude_project(&home),
        NeverRead::OpencodeStore => lock_opencode_store(&home),
    };
    let Some(locked) = locked else {
        let _ = fs::remove_dir_all(&home);
        return;
    };
    let chat = plant_chat(&home);
    let data = temp_dir("release-upgrade-locked-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    age_to_release_0_3_2(&data, Snapshot::Withheld);
    if let UpgradeStart::KilledInCommit = start {
        // A harness policy no pass can read stops the pass right after its takeover.
        let policy = data.join("harness_prefixes.txt");
        fs::write(&policy, b"\xff\xfe").unwrap();
        assert!(!ingest_output("all", &home, &data, false).status.success());
        fs::remove_file(&policy).unwrap();
        let owner = fs::read_to_string(data.join(".derived-owner.json")).unwrap();
        assert!(
            !owner.contains("03020302030203020302"),
            "no takeover: {owner}"
        );
        let cache = fs::read(data.join(".ingest_cache.bin")).unwrap();
        assert_eq!(&cache[12..20], b"AGRPCB01", "the cache was not re-encoded");
    }
    if !matches!(start, UpgradeStart::AsReleased) {
        strip_event_proofs(&data);
    }
    assert!(
        !data.join(".source_snapshot.bin").exists(),
        "{start:?} {store:?}"
    );
    assert_settles_beside_a_never_read_dir(&home, &data, &locked, 1, |minute| {
        let text = format!("upgrade churn {minute}");
        append_line(&chat, minute, &text);
        text
    });
    assert!(normalize(&data).contains(CRUSH_TEXT), "{start:?} {store:?}");
    unlock_dir(&locked);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn upgrade_settles_on_its_first_pass_beside_a_project_directory_release_never_read() {
    upgrade_settles_beside_a_store_release_never_read(
        UpgradeStart::AsReleased,
        NeverRead::ClaudeProject,
    );
}

#[cfg(unix)]
#[test]
fn upgrade_that_must_rebuild_events_settles_beside_a_project_directory_release_never_read() {
    upgrade_settles_beside_a_store_release_never_read(
        UpgradeStart::ProofsLost,
        NeverRead::ClaudeProject,
    );
}

#[cfg(unix)]
#[test]
fn upgrade_killed_inside_its_first_commit_settles_beside_a_project_directory_release_never_read() {
    upgrade_settles_beside_a_store_release_never_read(
        UpgradeStart::KilledInCommit,
        NeverRead::ClaudeProject,
    );
}

#[cfg(unix)]
#[test]
fn upgrade_settles_on_its_first_pass_beside_an_opencode_store_release_never_read() {
    upgrade_settles_beside_a_store_release_never_read(
        UpgradeStart::AsReleased,
        NeverRead::OpencodeStore,
    );
}

#[cfg(unix)]
#[test]
fn upgrade_that_must_rebuild_events_settles_beside_an_opencode_store_release_never_read() {
    upgrade_settles_beside_a_store_release_never_read(
        UpgradeStart::ProofsLost,
        NeverRead::OpencodeStore,
    );
}

#[cfg(unix)]
#[test]
fn upgrade_killed_inside_its_first_commit_settles_beside_an_opencode_store_release_never_read() {
    upgrade_settles_beside_a_store_release_never_read(
        UpgradeStart::KilledInCommit,
        NeverRead::OpencodeStore,
    );
}

const OPENCODE_TEXT: &str = "convert config to yaml";

/// Index `home`'s opencode store at `db`, with a text part caught mid-write, as release 0.3.2
/// would have: the partial read publishes the rest of the database without caching it, and the
/// snapshot ends up as `snapshot`. Returns the data dir, the database parked aside.
fn release_dir_with_uncached_opencode_rows(
    home: &Path,
    db: &Path,
    snapshot: Snapshot,
) -> (PathBuf, PathBuf) {
    rusqlite::Connection::open(db)
        .unwrap()
        .execute(
            "INSERT INTO part SELECT 'p9', id, session_id, ?1, 1767348001900 FROM message
             WHERE id = 'm2'",
            [r#"{"type":"text","text":"torn mid-wri"#],
        )
        .unwrap();
    let data = temp_dir("release-upgrade-partial-data");
    assert_published(&ingest_output("all", home, &data, false), "first index");
    assert!(normalize(&data).contains(OPENCODE_TEXT));
    // The release's cache: every other source, and nothing of the partial read.
    let parked = home.join("opencode.db.parked");
    fs::rename(db, &parked).unwrap();
    let scratch = temp_dir("release-upgrade-partial-scratch");
    assert_published(&ingest_output("all", home, &scratch, false), "cache donor");
    fs::copy(
        scratch.join(".ingest_cache.bin"),
        data.join(".ingest_cache.bin"),
    )
    .unwrap();
    let _ = fs::remove_dir_all(&scratch);
    age_to_release_0_3_2(&data, snapshot);
    (data, parked)
}

/// The release published a partial opencode read without caching it, and held its snapshot back
/// beside some other source issue. If that database fails the first pass after the upgrade, the
/// cache cannot prove it held no rows: the pass keeps them, and the next good read caches them.
#[test]
fn upgrade_keeps_rows_release_published_from_a_partial_read_it_never_cached() {
    let home = opencode_home();
    copy_dir(&fixture_home("claude"), &home);
    let db = home.join(".local/share/opencode/opencode.db");
    let (data, parked) = release_dir_with_uncached_opencode_rows(&home, &db, Snapshot::Withheld);

    fs::write(&db, b"not a database at all\n").unwrap();
    let failed = ingest_output("all", &home, &data, false);
    assert!(
        normalize(&data).contains(OPENCODE_TEXT),
        "the first pass dropped rows the release published (exit {:?})",
        failed.status.code()
    );
    fs::rename(&parked, &db).unwrap();
    for pass in 1..=2 {
        assert_published(
            &ingest_output("all", &home, &data, false),
            &format!("healed pass {pass}"),
        );
        assert!(normalize(&data).contains(OPENCODE_TEXT));
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The release published a partial opencode read without caching it. Once the store's directory
/// cannot be listed, nothing the upgrade decoded serves those rows, whatever the release did with
/// its snapshot: every pass keeps them until access returns, and then the index heals.
#[cfg(unix)]
#[test]
fn upgrade_keeps_uncached_opencode_rows_behind_a_store_directory_it_cannot_list() {
    for snapshot in [Snapshot::Published, Snapshot::Withheld, Snapshot::Pending] {
        let home = opencode_home();
        copy_dir(&fixture_home("claude"), &home);
        let store = home.join(".local/share/opencode");
        let db = store.join("opencode.db");
        let (data, parked) = release_dir_with_uncached_opencode_rows(&home, &db, snapshot);
        fs::rename(&parked, &db).unwrap();
        let Some(locked) = lock_dir(&store) else {
            let _ = fs::remove_dir_all(&home);
            let _ = fs::remove_dir_all(&data);
            return;
        };
        let passes: Vec<_> = (1..=3)
            .map(|_| {
                let code = ingest_output("all", &home, &data, false).status.code();
                (normalize(&data).contains(OPENCODE_TEXT), code)
            })
            .collect();
        unlock_dir(&locked);
        for (pass, (kept, code)) in passes.into_iter().enumerate() {
            assert!(
                kept,
                "{snapshot:?} snapshot, pass {} dropped rows the release published (exit {code:?})",
                pass + 1
            );
        }
        for pass in 1..=2 {
            assert_published(
                &ingest_output("all", &home, &data, false),
                &format!("{snapshot:?} snapshot, healed pass {pass}"),
            );
            assert!(normalize(&data).contains(OPENCODE_TEXT));
        }
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

const NIGHTLY_TEXT: &str = "convert config to toml";

/// opencode keeps one database per release channel side by side. The release published a partial
/// read of one uncached beside one it cached; the cached one's rows vouch for nothing of the
/// other, so a store directory neither can be listed in keeps both until access returns.
#[cfg(unix)]
#[test]
fn upgrade_keeps_uncached_rows_of_one_opencode_channel_beside_a_cached_one() {
    for snapshot in [Snapshot::Published, Snapshot::Withheld, Snapshot::Pending] {
        let home = opencode_home();
        copy_dir(&fixture_home("claude"), &home);
        let store = home.join(".local/share/opencode");
        let nightly = store.join("opencode-nightly.db");
        let seed = fs::read_to_string(fixtures_dir().join("opencode").join("seed.sql"))
            .unwrap()
            .replace("sess-oc", "nightly-oc")
            .replace(OPENCODE_TEXT, NIGHTLY_TEXT);
        rusqlite::Connection::open(&nightly)
            .unwrap()
            .execute_batch(&seed)
            .unwrap();
        let (data, parked) = release_dir_with_uncached_opencode_rows(&home, &nightly, snapshot);
        assert!(normalize(&data).contains(NIGHTLY_TEXT));
        fs::rename(&parked, &nightly).unwrap();
        let Some(locked) = lock_dir(&store) else {
            let _ = fs::remove_dir_all(&home);
            let _ = fs::remove_dir_all(&data);
            return;
        };
        let passes: Vec<_> = (1..=3)
            .map(|_| {
                let code = ingest_output("all", &home, &data, false).status.code();
                let published = normalize(&data);
                let kept = published.contains(NIGHTLY_TEXT) && published.contains(OPENCODE_TEXT);
                (kept, code)
            })
            .collect();
        unlock_dir(&locked);
        for (pass, (kept, code)) in passes.into_iter().enumerate() {
            assert!(
                kept,
                "{snapshot:?} snapshot, pass {} dropped rows the release published (exit {code:?})",
                pass + 1
            );
        }
        for pass in 1..=2 {
            assert_published(
                &ingest_output("all", &home, &data, false),
                &format!("{snapshot:?} snapshot, healed pass {pass}"),
            );
            let published = normalize(&data);
            assert!(published.contains(NIGHTLY_TEXT) && published.contains(OPENCODE_TEXT));
        }
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// opencode's channel database is a symlink no pass follows, a durable issue in its store. A
/// session deleted from the readable database since the release indexed it is a change the pass
/// observes, not a row lost behind that link: every pass publishes it, as the churn beside it.
#[cfg(unix)]
#[test]
fn upgrade_publishes_an_opencode_deletion_beside_a_symlinked_channel_database() {
    const CHILD_TEXT: &str = "check the generated yaml schema";
    for snapshot in [Snapshot::Published, Snapshot::Withheld, Snapshot::Pending] {
        let home = opencode_home();
        copy_dir(&fixture_home("claude"), &home);
        let store = home.join(".local/share/opencode");
        let target = home.join("nightly-copy.db");
        let seed = fs::read_to_string(fixtures_dir().join("opencode").join("seed.sql"))
            .unwrap()
            .replace("sess-oc", "nightly-oc");
        rusqlite::Connection::open(&target)
            .unwrap()
            .execute_batch(&seed)
            .unwrap();
        std::os::unix::fs::symlink(&target, store.join("opencode-nightly.db")).unwrap();
        let chat = plant_chat(&home);
        let data = temp_dir("release-upgrade-oc-link-data");
        assert_published(&ingest_output("all", &home, &data, false), "first index");
        let released = normalize(&data);
        assert!(released.contains(OPENCODE_TEXT) && released.contains(CHILD_TEXT));
        age_to_release_0_3_2(&data, snapshot);
        rusqlite::Connection::open(store.join("opencode.db"))
            .unwrap()
            .execute_batch(
                "DELETE FROM part WHERE session_id = 'sess-oc-child';
                 DELETE FROM message WHERE session_id = 'sess-oc-child';
                 DELETE FROM session WHERE id = 'sess-oc-child';",
            )
            .unwrap();
        for minute in [1, 2, 3] {
            let churn = format!("upgrade churn {minute}");
            append_line(&chat, minute, &churn);
            let context = format!("{snapshot:?} snapshot, pass {minute} after the upgrade");
            assert_published(&ingest_output("all", &home, &data, false), &context);
            let published = normalize(&data);
            assert!(
                published.contains(&churn),
                "{context}: churn was not published"
            );
            assert!(
                published.contains(OPENCODE_TEXT),
                "{context}: opencode rows dropped"
            );
            assert!(
                !published.contains(CHILD_TEXT),
                "{context}: the deletion was not published"
            );
        }
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

const EMPTY_CHAT: &str = "44444444-4444-4444-8444-444444444444";

/// A transcript the release read whole and found nothing to publish in leaves a cached entry with
/// no rows. Its directory turning unlistable costs no row, so every pass after the upgrade
/// publishes as a warm pass does, rather than holding every agent until access returns.
#[cfg(unix)]
#[test]
fn upgrade_publishes_beside_an_unlistable_directory_whose_files_published_nothing() {
    for snapshot in [Snapshot::Published, Snapshot::Pending] {
        let home = temp_dir("release-upgrade-empty-home");
        copy_dir(&fixture_home("claude"), &home);
        let project = home.join(".claude/projects/proj-empty");
        fs::create_dir_all(&project).unwrap();
        fs::write(
            project.join(format!("{EMPTY_CHAT}.jsonl")),
            "{\"type\":\"summary\",\"summary\":\"an empty chat\",\"leafUuid\":\"x\"}\n",
        )
        .unwrap();
        let chat = plant_chat(&home);
        let data = temp_dir("release-upgrade-empty-data");
        assert_published(&ingest_output("all", &home, &data, false), "first index");
        age_to_release_0_3_2(&data, snapshot);
        let Some(locked) = lock_dir(&project) else {
            let _ = fs::remove_dir_all(&home);
            let _ = fs::remove_dir_all(&data);
            return;
        };
        let passes: Vec<_> = (1..=3)
            .map(|minute| {
                let churn = format!("upgrade churn {minute}");
                append_line(&chat, minute, &churn);
                let output = ingest_output("all", &home, &data, false);
                let published = normalize(&data);
                (
                    output,
                    published.contains(&churn) && published.contains(CLAUDE_TEXT),
                )
            })
            .collect();
        unlock_dir(&locked);
        for (minute, (output, published)) in (1..).zip(passes) {
            let context = format!("{snapshot:?} snapshot, pass {minute} after the upgrade");
            assert_published(&output, &context);
            assert!(published, "{context}: churn or published rows missing");
        }
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// How the passes beside a locked directory listing a never-read file begin.
#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
enum NeverReadStart {
    /// The first pass after an upgrade from release 0.3.2, which published the snapshot.
    Upgrade,
    /// A current data dir whose event proofs a crash revoked.
    CrashRepair,
}

const GONE_TEXT: &str = "a chat deleted before the passes";

/// A transcript unreadable since the first index is listed though never cached. Its directory
/// turning unlistable must not hold `start`'s passes back, nor may a transcript deleted elsewhere
/// (`delete_elsewhere`), whose rows the pass saw go, count as lost behind the locked directory.
#[cfg(unix)]
fn publishes_beside_an_unlistable_directory_listing_a_file_never_read(
    start: NeverReadStart,
    delete_elsewhere: bool,
) {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("release-upgrade-never-read-home");
    copy_dir(&fixture_home("claude"), &home);
    let projects = home.join(".claude/projects");
    let project = projects.join("proj-mixed");
    let transcript = |project: &Path, session: &str, text: &str| {
        fs::create_dir_all(project).unwrap();
        let row = serde_json::json!({
            "type": "user", "userType": "external", "sessionId": session,
            "timestamp": "2026-01-03T09:00:00.000Z", "cwd": "/work/mixed",
            "message": {"role": "user", "content": text},
        });
        let path = project.join(format!("{session}.jsonl"));
        fs::write(&path, format!("{row}\n")).unwrap();
        path
    };
    transcript(
        &project,
        "55555555-5555-4555-8555-555555555555",
        "a row beside a chat never read",
    );
    let never = transcript(&project, EMPTY_CHAT, "a row no pass could read");
    fs::set_permissions(&never, fs::Permissions::from_mode(0o000)).unwrap();
    let gone = projects.join("proj-gone");
    transcript(&gone, "66666666-6666-4666-8666-666666666666", GONE_TEXT);
    let chat = plant_chat(&home);
    let data = temp_dir("release-upgrade-never-read-data");
    let first = ingest_output("all", &home, &data, false);
    let released = normalize(&data).contains(GONE_TEXT);
    let release_listed = fs::read(&never).is_err() && data.join(".source_snapshot.bin").exists();
    let locked = release_listed.then(|| lock_dir(&project)).flatten();
    if locked.is_some() {
        match start {
            NeverReadStart::Upgrade => age_to_release_0_3_2(&data, Snapshot::Published),
            NeverReadStart::CrashRepair => strip_event_proofs(&data),
        }
        if delete_elsewhere {
            fs::remove_dir_all(&gone).unwrap();
        }
    }
    let passes: Vec<_> = (1..=3)
        .filter(|_| locked.is_some())
        .map(|minute| {
            let churn = format!("upgrade churn {minute}");
            append_line(&chat, minute, &churn);
            let output = ingest_output("all", &home, &data, false);
            let published = normalize(&data);
            let kept = published.contains(&churn) && published.contains("a chat never read");
            (output, kept, published.contains(GONE_TEXT))
        })
        .collect();
    if let Some(locked) = &locked {
        unlock_dir(locked);
    }
    fs::set_permissions(&never, fs::Permissions::from_mode(0o644)).unwrap();
    assert_published(&first, "first index");
    assert!(released, "the first index did not publish {GONE_TEXT:?}");
    for (minute, (output, kept, gone_published)) in (1..).zip(passes) {
        let context = format!("{start:?}, pass {minute}");
        assert_published(&output, &context);
        assert!(kept, "{context}: churn or published rows missing");
        assert_eq!(
            gone_published, !delete_elsewhere,
            "{context}: {GONE_TEXT:?}"
        );
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn upgrade_publishes_beside_an_unlistable_directory_listing_a_file_never_read() {
    publishes_beside_an_unlistable_directory_listing_a_file_never_read(
        NeverReadStart::Upgrade,
        false,
    );
}

#[cfg(unix)]
#[test]
fn upgrade_publishes_a_deletion_beside_an_unlistable_directory_listing_a_file_never_read() {
    publishes_beside_an_unlistable_directory_listing_a_file_never_read(
        NeverReadStart::Upgrade,
        true,
    );
}

#[cfg(unix)]
#[test]
fn crash_repair_publishes_a_deletion_beside_an_unlistable_directory_listing_a_file_never_read() {
    publishes_beside_an_unlistable_directory_listing_a_file_never_read(
        NeverReadStart::CrashRepair,
        true,
    );
}

#[cfg(unix)]
const SPLIT_SESSION: &str = "55555555-5555-4555-8555-555555555555";

/// Write a codex rollout of `session` under `sessions/2026/01/<day>`, a prompt a minute from
/// `hour`, each answered.
#[cfg(unix)]
fn codex_rollout(home: &Path, day: &str, session: &str, hour: u32, prompts: &[&str]) -> PathBuf {
    let dir = home.join(".codex/sessions/2026/01").join(day);
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!(
        "rollout-2026-01-{day}T{hour:02}-00-00-{session}.jsonl"
    ));
    let mut lines = vec![
        serde_json::json!({"type": "session_meta",
                           "payload": {"id": session, "cwd": "/work/split", "source": "cli"}}),
        serde_json::json!({"type": "turn_context", "payload": {"model": "gpt-5.5-codex"}}),
    ];
    for (minute, prompt) in prompts.iter().enumerate() {
        let ts = format!("2026-01-{day}T{hour:02}:{minute:02}:00.000Z");
        let message = |role: &str, kind: &str, text: String| {
            serde_json::json!({"type": "response_item", "timestamp": ts,
                               "payload": {"type": "message", "role": role,
                                           "content": [{"type": kind, "text": text}]}})
        };
        lines.push(message("user", "input_text", (*prompt).to_owned()));
        lines.push(serde_json::json!({"type": "event_msg", "timestamp": ts,
                                      "payload": {"type": "user_message", "message": prompt}}));
        lines.push(message(
            "assistant",
            "output_text",
            format!("done: {prompt}"),
        ));
    }
    let body: String = lines.iter().map(|line| format!("{line}\n")).collect();
    fs::write(&path, body).unwrap();
    path
}

/// How the passes beside a day directory turned into a link begin.
#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
enum LinkStart {
    /// A current data dir, warm.
    Warm,
    /// The first pass after an upgrade from release 0.3.2, which published the snapshot.
    Upgrade,
    /// A current data dir whose event proofs a crash revoked.
    CrashRepair,
}

/// A codex session split over a rollout and a fork replaying its first two prompts. Its day
/// directory then moves onto a drive, linked back, that unmounts: no pass may drop the prompts
/// only that rollout holds, nor stop disclosing it, and the remounted drive leaves them in place.
#[cfg(unix)]
fn keeps_rows_behind_a_day_directory_link_whose_target_is_gone(start: LinkStart) {
    let home = temp_dir("release-upgrade-link-home");
    copy_dir(&fixture_home("claude"), &home);
    let prompts = ["prompt one", "prompt two", "prompt three", "prompt four"];
    let rollout = codex_rollout(&home, "03", SPLIT_SESSION, 10, &prompts);
    codex_rollout(&home, "05", SPLIT_SESSION, 10, &prompts[..2]);
    let chat = plant_chat(&home);
    let data = temp_dir("release-upgrade-link-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    let unique = |published: &str| prompts[2..].iter().all(|text| published.contains(text));
    assert!(unique(&normalize(&data)));
    match start {
        LinkStart::Warm => {}
        LinkStart::Upgrade => age_to_release_0_3_2(&data, Snapshot::Published),
        LinkStart::CrashRepair => strip_event_proofs(&data),
    }
    let day = rollout.parent().unwrap().to_path_buf();
    let drive = home.join("drive");
    fs::create_dir_all(&drive).unwrap();
    fs::rename(&day, drive.join("03")).unwrap();
    std::os::unix::fs::symlink(drive.join("03"), &day).unwrap();
    fs::rename(drive.join("03"), drive.join("03-unmounted")).unwrap();

    for minute in [1, 2, 3] {
        let churn = format!("upgrade churn {minute}");
        append_line(&chat, minute, &churn);
        let context = format!("{start:?}, pass {minute} with the drive unmounted");
        assert_published(&ingest_output("all", &home, &data, false), &context);
        let published = normalize(&data);
        assert!(
            published.contains(&churn),
            "{context}: churn was not published"
        );
        assert!(
            unique(&published),
            "{context}: the rollout's own prompts were dropped"
        );
        assert!(
            issue_kinds(&data, "codex", &day).contains(&"unsupported-link".to_owned()),
            "{context}: the link was not disclosed"
        );
    }
    fs::rename(drive.join("03-unmounted"), drive.join("03")).unwrap();
    for minute in [4, 5] {
        let churn = format!("upgrade churn {minute}");
        append_line(&chat, minute, &churn);
        let context = format!("{start:?}, pass {minute} with the drive remounted");
        assert_published(&ingest_output("all", &home, &data, false), &context);
        let published = normalize(&data);
        assert!(
            published.contains(&churn) && unique(&published),
            "{context}"
        );
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn a_warm_pass_keeps_rows_behind_a_day_directory_link_whose_target_is_gone() {
    keeps_rows_behind_a_day_directory_link_whose_target_is_gone(LinkStart::Warm);
}

#[cfg(unix)]
#[test]
fn upgrade_keeps_rows_behind_a_day_directory_link_whose_target_is_gone() {
    keeps_rows_behind_a_day_directory_link_whose_target_is_gone(LinkStart::Upgrade);
}

#[cfg(unix)]
#[test]
fn crash_repair_keeps_rows_behind_a_day_directory_link_whose_target_is_gone() {
    keeps_rows_behind_a_day_directory_link_whose_target_is_gone(LinkStart::CrashRepair);
}

/// A codex session resumed after compaction spans two rollouts whose turns collide, so it
/// publishes renumbered. Beside a rollout unreadable since the first index, which the upgrade
/// cannot list (`Upgrade`) or crash repair cannot read, the cache still holds every one of them.
#[cfg(unix)]
fn publishes_a_renumbered_session_beside_a_rollout_never_read(start: NeverReadStart) {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("release-upgrade-renumbered-home");
    copy_dir(&fixture_home("claude"), &home);
    let before = ["prompt one", "prompt two", "prompt three", "prompt four"];
    let after = ["after compaction one", "after compaction two"];
    codex_rollout(&home, "03", SPLIT_SESSION, 10, &before);
    codex_rollout(&home, "05", SPLIT_SESSION, 11, &after);
    let never_session = "66666666-6666-4666-8666-666666666666";
    let never = codex_rollout(
        &home,
        "06",
        never_session,
        12,
        &["a prompt no pass could read"],
    );
    fs::set_permissions(&never, fs::Permissions::from_mode(0o000)).unwrap();
    let chat = plant_chat(&home);
    let data = temp_dir("release-upgrade-renumbered-data");
    let first = ingest_output("all", &home, &data, false);
    let all_held = |published: &str| {
        before
            .iter()
            .chain(&after)
            .all(|text| published.contains(text))
    };
    let released = all_held(&normalize(&data));
    let observable = fs::read(&never).is_err() && data.join(".source_snapshot.bin").exists();
    let day = never.parent().unwrap().to_path_buf();
    let locked = match start {
        NeverReadStart::Upgrade if observable => {
            age_to_release_0_3_2(&data, Snapshot::Published);
            lock_dir(&day).map(Some)
        }
        NeverReadStart::CrashRepair if observable => {
            strip_event_proofs(&data);
            Some(None)
        }
        _ => None,
    };
    let passes: Vec<_> = (1..=3)
        .filter(|_| locked.is_some())
        .map(|minute| {
            let churn = format!("upgrade churn {minute}");
            append_line(&chat, minute, &churn);
            let output = ingest_output("all", &home, &data, false);
            let published = normalize(&data);
            (output, published.contains(&churn) && all_held(&published))
        })
        .collect();
    if let Some(Some(locked)) = &locked {
        unlock_dir(locked);
    }
    fs::set_permissions(&never, fs::Permissions::from_mode(0o644)).unwrap();
    assert_published(&first, "first index");
    assert!(released, "the first index did not publish the session");
    for (minute, (output, kept)) in (1..).zip(passes) {
        let context = format!("{start:?}, pass {minute}");
        assert_published(&output, &context);
        assert!(kept, "{context}: churn or published rows missing");
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn upgrade_publishes_a_renumbered_session_beside_a_rollout_never_read() {
    publishes_a_renumbered_session_beside_a_rollout_never_read(NeverReadStart::Upgrade);
}

#[cfg(unix)]
#[test]
fn crash_repair_publishes_a_renumbered_session_beside_a_rollout_never_read() {
    publishes_a_renumbered_session_beside_a_rollout_never_read(NeverReadStart::CrashRepair);
}

/// An upgrade held back by one scope past others it could publish past names that scope: here
/// the opencode store whose rows the release published uncached, not the foreign crush database
/// listed before it. Its rows stay published, and access to the store heals the index.
#[cfg(unix)]
#[test]
fn upgrade_refusal_names_the_store_that_holds_it_back() {
    let (home, foreign) = crush_upgrade_home();
    fs::write(&foreign, b"plain text where crush keeps its database\n").unwrap();
    let store = home.join(".local/share/opencode");
    fs::create_dir_all(&store).unwrap();
    let db = store.join("opencode.db");
    let seed = fs::read_to_string(fixtures_dir().join("opencode").join("seed.sql")).unwrap();
    rusqlite::Connection::open(&db)
        .unwrap()
        .execute_batch(&seed)
        .unwrap();
    let (data, parked) = release_dir_with_uncached_opencode_rows(&home, &db, Snapshot::Withheld);
    fs::rename(&parked, &db).unwrap();
    strip_event_proofs(&data);
    let Some(locked) = lock_dir(&store) else {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    };
    let held = ingest_output("all", &home, &data, false);
    unlock_dir(&locked);
    let stderr = String::from_utf8_lossy(&held.stderr);
    assert!(!held.status.success(), "{stderr}");
    let blocking = format!("agent opencode: {}", store.display());
    assert!(stderr.contains(&blocking), "{stderr}");
    assert!(!stderr.contains("agent crush"), "{stderr}");
    let published = normalize(&data);
    assert!(published.contains(OPENCODE_TEXT) && published.contains(CRUSH_TEXT));

    assert_published(&ingest_output("all", &home, &data, false), "healed pass");
    assert!(normalize(&data).contains(OPENCODE_TEXT));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A repair pass the corrupted event store holds back, though its decoded base served every scope
/// it could not read, names the scope it could not publish past: the unreadable transcript, not
/// the foreign crush database the release never read and the pass settles past.
#[cfg(unix)]
#[test]
fn repair_refusal_over_an_inconsistent_event_store_names_the_unreadable_transcript() {
    use std::os::unix::fs::PermissionsExt;

    let (home, foreign) = crush_upgrade_home();
    fs::write(&foreign, b"plain text where crush keeps its database\n").unwrap();
    let transcript = home.join(".claude/projects/proj-alpha/sess-claude-0001.jsonl");
    let data = temp_dir("release-upgrade-repair-label-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    age_to_release_0_3_2(&data, Snapshot::Published);
    strip_event_proofs(&data);
    let (name, mut payload) = event_rows(&data)
        .into_iter()
        .find(|(name, _)| name.starts_with("claude-"))
        .unwrap();
    payload.push(b'\n');
    rusqlite::Connection::open(
        data.join("events")
            .join(agrep_core::cache::EVENT_STORE_NAME),
    )
    .unwrap()
    .execute(
        "UPDATE event_sessions SET payload=?1 WHERE name=?2",
        rusqlite::params![payload, name],
    )
    .unwrap();
    fs::set_permissions(&transcript, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&transcript).is_ok() {
        // Privileged runners ignore the mode bits; there is no denial to observe.
        fs::set_permissions(&transcript, fs::Permissions::from_mode(0o644)).unwrap();
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    let held = ingest_output("all", &home, &data, false);
    fs::set_permissions(&transcript, fs::Permissions::from_mode(0o644)).unwrap();
    let stderr = String::from_utf8_lossy(&held.stderr);
    assert!(!held.status.success(), "{stderr}");
    assert!(stderr.contains("event repair observed"), "{stderr}");
    let blocking = format!("agent claude: {}", transcript.display());
    assert!(stderr.contains(&blocking), "{stderr}");
    assert!(!stderr.contains("agent crush"), "{stderr}");

    assert_published(&ingest_output("all", &home, &data, false), "healed pass");
    assert!(normalize(&data).contains(CLAUDE_TEXT));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
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

const CLINE_TEXT: &str = "build a cli flag parser";
const CLINE_SECOND_TEXT: &str = "build a yaml config loader";
const CLINE_SECOND_TASK: &str = "1767348200000";

const CLINE_TORN: &str = r#"[{"role":"user","ts":1767348100000,"content":[{"type":"te"#;

fn cline_task(home: &Path, id: &str) -> PathBuf {
    home.join(".cline/data/tasks")
        .join(id)
        .join("api_conversation_history.json")
}

/// A claude + cline home whose task 1767348100000 was torn before release 0.3.2 indexed it, and
/// its data dir as the release left it: the two readable tasks' rows published, no cline snapshot
/// and the source snapshot withheld. Returns the home, its data dir and the planted claude chat.
fn release_dir_beside_a_torn_whole_store_task() -> (PathBuf, PathBuf, PathBuf) {
    let home = temp_dir("release-upgrade-cline-home");
    copy_dir(&fixture_home("claude"), &home);
    copy_dir(&fixture_home("cline"), &home);
    let first = fs::read_to_string(cline_task(&home, "1767348000000")).unwrap();
    let second = cline_task(&home, CLINE_SECOND_TASK);
    fs::create_dir_all(second.parent().unwrap()).unwrap();
    fs::write(&second, first.replace(CLINE_TEXT, CLINE_SECOND_TEXT)).unwrap();
    let torn = cline_task(&home, "1767348100000");
    fs::create_dir_all(torn.parent().unwrap()).unwrap();
    fs::write(&torn, CLINE_TORN).unwrap();
    let history: Vec<_> = ["1767348000000", CLINE_SECOND_TASK, "1767348100000"]
        .into_iter()
        .map(|id| {
            serde_json::json!({"id": id, "cwdOnTaskInitialization": "/work/delta",
                               "modelId": "claude-fable-5"})
        })
        .collect();
    fs::write(
        home.join(".cline/data/state/taskHistory.json"),
        serde_json::Value::from(history).to_string(),
    )
    .unwrap();
    let chat = plant_chat(&home);
    let data = temp_dir("release-upgrade-cline-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    let published = normalize(&data);
    assert!(published.contains(CLINE_TEXT) && published.contains(CLINE_SECOND_TEXT));
    // The release's cache: every other source, and no cline snapshot.
    let parked = home.join(".cline.parked");
    fs::rename(home.join(".cline"), &parked).unwrap();
    let scratch = temp_dir("release-upgrade-cline-scratch");
    assert_published(&ingest_output("all", &home, &scratch, false), "cache donor");
    fs::copy(
        scratch.join(".ingest_cache.bin"),
        data.join(".ingest_cache.bin"),
    )
    .unwrap();
    let _ = fs::remove_dir_all(&scratch);
    fs::rename(&parked, home.join(".cline")).unwrap();
    age_to_release_0_3_2(&data, Snapshot::Withheld);
    (home, data, chat)
}

/// Read whole, the rows the release published are all the upgrade's cline read serves again: no
/// publication drops one, so every pass publishes beside the torn task, repair passes included.
#[test]
fn upgrade_publishes_beside_a_whole_store_task_torn_before_release_read_it() {
    for proofs_lost in [false, true] {
        let (home, data, chat) = release_dir_beside_a_torn_whole_store_task();
        if proofs_lost {
            strip_event_proofs(&data);
        }
        let torn = cline_task(&home, "1767348100000");
        for minute in [1, 2, 3] {
            let churn = format!("upgrade churn {minute}");
            append_line(&chat, minute, &churn);
            let context = format!("proofs lost: {proofs_lost}, pass {minute} after the upgrade");
            assert_published(&ingest_output("all", &home, &data, false), &context);
            let published = normalize(&data);
            assert!(
                published.contains(&churn),
                "{context}: churn was not published"
            );
            assert!(
                published.contains(CLINE_TEXT) && published.contains(CLINE_SECOND_TEXT),
                "{context}: cline rows were dropped"
            );
            assert!(
                issue_kinds(&data, "cline", &torn).contains(&"source-invalid".to_owned()),
                "{context}: the torn task was not disclosed"
            );
        }
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// Once a task the release published tears too, the upgrade's read serves none of its rows: no
/// pass may publish without them, and the first whole read of that task heals the index.
#[test]
fn upgrade_keeps_whole_store_rows_release_published_once_their_task_tears() {
    let (home, data, chat) = release_dir_beside_a_torn_whole_store_task();
    let published_task = cline_task(&home, CLINE_SECOND_TASK);
    let body = fs::read(&published_task).unwrap();
    fs::write(&published_task, CLINE_TORN).unwrap();
    for minute in [1, 2] {
        append_line(&chat, minute, &format!("upgrade churn {minute}"));
        let output = ingest_output("all", &home, &data, false);
        assert!(
            normalize(&data).contains(CLINE_SECOND_TEXT),
            "pass {minute} dropped rows the release published (exit {:?})",
            output.status.code()
        );
    }
    fs::write(&published_task, body).unwrap();
    append_line(&chat, 3, "upgrade churn 3");
    assert_published(&ingest_output("all", &home, &data, false), "healed pass");
    let published = normalize(&data);
    assert!(published.contains(CLINE_SECOND_TEXT) && published.contains("upgrade churn 3"));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// `(id, project)` of every cline row `data` publishes, sorted.
fn cline_projects(data: &Path) -> Vec<(String, String)> {
    let mut rows: Vec<_> = fs::read_to_string(data.join("messages.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .filter(|row| row["agent"] == "cline")
        .map(|row| {
            let field = |name: &str| row[name].as_str().unwrap().to_owned();
            (field("id"), field("project"))
        })
        .collect();
    rows.sort();
    rows
}

/// taskHistory.json attributes every task to its project. Torn, the upgrade's read still parses
/// each readable task, under a fallback attribution: those rows are not the ones the release
/// published, so no pass replaces them, and the restored index publishes them unchanged.
#[test]
fn upgrade_keeps_whole_store_attribution_once_the_task_index_tears() {
    let (home, data, chat) = release_dir_beside_a_torn_whole_store_task();
    let released = cline_projects(&data);
    assert_eq!(released.len(), 4);
    assert!(released.iter().all(|(_, project)| project == "delta"));
    let history = home.join(".cline/data/state/taskHistory.json");
    let body = fs::read(&history).unwrap();
    fs::write(&history, &body[..body.len() / 2]).unwrap();
    append_line(&chat, 1, "upgrade churn 1");
    ingest_output("all", &home, &data, false);
    assert_eq!(
        cline_projects(&data),
        released,
        "a torn task index re-attributed rows"
    );

    fs::write(&history, body).unwrap();
    for minute in [2, 3] {
        let churn = format!("upgrade churn {minute}");
        append_line(&chat, minute, &churn);
        assert_published(
            &ingest_output("all", &home, &data, false),
            &format!("pass {minute} after the upgrade"),
        );
        assert!(normalize(&data).contains(&churn));
        assert_eq!(cline_projects(&data), released, "pass {minute}");
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}
