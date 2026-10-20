//! Real-process controls: a conversation or store the index cannot read never holds back the
//! rest of the history, contributes no row, and stays disclosed until a pass reads it again.

mod common;

use common::*;
use rusqlite::Connection;
use std::fs;
use std::path::Path;
use std::process::{Command, Output};

fn messages(data: &Path) -> Vec<serde_json::Value> {
    sorted_lines(&data.join("messages.jsonl"))
        .iter()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

fn agents(data: &Path) -> std::collections::BTreeSet<String> {
    messages(data)
        .iter()
        .map(|row| row["agent"].as_str().unwrap().to_owned())
        .collect()
}

fn texts_of(data: &Path, agent: &str) -> Vec<String> {
    messages(data)
        .iter()
        .filter(|row| row["agent"] == agent)
        .map(|row| row["text"].as_str().unwrap().to_owned())
        .collect()
}

fn health(data: &Path) -> Vec<(String, String, Option<String>)> {
    let Ok(body) = fs::read(data.join(".source-health.json")) else {
        return Vec::new();
    };
    let record: serde_json::Value = serde_json::from_slice(&body).unwrap();
    record["issues"]
        .as_array()
        .unwrap()
        .iter()
        .map(|issue| {
            (
                issue["agent"].as_str().unwrap().to_owned(),
                issue["path"].as_str().unwrap().to_owned(),
                issue["session"].as_str().map(str::to_owned),
            )
        })
        .collect()
}

fn index_all(home: &Path, data: &Path) {
    let output = ingest_output("all", home, data, false);
    assert!(
        output.status.success(),
        "one unreadable conversation or store refused the index:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// `agrep-rs index --agent all <extra>`; `panic` makes the parse of that one source panic.
fn try_index(home: &Path, data: &Path, extra: &[&str], panic: Option<&Path>) -> Output {
    let mut cmd = Command::new(BIN);
    cmd.args(["index", "--agent", "all"])
        .args(extra)
        .env_clear()
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_RUNTIME_BUILD_ID", TEST_WRITER_BUILD_ID);
    if let Some(panic) = panic {
        cmd.env("AGREP_TEST_PARSE_PANIC", panic);
    }
    cmd.output().expect("spawn agrep-rs")
}

fn run_index(home: &Path, data: &Path, extra: &[&str], panic: Option<&Path>) -> Output {
    let output = try_index(home, data, extra, panic);
    assert!(
        output.status.success(),
        "{extra:?} index failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output
}

fn skipped_unchanged(output: &Output) -> bool {
    String::from_utf8_lossy(&output.stdout).contains("unchanged since last index")
}

/// A failed conversation is disclosed staleness, as a failed store is: the snapshot vouches for
/// its siblings, so an unchanged automatic pass takes the shortcut, and an explicit one re-reads.
fn settles_beside_the_failure(home: &Path, data: &Path, published: impl Fn()) {
    for pass in 0..2 {
        let output = run_index(home, data, &[], None);
        published();
        assert!(data.join(".source_snapshot.bin").exists());
        assert!(!data.join(".ingest_pending.bin").exists());
        assert_eq!(skipped_unchanged(&output), pass == 1, "pass {pass}");
    }
    let disclosure = fs::read(data.join(".source-health.json")).unwrap();
    let retried = run_index(home, data, &["--retry-failed-sources"], None);
    assert!(!skipped_unchanged(&retried));
    published();
    assert_eq!(fs::read(data.join(".source-health.json")).unwrap(), disclosure);
}

fn with_claude(home: &Path) {
    copy_dir(&fixture_home("claude"), home);
}

const BAD_COMPOSER: &str = "badbad00-0000-4000-8000-000000000000";

fn cursor_database(home: &Path) -> std::path::PathBuf {
    home.join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb")
}

/// A composer one of whose two bubbles does not parse, so the whole composer fails.
fn plant_malformed_composer(database: &Path) {
    let connection = Connection::open(database).unwrap();
    for (key, value) in [
        (
            format!("composerData:{BAD_COMPOSER}"),
            r#"{"createdAt":1767348000000,"fullConversationHeadersOnly":[{"bubbleId":"u"},{"bubbleId":"v"}]}"#,
        ),
        (
            format!("bubbleId:{BAD_COMPOSER}:u"),
            r#"{"type":1,"text":"a row of the broken composer must not leak"}"#,
        ),
        (format!("bubbleId:{BAD_COMPOSER}:v"), "{not json"),
    ] {
        connection
            .execute("INSERT INTO cursorDiskKV VALUES (?1, ?2)", [key.as_str(), value])
            .unwrap();
    }
}

#[test]
fn first_index_publishes_beside_a_malformed_cursor_composer() {
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-cursor-data");
    let database = cursor_database(&home);
    plant_malformed_composer(&database);

    settles_beside_the_failure(&home, &data, || {
        assert!(agents(&data).contains("claude"));
        let cursor = texts_of(&data, "cursor");
        assert!(!cursor.is_empty());
        assert!(cursor.iter().all(|text| !text.contains("must not leak")));
        assert_eq!(
            health(&data),
            [(
                "cursor".to_owned(),
                database.to_string_lossy().into_owned(),
                Some("badbad00-0000-4000-8000-000000000000".to_owned())
            )]
        );
    });

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

fn delete_cache(data: &Path) {
    for name in [".ingest_cache.bin", ".ingest_cache.bin.head", ".ingest_cache.bin.journal"] {
        let _ = fs::remove_file(data.join(name));
    }
}

fn corrupt_cache(data: &Path) {
    let cache = data.join(".ingest_cache.bin");
    let mut bytes = fs::read(&cache).unwrap();
    let at = bytes.len() - 8;
    bytes[at..].copy_from_slice(&[0xa5; 8]);
    fs::write(&cache, bytes).unwrap();
}

/// How a lane loses the parse cache, and the arguments of the pass that follows.
type CacheLoss = (&'static str, fn(&Path), &'static [&'static str]);

/// With no parse cache to answer for it (lost, corrupt, or `--full`), a malformed composer the
/// verified published generation never held lets the rest publish: nothing of it can be dropped.
#[test]
fn a_new_malformed_composer_publishes_past_a_lost_cache() {
    let lanes: [CacheLoss; 3] = [
        ("deleted", delete_cache, &[]),
        ("corrupt", corrupt_cache, &[]),
        ("full", |_| {}, &["--full"]),
    ];
    for (lane, lose, extra) in lanes {
        let home = cursor_home();
        with_claude(&home);
        let data = temp_dir(&format!("partial-lost-{lane}-data"));
        run_index(&home, &data, &[], None);
        let cursor_before = texts_of(&data, "cursor");
        assert!(!cursor_before.is_empty());
        lose(&data);
        plant_malformed_composer(&cursor_database(&home));
        append_claude_turn(&claude_transcript(&home), &format!("asked after the {lane} cache"));

        run_index(&home, &data, extra, None);
        let claude = texts_of(&data, "claude").join("\n");
        assert!(claude.contains(&format!("asked after the {lane} cache")), "{lane}");
        assert_eq!(texts_of(&data, "cursor"), cursor_before, "{lane}");
        assert_eq!(
            health(&data),
            [(
                "cursor".to_owned(),
                cursor_database(&home).to_string_lossy().into_owned(),
                Some(BAD_COMPOSER.to_owned())
            )],
            "{lane}"
        );
        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(home);
    }
}

const TORN_BUBBLE: &str =
    "bubbleId:c2a22222-2222-4222-8222-222222222222:b7b77777-7777-4777-8777-777777777777";

/// The remedy a refusal gives when only unreadable material with no last good copy holds it.
fn consent_remedy(agent: &str) -> String {
    format!(
        "fix or remove it in {agent}, or run `agrep reindex --full` to rebuild without it \
         (it is indexed again once readable)"
    )
}

/// Whether each source health record holds publication back.
fn holding(data: &Path) -> Vec<bool> {
    let record: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    record["issues"]
        .as_array()
        .unwrap()
        .iter()
        .map(|issue| issue["blocks_publication"] == true)
        .collect()
}

/// Case (4): published material turned unreadable while the cache is lost. The daemon's pass,
/// `agrep index` and `agrep index --full` keep the generation, name it with the remedy, and mark
/// it on source health; only a rebuild's consent publishes without it, naming it.
fn held_until_consent(home: &Path, data: &Path, agent: &str, named: &str) {
    let published = fs::read(data.join("messages.jsonl")).unwrap();
    let passes: [&[&str]; 4] = [
        CONSENT,
        &[],
        &["--retry-failed-sources"],
        &["--retry-failed-sources", "--full"],
    ];
    for (seen, extra) in passes.into_iter().enumerate() {
        let refused = try_index(home, data, extra, None);
        let stderr = String::from_utf8_lossy(&refused.stderr);
        assert!(!refused.status.success(), "{extra:?}");
        assert!(stderr.contains("retained the old generation"), "{extra:?}: {stderr}");
        assert!(stderr.contains(named), "{extra:?}: {stderr}");
        assert_eq!(fs::read(data.join("messages.jsonl")).unwrap(), published);
        // A first observation is retried, the consent's pass included; seen again, it persists.
        if seen == 0 {
            assert!(stderr.contains(PERSISTS), "{extra:?}: {stderr}");
            assert_eq!(holding(data), [false], "{extra:?}");
        } else {
            assert!(stderr.contains(&consent_remedy(agent)), "{extra:?}: {stderr}");
            assert_eq!(holding(data), [true], "{extra:?}");
        }
    }
    // Consent comes only with a full rebuild.
    assert!(!try_index(home, data, &["--drop-unreadable"], None).status.success());
    let rebuilt = run_index(home, data, CONSENT, None);
    let stdout = String::from_utf8_lossy(&rebuilt.stdout).into_owned();
    assert!(stdout.contains("rebuilt without unreadable"), "{stdout}");
    assert!(stdout.contains(named), "{stdout}");
    assert_eq!(holding(data), [false]);
}

/// A composer the published generation holds that turns malformed while the cache is lost has no
/// last good copy to serve: every pass keeps the whole published generation until a rebuild's
/// consent publishes the rest without it, and once it parses again it is indexed again.
#[test]
fn a_published_composer_turned_malformed_past_a_lost_cache_is_dropped_only_by_consent() {
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-lost-published-data");
    run_index(&home, &data, &[], None);
    let cursor_before = texts_of(&data, "cursor");
    delete_cache(&data);
    let database = Connection::open(cursor_database(&home)).unwrap();
    let whole: String = database
        .query_row("SELECT value FROM cursorDiskKV WHERE key = ?1", [TORN_BUBBLE], |row| row.get(0))
        .unwrap();
    database
        .execute("UPDATE cursorDiskKV SET value = '{not json' WHERE key = ?1", [TORN_BUBBLE])
        .unwrap();
    let transcript = claude_transcript(&home);
    append_claude_turn(&transcript, "asked while the composer is torn");

    held_until_consent(&home, &data, "cursor", "conversation c2a22222-2222-4222-8222-222222222222");
    assert!(texts_of(&data, "claude").iter().any(|text| text.contains("composer is torn")));
    let cursor = texts_of(&data, "cursor");
    assert!(!cursor.iter().any(|text| text.contains("keep chat history locally")));
    assert_eq!(cursor.len(), cursor_before.len() - 1);
    let disclosed = (
        "cursor".to_owned(),
        cursor_database(&home).to_string_lossy().into_owned(),
        Some("c2a22222-2222-4222-8222-222222222222".to_owned()),
    );
    assert_eq!(health(&data), std::slice::from_ref(&disclosed));
    // No longer published, the torn composer holds no later pass back.
    append_claude_turn(&transcript, "asked after the rebuild");
    run_index(&home, &data, &[], None);
    assert!(texts_of(&data, "claude").iter().any(|text| text.contains("after the rebuild")));
    assert_eq!(health(&data), [disclosed]);

    database
        .execute("UPDATE cursorDiskKV SET value = ?1 WHERE key = ?2", [whole.as_str(), TORN_BUBBLE])
        .unwrap();
    run_index(&home, &data, &["--retry-failed-sources"], None);
    assert_eq!(texts_of(&data, "cursor"), cursor_before);
    assert!(health(&data).is_empty());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// Case (4) for a per-file source: a published transcript turned unreadable while the cache is
/// lost holds every pass back until a rebuild's consent; readable again, it is indexed again.
#[cfg(unix)]
#[test]
fn a_published_transcript_turned_unreadable_past_a_lost_cache_is_dropped_only_by_consent() {
    use std::os::unix::fs::PermissionsExt as _;
    let home = temp_dir("partial-lost-transcript-home");
    with_claude(&home);
    let data = temp_dir("partial-lost-transcript-data");
    let (beta, session) = plant_beta_transcript(&home);
    run_index(&home, &data, &[], None);
    let published = || fs::read_to_string(data.join("messages.jsonl")).unwrap();
    assert!(published().contains(session));
    delete_cache(&data);
    fs::set_permissions(&beta, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&beta).is_ok() {
        // Permission bits do not bind this user (root): nothing to observe.
        fs::set_permissions(&beta, fs::Permissions::from_mode(0o644)).unwrap();
        return;
    }
    let transcript = claude_transcript(&home);
    append_claude_turn(&transcript, "asked while beta is unreadable");

    held_until_consent(&home, &data, "claude", &beta.to_string_lossy());
    assert!(!published().contains(session));
    assert!(texts_of(&data, "claude").iter().any(|text| text.contains("beta is unreadable")));
    assert_eq!(
        health(&data)
            .iter()
            .map(|(agent, path, _)| (agent.as_str(), Path::new(path)))
            .collect::<Vec<_>>(),
        [("claude", beta.as_path())]
    );
    append_claude_turn(&transcript, "asked after the rebuild");
    run_index(&home, &data, &[], None);
    assert!(texts_of(&data, "claude").iter().any(|text| text.contains("after the rebuild")));

    fs::set_permissions(&beta, fs::Permissions::from_mode(0o644)).unwrap();
    run_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(published().contains(session));
    assert!(health(&data).is_empty());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A transcript of its own project, beside the fixture's: (its path, its session id).
#[cfg(unix)]
fn plant_beta_transcript(home: &Path) -> (std::path::PathBuf, &'static str) {
    let project = home.join(".claude").join("projects").join("proj-beta");
    fs::create_dir_all(&project).unwrap();
    let session = "33333333-3333-4333-8333-333333333333";
    let beta = project.join(format!("{session}.jsonl"));
    let turn = serde_json::json!({
        "type": "user", "userType": "external", "sessionId": session,
        "timestamp": "2026-01-03T10:00:00.000Z", "cwd": "/work/beta",
        "message": {"role": "user", "content": "a question only the beta project asked"},
    });
    fs::write(&beta, format!("{turn}\n")).unwrap();
    (beta, session)
}

#[test]
fn first_index_publishes_beside_a_malformed_crush_session() {
    let home = crush_home();
    with_claude(&home);
    let data = temp_dir("partial-crush-data");
    let database = home
        .join(".local")
        .join("share")
        .join("crush")
        .join("crush.db");
    Connection::open(&database)
        .unwrap()
        .execute_batch(
            "INSERT INTO sessions VALUES ('sbad', NULL, 'broken', 1767349000000, 1767349000000);
             INSERT INTO messages VALUES ('sbad-m1', 'sbad', 'user',
               '[{\"type\":\"text\",\"data\":{\"text\":\"a row of the broken session must not leak\"}}]',
               NULL, 1767349000000, 1767349000000);
             INSERT INTO messages VALUES ('sbad-m2', 'sbad', 'user', '{not json', NULL,
               1767349000001, 1767349000001);",
        )
        .unwrap();

    settles_beside_the_failure(&home, &data, || {
        assert!(agents(&data).contains("claude"));
        let crush = texts_of(&data, "crush");
        assert!(!crush.is_empty());
        assert!(crush.iter().all(|text| !text.contains("must not leak")));
        assert_eq!(
            health(&data),
            [(
                "crush".to_owned(),
                database.to_string_lossy().into_owned(),
                Some("sbad".to_owned())
            )]
        );
    });

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// Search's first-run stream (`--emit-rows`, no preflight) publishes past a store whose read
/// fails, as the plain pass does: a first index has nothing published to drop.
#[test]
fn a_streamed_first_index_publishes_beside_a_store_that_fails_to_read() {
    let home = temp_dir("partial-stream-home");
    with_claude(&home);
    let data = temp_dir("partial-stream-data");
    let store = home.join(".local").join("share").join("opencode");
    fs::create_dir_all(&store).unwrap();
    let database = store.join("opencode.db");
    Connection::open(&database)
        .unwrap()
        .execute_batch("CREATE TABLE session_message(id TEXT PRIMARY KEY);")
        .unwrap();

    let output = ingest_emit_output("all", &home, &data);
    assert!(
        output.status.success(),
        "the stream refused a first index past one broken store:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(agents(&data), ["claude".to_owned()].into());
    assert_eq!(
        health(&data),
        [("opencode".to_owned(), database.to_string_lossy().into_owned(), None)]
    );

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A store whose read fails at runtime (the preflight saw a readable file) publishes nothing of
/// its own while every other agent publishes; once repaired, the next pass publishes it.
#[test]
fn a_store_that_fails_to_read_publishes_beside_healthy_agents_until_repaired() {
    let home = temp_dir("partial-store-home");
    with_claude(&home);
    let data = temp_dir("partial-store-data");
    let store = home.join(".local").join("share").join("opencode");
    fs::create_dir_all(&store).unwrap();
    let database = store.join("opencode.db");
    // The schema probe finds opencode 2.x, whose message query then cannot run.
    Connection::open(&database)
        .unwrap()
        .execute_batch("CREATE TABLE session_message(id TEXT PRIMARY KEY);")
        .unwrap();

    for pass in 0..2 {
        let output = run_index(&home, &data, &[], None);
        let published = agents(&data);
        assert!(published.contains("claude"));
        assert!(!published.contains("opencode"));
        assert_eq!(
            health(&data),
            [(
                "opencode".to_owned(),
                database.to_string_lossy().into_owned(),
                None
            )]
        );
        // The disclosure is durable, so an automatic pass over the unchanged store keeps it.
        assert_eq!(skipped_unchanged(&output), pass == 1);
    }
    // An explicit pass reads the failed store again with nothing changed; it fails the same way.
    let output = run_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(!skipped_unchanged(&output));
    assert!(!agents(&data).contains("opencode"));
    assert_eq!(health(&data).len(), 1);
    // Its published snapshot lists the store it never read: a pass over other changes still
    // publishes, and the next unchanged one keeps the shortcut.
    let transcript = claude_transcript(&home);
    append_claude_turn(&transcript, "a question asked beside the broken store");
    assert!(!skipped_unchanged(&run_index(&home, &data, &[], None)));
    assert!(texts_of(&data, "claude").iter().any(|text| text.contains("beside the broken")));
    assert_eq!(health(&data).len(), 1);
    assert!(skipped_unchanged(&run_index(&home, &data, &[], None)));

    fs::remove_file(&database).unwrap();
    let seed = fs::read_to_string(fixtures_dir().join("opencode").join("seed.sql")).unwrap();
    Connection::open(&database)
        .unwrap()
        .execute_batch(&seed)
        .unwrap();
    index_all(&home, &data);
    assert!(agents(&data).contains("opencode"));
    assert!(health(&data).is_empty());
    assert!(data.join(".source_snapshot.bin").exists());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A failure a retry heals with nothing changed (a panic standing in for a restored permission):
/// an unchanged automatic pass keeps the disclosure, an explicit pass or any pass after a change
/// reads the source again, and with nothing on record the explicit flag keeps the shortcut.
#[test]
fn an_explicit_pass_retries_a_failed_source_that_nothing_changed() {
    let home = temp_dir("partial-retry-home");
    with_claude(&home);
    copy_dir(&fixture_home("codex"), &home);
    let data = temp_dir("partial-retry-data");
    let transcript = claude_transcript(&home);
    let recorded = || {
        health(&data)
            .iter()
            .any(|(agent, path, _)| agent == "claude" && Path::new(path) == transcript)
    };
    let claude_texts = || texts_of(&data, "claude").join("\n");

    run_index(&home, &data, &[], Some(&transcript));
    assert!(recorded());
    assert!(!claude_texts().contains("flaky timer"));
    let disclosure = fs::read(data.join(".source-health.json")).unwrap();
    assert!(skipped_unchanged(&run_index(&home, &data, &[], None)));
    assert_eq!(fs::read(data.join(".source-health.json")).unwrap(), disclosure);
    assert!(!claude_texts().contains("flaky timer"));

    let retried = run_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(!skipped_unchanged(&retried));
    assert!(health(&data).is_empty());
    assert!(claude_texts().contains("flaky timer"));
    let settled = run_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(skipped_unchanged(&settled));

    append_claude_turn(&transcript, "first appended question");
    run_index(&home, &data, &[], Some(&transcript));
    assert!(recorded());
    assert!(!claude_texts().contains("first appended question"));
    append_claude_turn(&transcript, "second appended question");
    assert!(!skipped_unchanged(&run_index(&home, &data, &[], None)));
    assert!(health(&data).is_empty());
    assert!(claude_texts().contains("second appended question"));

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A pass killed between its cache commit and publication leaves a cache that no longer holds a
/// transcript the published generation still does: a later failed read of that transcript keeps
/// the generation, since the base is ahead of what it publishes.
#[test]
fn a_failed_read_after_a_killed_pass_keeps_the_rows_that_pass_saw_go() {
    let home = temp_dir("partial-killed-home");
    with_claude(&home);
    let data = temp_dir("partial-killed-data");
    let project = home.join(".claude").join("projects").join("proj-beta");
    fs::create_dir_all(&project).unwrap();
    let session = "33333333-3333-4333-8333-333333333333";
    let beta = project.join(format!("{session}.jsonl"));
    let turn = serde_json::json!({
        "type": "user", "userType": "external", "sessionId": session,
        "timestamp": "2026-01-03T10:00:00.000Z", "cwd": "/work/beta",
        "message": {"role": "user", "content": "a question only the beta project asked"},
    });
    let whole = format!("{turn}\n");
    fs::write(&beta, &whole).unwrap();
    run_index(&home, &data, &[], None);
    let published = || fs::read_to_string(data.join("messages.jsonl")).unwrap();
    assert!(published().contains(session));

    // The pass that sees the transcript gone commits its cache, then dies writing outputs.
    fs::remove_file(&beta).unwrap();
    let stats = data.join(".boundary_stats.bin");
    let _ = fs::remove_file(&stats);
    fs::create_dir_all(stats.join("blocker")).unwrap();
    assert!(!try_index(&home, &data, &[], None).status.success());
    assert!(published().contains(session));
    fs::remove_dir_all(&stats).unwrap();

    // Back torn, it cannot be read: the generation that still holds it is kept.
    fs::write(&beta, &whole[..whole.len() / 2]).unwrap();
    let torn = try_index(&home, &data, &[], None);
    assert!(
        !torn.status.success(),
        "published past a transcript the killed pass saw go:\n{}",
        String::from_utf8_lossy(&torn.stdout)
    );
    assert!(published().contains(session));

    fs::write(&beta, &whole).unwrap();
    run_index(&home, &data, &[], None);
    assert!(published().contains(session));

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// The token lane's analogue: a pass that saw a composer go dies after committing its cache, so
/// the cache no longer holds a composer the published generation still does. That composer back
/// torn keeps the generation; the base no longer answers for what was published.
#[test]
fn a_failed_composer_after_a_killed_pass_keeps_the_rows_that_pass_saw_go() {
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-killed-cursor-data");
    run_index(&home, &data, &[], None);
    let composer = "c1a11111-1111-4111-8111-111111111111";
    let published = || fs::read_to_string(data.join("messages.jsonl")).unwrap();
    let cursor_before = texts_of(&data, "cursor");
    assert!(published().contains(composer));

    let database = Connection::open(cursor_database(&home)).unwrap();
    let pattern = format!("%{composer}%");
    let rows: Vec<(String, String)> = database
        .prepare("SELECT key, value FROM cursorDiskKV WHERE key LIKE ?1 ORDER BY key")
        .unwrap()
        .query_map([pattern.as_str()], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert!(rows.len() > 2);
    let restore = |torn: bool| {
        for (index, (key, value)) in rows.iter().enumerate() {
            let value = if torn && index == 0 { "{not json" } else { value.as_str() };
            database
                .execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?1, ?2)", [key, value])
                .unwrap();
        }
    };
    assert!(rows[0].0.starts_with("bubbleId:"));

    // The pass that sees the composer gone commits its cache, then dies writing outputs.
    database
        .execute("DELETE FROM cursorDiskKV WHERE key LIKE ?1", [pattern.as_str()])
        .unwrap();
    let stats = data.join(".boundary_stats.bin");
    let _ = fs::remove_file(&stats);
    fs::create_dir_all(stats.join("blocker")).unwrap();
    assert!(!try_index(&home, &data, &[], None).status.success());
    assert!(published().contains(composer));
    fs::remove_dir_all(&stats).unwrap();

    // Back torn, it cannot be read: the generation that still holds it is kept.
    restore(true);
    let torn = try_index(&home, &data, &[], None);
    assert!(
        !torn.status.success(),
        "published past a composer the killed pass saw go:\n{}",
        String::from_utf8_lossy(&torn.stdout)
    );
    assert!(published().contains(composer));
    assert!(String::from_utf8_lossy(&torn.stderr).contains(&format!("conversation {composer}")));

    restore(false);
    run_index(&home, &data, &[], None);
    assert_eq!(texts_of(&data, "cursor"), cursor_before);

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

const CONSENT: &[&str] = &["--retry-failed-sources", "--full", "--drop-unreadable"];

/// The tail of a refusal over a first observation of unreadable material: a retry, then consent.
const PERSISTS: &str = "and will retry; if it persists, `agrep reindex --full` rebuilds without it";

/// A refusal that keeps the published generation and drops nothing: no consent remedy, no mark.
fn retried_not_dropped(output: &Output, data: &Path, published: &[u8], label: &str) {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(!output.status.success(), "{label} published:\n{stdout}");
    assert!(stderr.contains("and will retry"), "{label}: {stderr}");
    assert!(!stderr.contains("to rebuild without"), "{label}: {stderr}");
    assert!(!stdout.contains("rebuilt without"), "{label}: {stdout}");
    assert_eq!(fs::read(data.join("messages.jsonl")).unwrap(), published, "{label}");
    assert!(holding(data).iter().all(|held| !held), "{label} marked a failure");
}

fn opencode_database(home: &Path) -> std::path::PathBuf {
    home.join(".local/share/opencode/opencode.db")
}

/// An agent, the fixture home holding its store, the database's path under that home, and the
/// passes its failure takes to settle into one stable record set.
#[cfg(unix)]
type CorruptStore = (&'static str, fn() -> std::path::PathBuf, &'static str, usize);

/// A durable store defect: retried on its first observation, the consent's pass included; seen
/// again the same way it persists, gets the consent remedy and its mark, and the consent then
/// leaves it out. Repaired, the next explicit pass reads it again.
#[cfg(unix)]
fn left_out_once_it_persists(
    home: &Path,
    data: &Path,
    agent: &str,
    named: &str,
    settle: usize,
    damage: impl FnOnce(),
    repair: impl FnOnce(),
) {
    run_index(home, data, &[], None);
    let before = texts_of(data, agent);
    assert!(!before.is_empty(), "{agent}");
    let published = fs::read(data.join("messages.jsonl")).unwrap();
    delete_cache(data);
    damage();
    let first = try_index(home, data, CONSENT, None);
    let stderr = String::from_utf8_lossy(&first.stderr);
    assert!(!first.status.success(), "{agent}");
    assert!(stderr.contains(PERSISTS), "{agent}: {stderr}");
    // The refusal's label names what failed, a journal included.
    let label = stderr.lines().find(|line| line.starts_with("Error:")).unwrap_or_default();
    assert!(label.contains(named), "{agent}: {label}");
    assert!(!String::from_utf8_lossy(&first.stdout).contains("rebuilt without"));
    assert_eq!(fs::read(data.join("messages.jsonl")).unwrap(), published, "{agent}");
    assert!(holding(data).iter().all(|held| !held), "{agent}");
    for _ in 0..settle {
        let settling = try_index(home, data, &["--retry-failed-sources"], None);
        assert!(String::from_utf8_lossy(&settling.stderr).contains(PERSISTS), "{agent}");
    }
    let again = try_index(home, data, &["--retry-failed-sources"], None);
    let stderr = String::from_utf8_lossy(&again.stderr);
    assert!(!again.status.success(), "{agent}");
    // One record or several (a partly read store records two), each held by the same remedy.
    let remedy = format!("in {agent}, or run `agrep reindex --full` to rebuild without");
    assert!(stderr.contains(&remedy), "{agent}: {stderr}");
    assert!(holding(data).iter().any(|held| *held), "{agent}");
    let rebuilt = run_index(home, data, CONSENT, None);
    assert!(String::from_utf8_lossy(&rebuilt.stdout).contains("rebuilt without unreadable"));
    assert!(texts_of(data, agent).is_empty(), "{agent}");
    repair();
    run_index(home, data, &["--retry-failed-sources"], None);
    assert_eq!(texts_of(data, agent), before, "{agent}");
    assert!(health(data).is_empty(), "{agent}");
}

/// Overwrites every page but the first (the header and schema) of the database at `path`, and
/// returns its whole bytes.
#[cfg(unix)]
fn corrupt_pages(path: &Path) -> Vec<u8> {
    let whole = fs::read(path).unwrap();
    let page = match u16::from_be_bytes([whole[16], whole[17]]) {
        1 => 65536,
        size => usize::from(size),
    };
    let mut corrupt = whole.clone();
    corrupt[page..].fill(0xa5);
    fs::write(path, corrupt).unwrap();
    whole
}

/// Durable defects of a store, each left out by consent only once a later pass records it again:
/// a hot rollback journal no writer holds, a WAL the user may not read, and corrupt pages in a
/// crush or Cursor census.
#[cfg(unix)]
#[test]
fn a_durable_store_defect_is_left_out_only_once_it_persists() {
    use std::os::unix::fs::PermissionsExt as _;
    let sidecars = |home: &Path| {
        for sidecar in ["opencode.db-journal", "opencode.db-wal"] {
            let _ = fs::remove_file(opencode_database(home).with_file_name(sidecar));
        }
    };
    for label in ["hot-journal", "wal"] {
        let home = opencode_home();
        with_claude(&home);
        let data = temp_dir(&format!("partial-durable-{label}-data"));
        let database = opencode_database(&home);
        left_out_once_it_persists(
            &home,
            &data,
            "opencode",
            if label == "wal" { "" } else { "opencode.db-journal needs recovery" },
            0,
            || {
                let sidecar = if label == "wal" { "opencode.db-wal" } else { "opencode.db-journal" };
                let sidecar = database.with_file_name(sidecar);
                fs::write(&sidecar, [0xd9; 512]).unwrap();
                if label == "wal" {
                    fs::set_permissions(&sidecar, fs::Permissions::from_mode(0o000)).unwrap();
                }
            },
            || sidecars(&home),
        );
        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(home);
    }
    // opencode's corrupt pages read partly: the first pass caches what parsed, so the second
    // records a second failure of the same unchanged file, stable from the third pass on.
    let stores: [CorruptStore; 3] = [
        ("opencode", opencode_home, ".local/share/opencode/opencode.db", 1),
        ("crush", crush_home, ".local/share/crush/crush.db", 0),
        ("cursor", cursor_home, ".config/Cursor/User/globalStorage/state.vscdb", 0),
    ];
    for (agent, store_home, database, settle) in stores {
        let home = store_home();
        with_claude(&home);
        let data = temp_dir(&format!("partial-corrupt-{agent}-data"));
        let database = home.join(database);
        let whole = std::cell::RefCell::new(Vec::new());
        left_out_once_it_persists(
            &home,
            &data,
            agent,
            "",
            settle,
            || *whole.borrow_mut() = corrupt_pages(&database),
            || fs::write(&database, whole.borrow().as_slice()).unwrap(),
        );
        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(home);
    }
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-durable-cursor-journal-data");
    let journal = cursor_database(&home).with_file_name("state.vscdb-journal");
    left_out_once_it_persists(
        &home,
        &data,
        "cursor",
        "state.vscdb-journal needs recovery",
        0,
        || fs::write(&journal, [0xd9; 512]).unwrap(),
        || fs::remove_file(&journal).unwrap(),
    );
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A rollback journal no writer holds that SQLite would not roll back (empty) is no failure:
/// the store reads as usual, with nothing disclosed.
#[test]
fn an_idle_empty_journal_reads_as_usual() {
    let home = opencode_home();
    with_claude(&home);
    let data = temp_dir("partial-idle-journal-data");
    run_index(&home, &data, &[], None);
    let opencode_before = texts_of(&data, "opencode");
    delete_cache(&data);
    fs::write(opencode_database(&home).with_file_name("opencode.db-journal"), b"").unwrap();
    run_index(&home, &data, &["--retry-failed-sources", "--full"], None);
    assert_eq!(texts_of(&data, "opencode"), opencode_before);
    assert!(health(&data).is_empty());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A live writer holding its transaction open across many passes, with or without a write yet,
/// is never a persistent failure: its store is read when it can be, retried when it cannot, and
/// never marked or left out by the consent. A write leaves a rollback journal, whose writer only
/// POSIX can probe; Windows names that journal instead of promising a retry.
#[test]
fn a_long_held_write_transaction_is_never_left_out() {
    #[cfg(windows)]
    let writes = [""];
    #[cfg(not(windows))]
    let writes = ["", "UPDATE session SET time_created = time_created + 1;"];
    for write in writes {
        let home = opencode_home();
        with_claude(&home);
        let data = temp_dir("partial-held-writer-data");
        run_index(&home, &data, &[], None);
        let opencode_before = texts_of(&data, "opencode");
        delete_cache(&data);
        let writer = Connection::open(opencode_database(&home)).unwrap();
        writer.execute_batch(&format!("BEGIN IMMEDIATE; {write}")).unwrap();
        for round in 0..4 {
            let published = fs::read(data.join("messages.jsonl")).unwrap();
            let pass = try_index(&home, &data, CONSENT, None);
            if write.is_empty() {
                assert!(pass.status.success(), "{}", String::from_utf8_lossy(&pass.stderr));
            } else {
                retried_not_dropped(&pass, &data, &published, &format!("round {round}"));
                assert!(!String::from_utf8_lossy(&pass.stderr).contains("if it persists"));
                // However often it repeats, a live writer's lock asks for the next retry.
                let record: serde_json::Value = serde_json::from_slice(
                    &fs::read(data.join(".source-health.json")).unwrap(),
                )
                .unwrap();
                assert_eq!(record["issues"][0]["retry"], true, "round {round}: {record}");
            }
            assert_eq!(texts_of(&data, "opencode"), opencode_before, "round {round}");
        }
        writer.execute_batch("ROLLBACK;").unwrap();
        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(home);
    }
}

/// A store committing through its WAL faster than a snapshot can be taken moves its generation
/// under every read: the consent's rebuild keeps it, refusing to retry, or reads it whole.
#[test]
fn a_store_committing_through_its_wal_is_never_dropped() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let home = opencode_home();
    with_claude(&home);
    let data = temp_dir("partial-wal-data");
    let database = opencode_database(&home);
    Connection::open(&database)
        .unwrap()
        .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE churn(n INTEGER); INSERT INTO churn VALUES (0);")
        .unwrap();
    run_index(&home, &data, &[], None);
    let opencode_before = texts_of(&data, "opencode");
    assert!(!opencode_before.is_empty());
    let stop = std::sync::Arc::new(AtomicBool::new(false));
    let writer = {
        let (stop, database) = (stop.clone(), database.clone());
        std::thread::spawn(move || {
            let connection = Connection::open(&database).unwrap();
            while !stop.load(Ordering::Relaxed) {
                let _ = connection.execute("UPDATE churn SET n = n + 1", []);
                std::thread::sleep(std::time::Duration::from_micros(500));
            }
        })
    };
    for round in 0..4 {
        let published = fs::read(data.join("messages.jsonl")).unwrap();
        let output = try_index(&home, &data, CONSENT, None);
        if output.status.success() {
            assert!(!String::from_utf8_lossy(&output.stdout).contains("rebuilt without"));
        } else {
            retried_not_dropped(&output, &data, &published, &format!("round {round}"));
        }
        assert_eq!(texts_of(&data, "opencode"), opencode_before, "round {round}");
    }
    stop.store(true, Ordering::Relaxed);
    writer.join().unwrap();

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A WAL commit landing between the postflight snapshot's stat of the WAL and its open (as the
/// churn above can, on Linux, where a SQLite WAL's identity is read through an open) is the same
/// moving source a preflight catch is: the pass keeps the published generation, says it will
/// retry, marks the record for that retry, and drops nothing; the next pass publishes.
#[cfg(target_os = "linux")]
#[test]
fn a_wal_commit_caught_by_the_postflight_snapshot_is_retried() {
    let home = opencode_home();
    with_claude(&home);
    let data = temp_dir("partial-postflight-wal-data");
    let database = opencode_database(&home);
    // Held open to the end: closing the last connection would checkpoint the WAL away.
    let writer = Connection::open(&database).unwrap();
    writer
        .execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE churn(n INTEGER); INSERT INTO churn VALUES (0);")
        .unwrap();
    run_index(&home, &data, &[], None);
    let opencode_before = texts_of(&data, "opencode");
    let published = fs::read(data.join("messages.jsonl")).unwrap();
    let park = temp_dir("partial-postflight-wal-park");
    for step in PARK_STEPS.iter().filter(|step| **step != "postflight-open-wal") {
        fs::write(park.join(format!("{step}.go")), b"").unwrap();
    }
    let child = Command::new(BIN)
        .args(["index", "--agent", "all"])
        .args(CONSENT)
        .env_clear()
        .env("AGREP_HOME", &home)
        .env("AGREP_DATA_DIR", &data)
        .env("AGREP_RUNTIME_BUILD_ID", TEST_WRITER_BUILD_ID)
        .env("AGREP_TEST_PARK", &park)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    while !park.join("postflight-open-wal").exists() {
        assert!(std::time::Instant::now() < deadline, "the pass never stamped the WAL postflight");
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    writer.execute("UPDATE churn SET n = n + 1", []).unwrap();
    fs::write(park.join("postflight-open-wal.go"), b"").unwrap();
    let output = child.wait_with_output().unwrap();
    let stderr = String::from_utf8_lossy(&output.stderr);
    let wal = format!("{}-wal", database.display());
    assert!(
        stderr.contains(&format!(
            "{wal}: source changed while opening; retained the old generation and will retry"
        )),
        "{stderr}"
    );
    retried_not_dropped(&output, &data, &published, "postflight");
    let record: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    let issue = record["issues"]
        .as_array()
        .unwrap()
        .iter()
        .find(|issue| issue["path"] == wal.as_str())
        .unwrap_or_else(|| panic!("no record of the WAL: {record}"));
    assert_eq!(issue["retry"], true, "{record}");
    assert_eq!(texts_of(&data, "opencode"), opencode_before);
    run_index(&home, &data, &[], None);
    assert_eq!(texts_of(&data, "opencode"), opencode_before);
    drop(writer);
    let _ = fs::remove_dir_all(park);
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// Runs the consent's pass parked at `step` of Cursor's collector, lands `write` there, and
/// returns the pass's output once it finishes.
fn consent_with_cursor_write_at(home: &Path, data: &Path, step: &str, write: &str) -> Output {
    let park = temp_dir("partial-cursor-park");
    for other in ["mutex", "rename", "restore", "warm-lane", "cursor-open", "cursor-read"] {
        if other != step {
            fs::write(park.join(format!("{other}.go")), b"").unwrap();
        }
    }
    let child = Command::new(BIN)
        .args(["index", "--agent", "all"])
        .args(CONSENT)
        .env_clear()
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_RUNTIME_BUILD_ID", TEST_WRITER_BUILD_ID)
        .env("AGREP_TEST_PARK", &park)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
    while !park.join(step).exists() {
        assert!(std::time::Instant::now() < deadline, "the pass never reached {step}");
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    let writer = Connection::open(cursor_database(home)).unwrap();
    writer.execute_batch(write).unwrap();
    fs::write(park.join(format!("{step}.go")), b"").unwrap();
    let output = child.wait_with_output().unwrap();
    if !writer.is_autocommit() {
        writer.execute_batch("ROLLBACK;").unwrap();
    }
    let _ = fs::remove_dir_all(park);
    output
}

/// A writer that takes Cursor's database after the census listed it fails the collector's own
/// open; one that commits between that open and the read moves the generation under it. Neither
/// is dropped by the consent's rebuild, however often it repeats. The held writer's rollback
/// journal is retried only where its writer can be probed (POSIX); Windows names it instead.
#[test]
fn a_cursor_store_written_during_the_pass_is_never_dropped() {
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-cursor-write-data");
    run_index(&home, &data, &[], None);
    let published = fs::read(data.join("messages.jsonl")).unwrap();
    #[cfg(not(windows))]
    {
        let locked = consent_with_cursor_write_at(
            &home,
            &data,
            "cursor-open",
            "BEGIN IMMEDIATE; INSERT INTO cursorDiskKV VALUES ('churn', 'x');",
        );
        retried_not_dropped(&locked, &data, &published, "locked");
        assert!(
            String::from_utf8_lossy(&locked.stderr).contains("cursor database could not be opened")
        );
    }
    for round in 0..2 {
        let moved = consent_with_cursor_write_at(
            &home,
            &data,
            "cursor-read",
            &format!("INSERT OR REPLACE INTO cursorDiskKV VALUES ('churn', '{round}');"),
        );
        retried_not_dropped(&moved, &data, &published, &format!("moved {round}"));
        let stderr = String::from_utf8_lossy(&moved.stderr);
        assert!(stderr.contains("SQLite generation moved"), "{stderr}");
        assert!(!stderr.contains("if it persists"), "{stderr}");
    }

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A per-file source replaced faster than it can be read never fails the same way twice, so the
/// consent's rebuild keeps its published rows, whatever each pass catches.
#[test]
fn a_transcript_replaced_during_every_pass_is_never_dropped() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let home = temp_dir("partial-replace-home");
    with_claude(&home);
    copy_dir(&fixture_home("gemini"), &home);
    let data = temp_dir("partial-replace-data");
    run_index(&home, &data, &[], None);
    let gemini_before = texts_of(&data, "gemini");
    assert!(!gemini_before.is_empty());
    let session = home.join(GEMINI_SESSION);
    let body = fs::read(&session).unwrap();
    let stop = std::sync::Arc::new(AtomicBool::new(false));
    let writer = {
        let (stop, session) = (stop.clone(), session.clone());
        std::thread::spawn(move || {
            let staged = session.with_extension("staged");
            while !stop.load(Ordering::Relaxed) {
                let _ = fs::write(&staged, &body).and_then(|()| fs::rename(&staged, &session));
                std::thread::sleep(std::time::Duration::from_micros(200));
            }
        })
    };
    delete_cache(&data);
    for round in 0..6 {
        let output = try_index(&home, &data, CONSENT, None);
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(!stdout.contains("rebuilt without"), "round {round}: {stdout}");
        assert_eq!(texts_of(&data, "gemini"), gemini_before, "round {round}");
    }
    stop.store(true, Ordering::Relaxed);
    writer.join().unwrap();

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

const GEMINI_SESSION: &str =
    ".gemini/tmp/hash5555synthetic/chats/session-2026-01-02T10-00-55555555.json";

/// The consent pass with the clock a failed read's stamps are judged against pinned at `now_ns`.
fn consent_at(home: &Path, data: &Path, now_ns: Option<i64>) -> Output {
    let mut cmd = Command::new(BIN);
    cmd.args(["index", "--agent", "all"])
        .args(CONSENT)
        .env_clear()
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_RUNTIME_BUILD_ID", TEST_WRITER_BUILD_ID);
    if let Some(now_ns) = now_ns {
        cmd.env("AGREP_TEST_NOW_NS", now_ns.to_string());
    }
    cmd.output().expect("spawn agrep-rs")
}

/// On a 1-second clock, a session caught torn or empty twice within one tick has one identity
/// both times: neither catch proves it stays so, and the consent keeps its rows. Observed past
/// that tick, the same state persists from its next observation on, and the consent drops it.
#[test]
fn a_failure_caught_within_a_timestamp_tick_never_persists() {
    for (label, damaged) in [("empty", 0usize), ("torn", 1)] {
        let home = temp_dir(&format!("partial-racy-{label}-home"));
        with_claude(&home);
        copy_dir(&fixture_home("gemini"), &home);
        let data = temp_dir(&format!("partial-racy-{label}-data"));
        run_index(&home, &data, &[], None);
        let gemini_before = texts_of(&data, "gemini");
        let session = home.join(GEMINI_SESSION);
        let body = fs::read(&session).unwrap();
        delete_cache(&data);
        fs::write(&session, &body[..damaged * body.len() / 2]).unwrap();
        // A whole-second stamp is what a coarse filesystem gives: one tick is two seconds wide.
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap();
        let tick = std::time::UNIX_EPOCH + std::time::Duration::from_secs(now.as_secs());
        fs::File::options()
            .write(true)
            .open(&session)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(tick))
            .unwrap();
        let inside = i64::try_from(now.as_nanos()).unwrap();
        for catch in 0..2 {
            let caught = consent_at(&home, &data, Some(inside));
            let stderr = String::from_utf8_lossy(&caught.stderr);
            assert!(stderr.contains(PERSISTS), "{label} catch {catch}: {stderr}");
            assert!(!String::from_utf8_lossy(&caught.stdout).contains("rebuilt without"));
            assert_eq!(texts_of(&data, "gemini"), gemini_before, "{label} catch {catch}");
        }
        let past = inside + 10_000_000_000;
        let seen = consent_at(&home, &data, Some(past));
        assert!(String::from_utf8_lossy(&seen.stderr).contains(PERSISTS), "{label}");
        let dropped = consent_at(&home, &data, Some(past));
        assert!(dropped.status.success(), "{}", String::from_utf8_lossy(&dropped.stderr));
        assert!(texts_of(&data, "gemini").is_empty(), "{label}");
        let _ = fs::remove_dir_all(data);
        let _ = fs::remove_dir_all(home);
    }
}

/// A whole-file JSON source caught mid-truncate reads as nothing; with no last good copy, that is
/// no proof its published rows are gone, so even a full rebuild keeps them.
#[test]
fn a_published_json_source_caught_empty_keeps_its_rows() {
    let home = temp_dir("partial-truncate-home");
    with_claude(&home);
    copy_dir(&fixture_home("gemini"), &home);
    let data = temp_dir("partial-truncate-data");
    run_index(&home, &data, &[], None);
    let gemini_before = texts_of(&data, "gemini");
    assert!(!gemini_before.is_empty());
    let session = home.join(GEMINI_SESSION);
    let body = fs::read(&session).unwrap();
    fs::write(&session, b"").unwrap();
    let refused = try_index(&home, &data, &["--full"], None);
    assert!(!refused.status.success(), "{}", String::from_utf8_lossy(&refused.stdout));
    assert_eq!(texts_of(&data, "gemini"), gemini_before);
    fs::write(&session, body).unwrap();
    run_index(&home, &data, &["--full"], None);
    assert_eq!(texts_of(&data, "gemini"), gemini_before);

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A failure a retry may clear, published past because nothing published was at stake, asks for
/// that retry: the next automatic pass reads the store again instead of taking the shortcut.
#[test]
fn an_automatic_pass_retries_a_store_it_published_past() {
    let home = opencode_home();
    with_claude(&home);
    let data = temp_dir("partial-retry-past-data");
    let writer = Connection::open(opencode_database(&home)).unwrap();
    writer
        .execute_batch("BEGIN IMMEDIATE; UPDATE session SET time_created = time_created + 1;")
        .unwrap();
    run_index(&home, &data, &[], None);
    assert!(!agents(&data).contains("opencode"));
    let record: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    assert_eq!(record["issues"][0]["retry"], true, "{record}");
    writer.execute_batch("ROLLBACK;").unwrap();
    let retried = run_index(&home, &data, &[], None);
    assert!(!skipped_unchanged(&retried));
    assert!(agents(&data).contains("opencode"));
    assert!(health(&data).is_empty());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A mark no pass stands behind any more (the store it named reads whole) is retired by the next
/// pass, though nothing changed that would end the unchanged-source shortcut.
#[test]
fn a_stale_publication_mark_is_retired_over_unchanged_sources() {
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-stale-mark-data");
    run_index(&home, &data, &[], None);
    assert!(skipped_unchanged(&run_index(&home, &data, &[], None)));
    let marked = serde_json::json!({
        "code": "source-unreadable",
        "issues": [{
            "agent": "cursor",
            "path": cursor_database(&home).to_string_lossy(),
            "kind": "source-read-failed",
            "reason": "cursor database could not be opened",
            "blocks_publication": true,
        }],
    });
    fs::write(data.join(".source-health.json"), marked.to_string()).unwrap();
    let output = run_index(&home, &data, &[], None);
    assert!(!skipped_unchanged(&output));
    assert!(health(&data).is_empty());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// Consent rebuilds from what is readable, never an empty generation over a published one: with
/// nothing readable left it refuses, naming the material, and no pass offers that consent.
#[cfg(unix)]
#[test]
fn consent_never_publishes_an_empty_generation_over_a_published_one() {
    use std::os::unix::fs::PermissionsExt as _;
    let home = temp_dir("partial-empty-consent-home");
    let data = temp_dir("partial-empty-consent-data");
    let (beta, session) = plant_beta_transcript(&home);
    run_index(&home, &data, &[], None);
    let published = fs::read(data.join("messages.jsonl")).unwrap();
    assert!(String::from_utf8_lossy(&published).contains(session));
    delete_cache(&data);
    fs::set_permissions(&beta, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&beta).is_ok() {
        fs::set_permissions(&beta, fs::Permissions::from_mode(0o644)).unwrap();
        return;
    }
    // Nothing else was published: no rebuild helps, so the refusal gives the agent's own fix,
    // never the rebuild. Seen once it is retried; the same failure again is marked.
    let passes = [&["--retry-failed-sources", "--full"][..], &["--retry-failed-sources"]];
    for (seen, extra) in passes.into_iter().enumerate() {
        let plain = try_index(&home, &data, extra, None);
        let stderr = String::from_utf8_lossy(&plain.stderr);
        assert!(!plain.status.success());
        assert!(!stderr.contains("reindex --full"), "{stderr}");
        if seen == 0 {
            assert!(
                stderr.ends_with(" and will retry; if it persists, fix or remove it in claude\n"),
                "{stderr}"
            );
            assert!(holding(&data).iter().all(|held| !held));
            continue;
        }
        assert!(stderr.ends_with(" - fix or remove it in claude\n"), "{stderr}");
        assert_eq!(holding(&data), [true]);
        let record: serde_json::Value =
            serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
        assert_eq!(record["issues"][0]["rebuild"], false, "{record}");
    }
    let refused = try_index(&home, &data, CONSENT, None);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(!refused.status.success(), "{}", String::from_utf8_lossy(&refused.stdout));
    assert!(stderr.contains(&*beta.to_string_lossy()), "{stderr}");
    assert!(stderr.contains("would leave no history"), "{stderr}");
    assert!(stderr.contains("fix or remove it in claude"), "{stderr}");
    assert_eq!(fs::read(data.join("messages.jsonl")).unwrap(), published);
    // The refusal keeps the record holding the index, with the agent's fix as its only lever.
    assert_eq!(holding(&data), [true]);
    let record: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    assert_eq!(record["issues"][0]["rebuild"], false, "{record}");
    fs::set_permissions(&beta, fs::Permissions::from_mode(0o644)).unwrap();
    run_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(health(&data).is_empty());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// The refusal names every unreadable conversation holding it back, as status does, and its
/// remedy speaks of them all.
#[test]
fn the_refusal_names_each_unreadable_conversation() {
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-two-torn-data");
    run_index(&home, &data, &[], None);
    delete_cache(&data);
    let database = Connection::open(cursor_database(&home)).unwrap();
    let first: String = database
        .query_row(
            "SELECT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:c1a11111-%' ORDER BY key",
            [],
            |row| row.get(0),
        )
        .unwrap();
    for key in [first.as_str(), TORN_BUBBLE] {
        database
            .execute("UPDATE cursorDiskKV SET value = '{not json' WHERE key = ?1", [key])
            .unwrap();
    }
    append_claude_turn(&claude_transcript(&home), "asked while two composers are torn");
    let seen = try_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(String::from_utf8_lossy(&seen.stderr).contains(PERSISTS));
    let refused = try_index(&home, &data, &["--retry-failed-sources"], None);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(!refused.status.success());
    for composer in ["c1a11111-1111-4111-8111-111111111111", "c2a22222-2222-4222-8222-222222222222"] {
        assert!(stderr.contains(&format!("conversation {composer}")), "{stderr}");
    }
    assert!(
        stderr.contains(
            "fix or remove them in cursor, or run `agrep reindex --full` to rebuild without them \
             (they are indexed again once readable)"
        ),
        "{stderr}"
    );
    assert_eq!(holding(&data), [true, true]);
    let rebuilt = run_index(&home, &data, CONSENT, None);
    let stdout = String::from_utf8_lossy(&rebuilt.stdout);
    assert_eq!(stdout.matches("rebuilt without unreadable").count(), 2, "{stdout}");

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A published gemini session left at 0 bytes is empty content, not a retry: retried on its
/// first observation, named "file is empty", then held and left out by consent once it persists.
#[test]
fn a_session_file_left_empty_is_left_out_once_it_persists() {
    let home = temp_dir("partial-empty-gemini-home");
    with_claude(&home);
    copy_dir(&fixture_home("gemini"), &home);
    let data = temp_dir("partial-empty-gemini-data");
    run_index(&home, &data, &[], None);
    let session = home.join(GEMINI_SESSION);
    let body = fs::read(&session).unwrap();
    delete_cache(&data);
    fs::write(&session, b"").unwrap();
    let first = try_index(&home, &data, CONSENT, None);
    let stderr = String::from_utf8_lossy(&first.stderr);
    assert!(stderr.contains(PERSISTS), "{stderr}");
    assert!(stderr.contains("file is empty"), "{stderr}");
    let again = try_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(String::from_utf8_lossy(&again.stderr).contains(&consent_remedy("gemini")));
    run_index(&home, &data, CONSENT, None);
    assert!(texts_of(&data, "gemini").is_empty());
    assert!(agents(&data).contains("claude"));
    fs::write(&session, body).unwrap();
    run_index(&home, &data, &["--retry-failed-sources"], None);
    assert!(!texts_of(&data, "gemini").is_empty());
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// One stuck crush conversation is named by its own rows: another conversation written to the
/// same database between passes leaves it the same failure, so it persists and consent drops it.
#[test]
fn a_stuck_chat_in_a_busy_crush_database_persists() {
    let home = crush_home();
    with_claude(&home);
    let data = temp_dir("partial-busy-crush-data");
    let database = Connection::open(home.join(".local/share/crush/crush.db")).unwrap();
    database
        .execute_batch(
            "INSERT INTO sessions VALUES ('sbad', NULL, 'broken', 1767349000000, 1767349000000);
             INSERT INTO messages VALUES ('sbad-m1', 'sbad', 'user',
               '[{\"type\":\"text\",\"data\":{\"text\":\"the stuck chat once read whole\"}}]',
               NULL, 1767349000000, 1767349000000);",
        )
        .unwrap();
    run_index(&home, &data, &[], None);
    assert!(texts_of(&data, "crush").iter().any(|text| text.contains("once read whole")));
    delete_cache(&data);
    database
        .execute_batch(
            "INSERT INTO messages VALUES ('sbad-m2', 'sbad', 'user', '{not json', NULL,
               1767349000001, 1767349000001);",
        )
        .unwrap();
    let first = try_index(&home, &data, CONSENT, None);
    assert!(String::from_utf8_lossy(&first.stderr).contains(PERSISTS));
    database
        .execute_batch(
            "INSERT INTO sessions VALUES ('sbusy', NULL, 'busy', 1767350000000, 1767350000000);
             INSERT INTO messages VALUES ('sbusy-m1', 'sbusy', 'user',
               '[{\"type\":\"text\",\"data\":{\"text\":\"written between passes\"}}]',
               NULL, 1767350000000, 1767350000000);",
        )
        .unwrap();
    let again = try_index(&home, &data, &["--retry-failed-sources"], None);
    let stderr = String::from_utf8_lossy(&again.stderr);
    assert!(stderr.contains(&consent_remedy("crush")), "{stderr}");
    let rebuilt = run_index(&home, &data, CONSENT, None);
    assert!(String::from_utf8_lossy(&rebuilt.stdout).contains("conversation sbad"));
    assert!(texts_of(&data, "crush").iter().any(|text| text.contains("between passes")));
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A writer that holds Cursor's database when the census reads it is a lock, not a defect: the
/// refusal promises the retry that will read it. Its rollback journal's writer can be probed only
/// on POSIX; Windows names the journal instead of promising a retry.
#[cfg(not(windows))]
#[test]
fn a_cursor_store_locked_at_its_census_will_retry() {
    let home = cursor_home();
    with_claude(&home);
    let data = temp_dir("partial-census-lock-data");
    run_index(&home, &data, &[], None);
    delete_cache(&data);
    let writer = Connection::open(cursor_database(&home)).unwrap();
    writer
        .execute_batch("BEGIN IMMEDIATE; INSERT OR REPLACE INTO cursorDiskKV VALUES ('held', 'x');")
        .unwrap();
    let published = fs::read(data.join("messages.jsonl")).unwrap();
    let refused = try_index(&home, &data, &["--retry-failed-sources"], None);
    writer.execute_batch("ROLLBACK;").unwrap();
    retried_not_dropped(&refused, &data, &published, "census lock");
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A first index whose only material is a malformed chat publishes nothing, and its refusal
/// names the one lever there is: fixing the chat in its agent.
#[test]
fn a_first_index_of_only_a_malformed_chat_names_its_fix() {
    let home = cursor_home();
    let data = temp_dir("partial-only-malformed-data");
    let database = Connection::open(cursor_database(&home)).unwrap();
    database.execute_batch("DELETE FROM cursorDiskKV;").unwrap();
    drop(database);
    plant_malformed_composer(&cursor_database(&home));
    let refused = try_index(&home, &data, &[], None);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(!refused.status.success());
    assert!(stderr.contains(&format!("conversation {BAD_COMPOSER}")), "{stderr}");
    assert!(stderr.contains("fix or remove it in cursor"), "{stderr}");
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A corrupt crush database its app keeps rewriting never fails the same way twice: the refusal
/// says to stop the app before the rebuild can leave the database out, and nothing is dropped.
#[cfg(unix)]
#[test]
fn a_corrupt_store_its_agent_keeps_rewriting_says_to_stop_the_agent() {
    let home = crush_home();
    with_claude(&home);
    let data = temp_dir("partial-rewritten-corrupt-data");
    let database = home.join(".local/share/crush/crush.db");
    run_index(&home, &data, &[], None);
    let published = fs::read(data.join("messages.jsonl")).unwrap();
    delete_cache(&data);
    let whole = corrupt_pages(&database);
    let first = try_index(&home, &data, CONSENT, None);
    assert!(String::from_utf8_lossy(&first.stderr).contains(PERSISTS));
    let mut rewritten = fs::read(&database).unwrap();
    let at = rewritten.len() - 1;
    rewritten[at] ^= 0xff;
    fs::write(&database, rewritten).unwrap();
    let again = try_index(&home, &data, CONSENT, None);
    let stderr = String::from_utf8_lossy(&again.stderr);
    assert!(stderr.contains("crush keeps rewriting a database agrep reads as corrupt - stop crush"), "{stderr}");
    assert!(!String::from_utf8_lossy(&again.stdout).contains("rebuilt without"));
    assert_eq!(fs::read(data.join("messages.jsonl")).unwrap(), published);
    fs::write(&database, whole).unwrap();
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// Runs a plain pass, killing it if it outlives `limit`; None when it had to be killed.
#[cfg(unix)]
fn index_within(home: &Path, data: &Path, limit: std::time::Duration) -> Option<Output> {
    let mut child = Command::new(BIN)
        .args(["index", "--agent", "all"])
        .env_clear()
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_RUNTIME_BUILD_ID", TEST_WRITER_BUILD_ID)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + limit;
    while child.try_wait().unwrap().is_none() {
        if std::time::Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    Some(child.wait_with_output().unwrap())
}

/// A FIFO, or a link to one, where a store's rollback journal belongs fails that store's read
/// at once: the pass finishes, records the store, and publishes the other agents.
#[cfg(unix)]
#[test]
fn a_fifo_journal_never_blocks_a_pass() {
    let stores: [CorruptStore; 3] = [
        ("cursor", cursor_home, ".config/Cursor/User/globalStorage/state.vscdb", 0),
        ("opencode", opencode_home, ".local/share/opencode/opencode.db", 0),
        ("crush", crush_home, ".local/share/crush/crush.db", 0),
    ];
    for (agent, store_home, database, _) in stores {
        for linked in [false, true] {
            let home = store_home();
            with_claude(&home);
            let data = temp_dir(&format!("partial-fifo-{agent}-data"));
            let database = home.join(database);
            let mut journal = database.as_os_str().to_owned();
            journal.push("-journal");
            let journal = std::path::PathBuf::from(journal);
            let fifo = if linked { home.join("elsewhere") } else { journal.clone() };
            let made = Command::new("mkfifo").arg(&fifo).status().unwrap();
            assert!(made.success());
            if linked {
                std::os::unix::fs::symlink(&fifo, &journal).unwrap();
            }
            let output = index_within(&home, &data, std::time::Duration::from_secs(60))
                .unwrap_or_else(|| panic!("{agent} (linked {linked}): the pass blocked on the journal"));
            assert!(agents(&data).contains("claude"), "{agent}: {output:?}");
            assert!(
                health(&data).iter().any(|(held, path, _)| held == agent && Path::new(path) == database),
                "{agent} (linked {linked}): {:?}",
                health(&data)
            );
            let _ = fs::remove_dir_all(data);
            let _ = fs::remove_dir_all(home);
        }
    }
}

/// Every step a pass can park at; a test releases all but the one it kills at.
#[cfg(unix)]
const PARK_STEPS: &[&str] = &[
    "mutex",
    "rename",
    "restore",
    "warm-lane",
    "cursor-open",
    "cursor-read",
    "consent-armed",
    "consent-cached",
    "consent-commit",
    "event-proof",
    "consent-promote",
    "consent-promoted-1",
    "consent-promoted-2",
    "consent-promoted-3",
    "consent-promoted-4",
    "consent-proof",
    "consent-signature",
    "consent-signed",
    "consent-published",
    "postflight-open-wal",
];

/// The files of a published generation, by name.
#[cfg(unix)]
const GENERATION_FILES: &[&str] = &[
    "messages.jsonl",
    "replies.jsonl",
    "sessions.jsonl",
    "session_family.meta.json",
    ".ingest.sig",
    "boundary_stats.json",
];

#[cfg(unix)]
type Outputs = Vec<(String, Vec<u8>)>;

/// A claude transcript published, then unreadable with no cached copy and seen so twice: every
/// plain pass holds, and only a consented rebuild publishes without it.
#[cfg(unix)]
struct HeldTranscript {
    home: std::path::PathBuf,
    data: std::path::PathBuf,
    beta: std::path::PathBuf,
    session: &'static str,
}

#[cfg(unix)]
impl HeldTranscript {
    /// None where permission bits do not bind (root).
    fn new(label: &str) -> Option<Self> {
        Self::at(temp_dir(&format!("{label}-home")), temp_dir(&format!("{label}-data")))
    }

    fn at(home: std::path::PathBuf, data: std::path::PathBuf) -> Option<Self> {
        use std::os::unix::fs::PermissionsExt as _;
        fs::create_dir_all(&data).unwrap();
        with_claude(&home);
        let (beta, session) = plant_beta_transcript(&home);
        append_claude_tool_call(&beta, session, "2026-01-03T10:00:01.000Z", "toolu_beta01");
        run_index(&home, &data, &[], None);
        assert!(carries(&event_rows(&data), session), "the event store never carried the chat");
        delete_cache(&data);
        fs::set_permissions(&beta, fs::Permissions::from_mode(0o000)).unwrap();
        let held = Self { home, data, beta, session };
        if fs::read(&held.beta).is_ok() {
            return None;
        }
        let alpha = claude_transcript(&held.home);
        append_claude_turn(&alpha, "asked while beta is unreadable");
        let alpha_session = "11111111-1111-4111-8111-111111111111";
        append_claude_tool_call(&alpha, alpha_session, "2026-01-02T11:00:01.000Z", "toolu_alpha2");
        for _ in 0..2 {
            let refused = try_index(&held.home, &held.data, &["--retry-failed-sources"], None);
            assert!(!refused.status.success());
        }
        assert_eq!(holding(&held.data), [true]);
        Some(held)
    }

    /// A held box, and the outputs an uninterrupted consent publishes from that same box.
    fn with_reference(label: &str) -> Option<(Self, Outputs)> {
        let (home, data) = (temp_dir(&format!("{label}-home")), temp_dir(&format!("{label}-data")));
        let reference = Self::at(home.clone(), data.clone())?;
        run_index(&reference.home, &reference.data, CONSENT, None);
        assert!(reference.events_proven());
        let outputs = reference.outputs();
        drop(reference);
        Some((Self::at(home, data)?, outputs))
    }

    fn published(&self) -> bool {
        fs::read_to_string(self.data.join("messages.jsonl")).unwrap().contains(self.session)
    }

    fn plain(&self) -> Output {
        try_index(&self.home, &self.data, &[], None)
    }

    fn marker(&self) -> Option<Vec<u8>> {
        fs::read(self.data.join(".consent_pending")).ok()
    }

    fn generation(&self) -> Vec<(&'static str, Vec<u8>)> {
        GENERATION_FILES
            .iter()
            .map(|name| (*name, fs::read(self.data.join(name)).unwrap_or_default()))
            .collect()
    }

    /// The published files and the event store's rows.
    fn outputs(&self) -> Outputs {
        let mut outputs: Outputs =
            self.generation().into_iter().map(|(name, bytes)| (name.to_owned(), bytes)).collect();
        outputs.extend(
            event_rows(&self.data).into_iter().map(|(name, payload)| (format!("events/{name}"), payload)),
        );
        outputs
    }

    /// What a reader checks before serving the event store: its proof matches it.
    fn events_proven(&self) -> bool {
        let agents = agrep_core::ingest::registry::run_agents("all");
        agrep_core::cache::events_complete(&self.data.join("events"), &agents).unwrap()
    }

    fn assert_outputs(&self, expected: &Outputs, label: &str) {
        let actual = self.outputs();
        let differing: Vec<&str> = expected
            .iter()
            .zip(&actual)
            .filter(|(left, right)| left != right)
            .map(|(left, _)| left.0.as_str())
            .collect();
        assert!(
            actual.len() == expected.len() && differing.is_empty(),
            "{label}: differs from an uninterrupted consent's: {differing:?} ({} vs {} outputs)",
            actual.len(),
            expected.len()
        );
    }

    /// The next plain pass still refuses, the transcript's rows published and the mark kept,
    /// over an event store its proof still vouches for.
    fn still_held(&self, label: &str) -> Output {
        let plain = self.plain();
        assert!(!plain.status.success(), "{label}: a plain pass published the drop");
        assert!(self.published(), "{label}");
        assert_eq!(holding(&self.data), [true], "{label}: the mark was lost");
        assert!(self.events_proven(), "{label}: the event store lost its proof");
        plain
    }

    /// The consent stands once the next plain pass ran: that pass publishes, undoing nothing,
    /// a generation consistent across its files and event store, with no row of the transcript
    /// and every other row, unheld; a consent run again changes none of it.
    fn consented(&self, label: &str) -> Output {
        let plain = self.plain();
        let stdout = String::from_utf8_lossy(&plain.stdout).into_owned();
        assert!(
            plain.status.success(),
            "{label}: {stdout}{}",
            String::from_utf8_lossy(&plain.stderr)
        );
        assert!(!stdout.contains("undid"), "{label}: {stdout}");
        assert!(self.marker().is_none(), "{label}");
        assert_eq!(holding(&self.data), [false], "{label}");
        assert!(self.events_proven(), "{label}: the event store does not match its proof");
        let generation = self.generation();
        for (name, bytes) in &generation {
            let text = String::from_utf8_lossy(bytes);
            assert!(!text.contains(self.session), "{label}: {name} keeps the dropped transcript");
        }
        let messages = String::from_utf8_lossy(&generation[0].1).into_owned();
        assert!(messages.contains("asked while beta is unreadable"), "{label}");
        let signature = String::from_utf8_lossy(&generation[4].1).into_owned();
        assert_eq!(
            signature.split(':').next().unwrap(),
            messages.lines().count().to_string(),
            "{label}: the signature counts another generation"
        );
        let outputs = self.outputs();
        assert!(
            !carries(&event_rows(&self.data), self.session),
            "{label}: the event store keeps the dropped transcript"
        );
        let again = try_index(&self.home, &self.data, CONSENT, None);
        assert!(again.status.success(), "{label}: the consent could not run again");
        self.assert_outputs(&outputs, &format!("{label}, then a consent run again"));
        assert!(self.plain().status.success(), "{label}: a later pass refused");
        plain
    }

    /// A consent killed while parked at `step`; every other step runs on.
    fn kill_consent_at(&self, step: &str) {
        let park = temp_dir("partial-consent-park");
        for other in PARK_STEPS.iter().filter(|other| **other != step) {
            fs::write(park.join(format!("{other}.go")), b"").unwrap();
        }
        let mut child = self.spawn_consent(Some(&park));
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
        while !park.join(step).exists() {
            assert!(std::time::Instant::now() < deadline, "the consent never reached {step}");
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        child.kill().unwrap();
        child.wait().unwrap();
        let _ = fs::remove_dir_all(park);
    }

    /// The consent's own steps a consent reaches.
    fn consent_steps(&self) -> Vec<&'static str> {
        let park = temp_dir("partial-consent-steps");
        for step in PARK_STEPS {
            fs::write(park.join(format!("{step}.go")), b"").unwrap();
        }
        assert!(self.spawn_consent(Some(&park)).wait().unwrap().success());
        let reached = PARK_STEPS
            .iter()
            .copied()
            .filter(|step| step.starts_with("consent-") && park.join(step).exists())
            .collect();
        let _ = fs::remove_dir_all(park);
        reached
    }

    fn spawn_consent(&self, park: Option<&Path>) -> std::process::Child {
        let mut command = Command::new(BIN);
        command
            .args(["index", "--agent", "all"])
            .args(CONSENT)
            .env_clear()
            .env("AGREP_HOME", &self.home)
            .env("AGREP_DATA_DIR", &self.data)
            .env("AGREP_RUNTIME_BUILD_ID", TEST_WRITER_BUILD_ID)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        if let Some(park) = park {
            command.env("AGREP_TEST_PARK", park);
        }
        command.spawn().unwrap()
    }
}

#[cfg(unix)]
impl Drop for HeldTranscript {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt as _;
        let _ = fs::set_permissions(&self.beta, fs::Permissions::from_mode(0o644));
        let _ = fs::remove_dir_all(&self.data);
        let _ = fs::remove_dir_all(&self.home);
    }
}

/// A consented rebuild killed before its commit point, even after its parse cache committed,
/// leaves the hold as it was: the mark stays, the event store and every published file are
/// untouched and still proven, and the next plain pass still refuses. Only a consent drops it.
#[cfg(unix)]
#[test]
fn a_consent_stopped_before_its_commit_point_leaves_the_hold() {
    let Some(held) = HeldTranscript::new("partial-unpublished-consent") else {
        return;
    };
    let before = held.outputs();
    for step in ["consent-armed", "consent-cached"] {
        held.kill_consent_at(step);
        assert_eq!(held.marker().map(|body| body[0]), Some(b'1'), "{step}");
        let undone = held.still_held(step);
        let stdout = String::from_utf8_lossy(&undone.stdout);
        assert!(stdout.contains("undid a consented rebuild that stopped before publishing"));
        assert!(held.marker().is_none(), "{step}");
        held.assert_outputs(&before, step);
        held.still_held(&format!("{step}, then another plain pass"));
    }

    let rebuilt = run_index(&held.home, &held.data, CONSENT, None);
    assert!(String::from_utf8_lossy(&rebuilt.stdout).contains("rebuilt without unreadable"));
    assert!(!held.published());
    assert_eq!(holding(&held.data), [false]);
}

/// From its commit point, recorded right after its parse cache commits, a consent is never
/// rolled back: one killed anywhere after it, mid event write or even after it published, is
/// published again in full by the next plain pass, event store, proofs and boundary stats
/// equal to an uninterrupted consent's, though that pass can no longer read the transcript.
#[cfg(unix)]
#[test]
fn a_consent_interrupted_after_its_commit_point_is_completed() {
    let Some(probe) = HeldTranscript::new("partial-consent-steps") else {
        return;
    };
    let steps = probe.consent_steps();
    drop(probe);
    let committed = steps.iter().position(|step| *step == "consent-commit");
    let after_commit = committed.map_or(&[][..], |at| &steps[at..]);
    // The event write, the latest step a consent once rolled back at, comes first.
    for step in std::iter::once("event-proof").chain(after_commit.iter().copied()) {
        let (held, reference) = HeldTranscript::with_reference("partial-committed-consent").unwrap();
        held.kill_consent_at(step);
        assert_eq!(held.marker().map(|body| body[0]), Some(b'p'), "{step}");
        let completed = held.consented(step);
        assert!(
            String::from_utf8_lossy(&completed.stdout)
                .contains("completing a consented rebuild that stopped while publishing"),
            "{step}"
        );
        held.assert_outputs(&reference, step);
    }
    for step in [
        "consent-armed",
        "consent-cached",
        "consent-commit",
        "consent-promote",
        "consent-promoted-1",
        "consent-proof",
        "consent-signature",
        "consent-signed",
        "consent-published",
    ] {
        assert!(steps.contains(&step), "{step} never reached: {steps:?}");
    }

    // The record a crash right after publication leaves, put back over a published consent.
    let (held, reference) = HeldTranscript::with_reference("partial-published-consent").unwrap();
    run_index(&held.home, &held.data, CONSENT, None);
    let mut record = b"p".to_vec();
    record.extend(fs::read(held.data.join(".ingest.sig")).unwrap());
    fs::write(held.data.join(".consent_pending"), record).unwrap();
    held.consented("crash after publication");
    held.assert_outputs(&reference, "crash after publication");
}

/// An obstacle after the commit point (a directory where the session family proof lands, an
/// event store it cannot write) fails the consent and every later pass alike, never rolling it
/// back; once it is gone, the next plain pass publishes the uninterrupted consent's generation.
#[cfg(unix)]
#[test]
fn a_consent_that_fails_after_its_commit_point_is_completed_once_it_can_be() {
    use std::os::unix::fs::PermissionsExt as _;
    let Some((held, reference)) = HeldTranscript::with_reference("partial-late-meta") else {
        return;
    };
    let meta = held.data.join("session_family.meta.json");
    let _ = fs::remove_file(&meta);
    fs::create_dir(&meta).unwrap();
    let failed = try_index(&held.home, &held.data, CONSENT, None);
    let stdout = String::from_utf8_lossy(&failed.stdout);
    assert!(!failed.status.success(), "{stdout}");
    assert!(!stdout.contains("undid"), "{stdout}");
    assert!(stdout.contains("the next pass publishes it again in full"), "{stdout}");
    assert_eq!(held.marker().map(|body| body[0]), Some(b'p'));
    assert_eq!(holding(&held.data), [false]);
    fs::remove_dir(&meta).unwrap();
    held.consented("after the directory is gone");
    assert!(meta.is_file());
    held.assert_outputs(&reference, "after the directory is gone");
    drop(held);

    let (held, reference) = HeldTranscript::with_reference("partial-late-events").unwrap();
    // An event store no pass can write: neither the store nor a replacement beside it.
    let events = held.data.join("events");
    let store = events.join(".store.sqlite3");
    fs::set_permissions(&store, fs::Permissions::from_mode(0o444)).unwrap();
    fs::set_permissions(&events, fs::Permissions::from_mode(0o555)).unwrap();
    let failed = try_index(&held.home, &held.data, CONSENT, None);
    let stderr = String::from_utf8_lossy(&failed.stderr).into_owned();
    assert!(!failed.status.success());
    assert!(stderr.contains(&events.display().to_string()), "the error names no path: {stderr}");
    for pass in 0..2 {
        assert_eq!(held.marker().map(|body| body[0]), Some(b'p'), "pass {pass}");
        let plain = held.plain();
        let stdout = String::from_utf8_lossy(&plain.stdout);
        assert!(!plain.status.success(), "pass {pass}: {stdout}");
        assert!(!stdout.contains("undid"), "pass {pass}: {stdout}");
        assert!(stdout.contains("completing a consented rebuild"), "pass {pass}: {stdout}");
        assert_eq!(holding(&held.data), [false], "pass {pass}");
    }
    fs::set_permissions(&events, fs::Permissions::from_mode(0o755)).unwrap();
    fs::set_permissions(&store, fs::Permissions::from_mode(0o644)).unwrap();
    held.consented("after the event store is writable");
    held.assert_outputs(&reference, "after the event store is writable");
}

/// A consent killed anywhere in its run, at each of its phases' park steps and at timed points:
/// either it never committed, and the next plain pass still holds over the untouched, proven
/// generation, or it did, and that pass completes it to the uninterrupted consent's generation.
/// Either way no reader is left an event store its proof does not vouch for.
#[cfg(unix)]
#[test]
fn a_consent_killed_at_any_point_is_held_or_completed() {
    let Some(timed) = HeldTranscript::new("partial-consent-fuzz-timing") else {
        return;
    };
    let started = std::time::Instant::now();
    assert!(timed.spawn_consent(None).wait().unwrap().success());
    let span = started.elapsed();
    drop(timed);
    let probe = HeldTranscript::new("partial-consent-fuzz-steps").unwrap();
    let mut steps = probe.consent_steps();
    drop(probe);
    steps.insert(0, "event-proof");
    // Every phase's park step, then evenly over the run and densely over its second half.
    let rounds = 16u32;
    let delays = (0..=rounds)
        .map(|round| span * round / rounds)
        .chain((0..=2 * rounds).map(|round| span / 2 + span * round / (4 * rounds)));
    let kills = steps
        .into_iter()
        .map(|step| (Some(step), std::time::Duration::ZERO))
        .chain(delays.map(|delay| (None, delay)));
    let (mut held_rounds, mut completed_rounds) = (0, 0);
    for (round, (step, delay)) in kills.enumerate() {
        let (held, reference) = HeldTranscript::with_reference("partial-consent-fuzz").unwrap();
        let before = held.outputs();
        match step {
            Some(step) => held.kill_consent_at(step),
            None => {
                let mut child = held.spawn_consent(None);
                std::thread::sleep(delay);
                let _ = child.kill();
                child.wait().unwrap();
            }
        }
        let committed = match held.marker() {
            Some(body) => body.first() == Some(&b'p'),
            None => !held.published(),
        };
        let label = format!("round {round} ({step:?}, {delay:?})");
        if committed {
            held.consented(&label);
            completed_rounds += 1;
        } else {
            held.still_held(&label);
            held.assert_outputs(&before, &format!("{label}, held"));
            held_rounds += 1;
            let rebuilt = try_index(&held.home, &held.data, CONSENT, None);
            assert!(rebuilt.status.success(), "{label}: the consent could not finish after");
        }
        held.assert_outputs(&reference, &label);
    }
    assert!(held_rounds > 0 && completed_rounds > 0, "{held_rounds} held, {completed_rounds}");
}

fn claude_transcript(home: &Path) -> std::path::PathBuf {
    home.join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl")
}

fn append_claude_turn(transcript: &Path, text: &str) {
    let mut body = fs::read_to_string(transcript).unwrap();
    let turn = serde_json::json!({
        "type": "user", "userType": "external",
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "timestamp": "2026-01-02T11:00:00.000Z", "cwd": "/work/alpha",
        "message": {"role": "user", "content": text},
    });
    body.push_str(&format!("{turn}\n"));
    fs::write(transcript, body).unwrap();
}

/// Whether any event row, by name or payload, belongs to `session`.
#[cfg(unix)]
fn carries(rows: &[(String, Vec<u8>)], session: &str) -> bool {
    rows.iter().any(|(name, payload)| {
        name.contains(session) || String::from_utf8_lossy(payload).contains(session)
    })
}

/// A tool call answering the transcript's last turn, so the event store carries that turn.
#[cfg(unix)]
fn append_claude_tool_call(transcript: &Path, session: &str, timestamp: &str, id: &str) {
    let mut body = fs::read_to_string(transcript).unwrap();
    let call = serde_json::json!({
        "type": "assistant", "sessionId": session, "timestamp": timestamp,
        "message": {"role": "assistant", "model": "claude-fable-5", "content": [
            {"type": "tool_use", "id": id, "name": "Grep", "input": {"pattern": id}},
        ]},
    });
    body.push_str(&format!("{call}\n"));
    fs::write(transcript, body).unwrap();
}

/// Malforms a published crush conversation (`sc1`) and adds one never published (`sbad`).
fn malform_crush_chats(home: &Path, unpublished: bool) {
    let database = Connection::open(home.join(".local/share/crush/crush.db")).unwrap();
    database
        .execute_batch(
            "UPDATE messages SET parts = '{not json' WHERE id = 'm1';
             UPDATE sessions SET updated_at = updated_at + 1000 WHERE id = 'sc1';",
        )
        .unwrap();
    if unpublished {
        database
            .execute_batch(
                "INSERT INTO sessions VALUES ('sbad', NULL, 'broken', 1767349000000, 1767349000000);
                 INSERT INTO messages VALUES ('sbad-m2', 'sbad', 'user', '{not json', NULL,
                   1767349000001, 1767349000001);",
            )
            .unwrap();
    }
}

/// `agrep index --full` parses without the parse cache, so a published chat that no longer
/// parses but which that cache still serves fails its read there alone: the refusal says so and
/// sends the user to a plain `agrep index`, which publishes with it, and marks nothing as holding
/// the index back (the daemon's own --full escalation included).
#[test]
fn a_full_pass_bypassing_a_cache_that_serves_the_chat_never_holds_the_index() {
    let home = crush_home();
    with_claude(&home);
    let data = temp_dir("partial-bypassed-cache-data");
    run_index(&home, &data, &[], None);
    malform_crush_chats(&home, false);
    run_index(&home, &data, &[], None);
    for round in 0..3 {
        let refused = try_index(&home, &data, &["--full"], None);
        let stderr = String::from_utf8_lossy(&refused.stderr);
        assert!(!refused.status.success(), "round {round}");
        assert!(stderr.contains("conversation sc1"), "round {round}: {stderr}");
        assert!(
            stderr.contains("without the parse cache; retained the old generation - the parse \
                             cache this --full pass skipped still serves it; run `agrep index`, \
                             which publishes with it"),
            "round {round}: {stderr}"
        );
        assert!(!stderr.contains("no complete cache fallback"), "round {round}: {stderr}");
        assert!(!stderr.contains("reindex --full"), "round {round}: {stderr}");
        assert!(holding(&data).iter().all(|held| !held), "round {round}: marked as holding");
    }
    let published = run_index(&home, &data, &[], None);
    assert!(published.status.success());
    assert!(texts_of(&data, "crush").iter().any(|text| text.contains("convert the readme")));
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A cache a plain pass refuses (its journal unreadable: the frames past its head are lost)
/// serves nothing to a --full pass either: both take the consent path and keep the mark, instead
/// of --full sending the user to a plain pass that then holds, and clearing the mark it set.
#[cfg(unix)]
#[test]
fn a_cache_a_plain_pass_refuses_never_counts_as_serving_a_full_pass() {
    use std::os::unix::fs::PermissionsExt as _;
    let home = crush_home();
    with_claude(&home);
    let data = temp_dir("partial-refused-cache-data");
    run_index(&home, &data, &[], None);
    append_claude_turn(&claude_transcript(&home), "a turn that commits a journal frame");
    run_index(&home, &data, &[], None);
    malform_crush_chats(&home, false);
    run_index(&home, &data, &[], None);
    let journal = data.join(".ingest_cache.bin.journal");
    assert!(journal.is_file(), "no journal frames were written");
    fs::set_permissions(&journal, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&journal).is_ok() {
        fs::set_permissions(&journal, fs::Permissions::from_mode(0o644)).unwrap();
        return;
    }
    let mut marks = Vec::new();
    for argv in [&["--full"][..], &["--full"], &[], &[], &["--full"]] {
        let refused = try_index(&home, &data, argv, None);
        let stderr = String::from_utf8_lossy(&refused.stderr);
        assert!(!refused.status.success(), "{argv:?}: {stderr}");
        assert!(!stderr.contains("still serves"), "{argv:?}: {stderr}");
        marks.push(holding(&data).iter().any(|held| *held));
    }
    // Once seen to persist, every pass alike holds and keeps the mark: it never flips back.
    assert!(marks[1..].iter().all(|held| *held), "{marks:?}");
    let _ = fs::set_permissions(&journal, fs::Permissions::from_mode(0o644));
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

/// A refusal names the chat that holds the index back, the published one, never a broken chat
/// that was never published beside it, on every pass alike.
#[test]
fn a_refusal_names_the_held_chat_before_one_never_published() {
    let home = crush_home();
    with_claude(&home);
    let data = temp_dir("partial-named-held-data");
    run_index(&home, &data, &[], None);
    delete_cache(&data);
    malform_crush_chats(&home, true);
    for round in 0..3 {
        let refused = try_index(&home, &data, &["--full"], None);
        let stderr = String::from_utf8_lossy(&refused.stderr);
        assert!(!refused.status.success(), "round {round}");
        assert!(stderr.contains("conversation sc1"), "round {round}: {stderr}");
        assert!(!stderr.contains("conversation sbad"), "round {round}: {stderr}");
    }
    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}
