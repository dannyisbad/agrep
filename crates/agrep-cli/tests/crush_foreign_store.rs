//! A file at a token store's database path (crush, cursor) that is not a readable database of it
//! (foreign tables, empty, not SQLite, partial schema, unreadable) is disclosed as a source issue
//! while every other agent keeps publishing, and never reads as a cleanly empty store.

mod common;

use common::*;
use std::fs;
use std::path::{Path, PathBuf};

const CLAUDE_TEXT: &str = "how do i fix the flaky timer test";
const CRUSH_TEXT: &str = "convert the readme to asciidoc";

fn crush_db(home: &Path) -> PathBuf {
    home.join(".local")
        .join("share")
        .join("crush")
        .join("crush.db")
}

fn claude_transcript(home: &Path) -> PathBuf {
    home.join(".claude")
        .join("projects")
        .join("proj-alpha")
        .join("sess-claude-0001.jsonl")
}

fn claude_home(tag: &str) -> PathBuf {
    let home = temp_dir(tag);
    copy_dir(&fixture_home("claude"), &home);
    home
}

fn remove_database(db: &Path) {
    for suffix in ["", "-wal", "-shm", "-journal"] {
        let mut path = db.as_os_str().to_os_string();
        path.push(suffix);
        let _ = fs::remove_file(PathBuf::from(path));
    }
}

fn plant_foreign_tables(db: &Path) {
    let connection = rusqlite::Connection::open(db).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE notes(id TEXT PRIMARY KEY, body TEXT); \
             INSERT INTO notes VALUES ('n1', 'a notes app, not crush');",
        )
        .unwrap();
    connection.close().unwrap();
}

fn plant_empty_file(db: &Path) {
    fs::write(db, b"").unwrap();
}

fn plant_not_sqlite(db: &Path) {
    fs::write(
        db,
        b"plain text where crush keeps its database\n".repeat(64),
    )
    .unwrap();
}

/// crush's sessions table with a conversation in it, but no messages table to parse.
fn plant_partial_schema(db: &Path) {
    let connection = rusqlite::Connection::open(db).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT, \
             updated_at INTEGER, created_at INTEGER); \
             INSERT INTO sessions VALUES ('sp1', NULL, 'partial', 1767348000000, 1767348000000);",
        )
        .unwrap();
    connection.close().unwrap();
}

fn plant_crush_seed(db: &Path) {
    remove_database(db);
    let seed = fs::read_to_string(fixtures_dir().join("crush").join("seed.sql")).unwrap();
    let connection = rusqlite::Connection::open(db).unwrap();
    connection.execute_batch(&seed).unwrap();
    connection.close().unwrap();
}

fn append_churn(source: &Path, minute: u32) {
    let mut body = fs::read_to_string(source).unwrap();
    body.push_str(&format!(
        concat!(
            "{{\"type\":\"user\",\"userType\":\"external\",",
            "\"sessionId\":\"11111111-1111-4111-8111-111111111111\",",
            "\"timestamp\":\"2026-01-02T11:{:02}:00.000Z\",\"cwd\":\"/work/alpha\",",
            "\"message\":{{\"role\":\"user\",\"content\":\"crush probe churn {}\"}}}}\n"
        ),
        minute, minute
    ));
    fs::write(source, body).unwrap();
}

/// A second claude chat in its own project, so it can be made unreadable apart from the churn.
fn plant_claude_chat(home: &Path, text: &str) -> PathBuf {
    let project = home.join(".claude").join("projects").join("proj-beta");
    fs::create_dir_all(&project).unwrap();
    let path = project.join("22222222-2222-4222-8222-222222222222.jsonl");
    let rows = [
        serde_json::json!({
            "type": "user", "userType": "external",
            "sessionId": "22222222-2222-4222-8222-222222222222",
            "timestamp": "2026-01-03T10:00:00.000Z", "cwd": "/work/beta",
            "message": {"role": "user", "content": text},
        }),
        serde_json::json!({
            "type": "assistant", "sessionId": "22222222-2222-4222-8222-222222222222",
            "timestamp": "2026-01-03T10:00:05.000Z", "cwd": "/work/beta",
            "message": {"role": "assistant", "model": "claude-fable-5",
                        "content": [{"type": "text", "text": "noted"}]},
        }),
    ];
    let body: String = rows.iter().map(|row| format!("{row}\n")).collect();
    fs::write(&path, body).unwrap();
    path
}

/// Kinds of the disclosed source-health issues naming `path` for `agent`.
fn source_issue_kinds(data: &Path, agent: &str, path: &Path) -> Vec<String> {
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

fn crush_issue_kinds(data: &Path, db: &Path) -> Vec<String> {
    source_issue_kinds(data, "crush", db)
}

fn assert_published(output: &std::process::Output, context: &str) {
    assert!(
        output.status.success(),
        "{context}: indexing failed for every agent:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn has_crush_rows(data: &Path) -> bool {
    normalize(data).contains("\"agent\":\"crush\"")
}

#[test]
fn foreign_crush_store_does_not_block_first_index_of_other_agents() {
    let variants = [
        ("foreign-tables", plant_foreign_tables as fn(&Path)),
        ("empty-file", plant_empty_file as fn(&Path)),
        ("not-sqlite", plant_not_sqlite as fn(&Path)),
        ("partial-schema", plant_partial_schema as fn(&Path)),
    ];
    for (variant, plant) in variants {
        let home = claude_home(&format!("crush-{variant}-home"));
        let db = crush_db(&home);
        fs::create_dir_all(db.parent().unwrap()).unwrap();
        plant(&db);
        let data = temp_dir(&format!("crush-{variant}-data"));

        let first = ingest_output("all", &home, &data, false);
        assert_published(&first, variant);
        assert!(
            normalize(&data).contains(CLAUDE_TEXT),
            "{variant}: claude rows missing"
        );
        assert!(
            !has_crush_rows(&data),
            "{variant}: a foreign file invented crush rows"
        );
        assert!(
            crush_issue_kinds(&data, &db).contains(&"unsupported-file-type".to_string()),
            "{variant}: the foreign store was not disclosed: {:?}",
            crush_issue_kinds(&data, &db)
        );

        // The disclosure the first generation published must not freeze the next one.
        append_churn(&claude_transcript(&home), 1);
        let second = ingest_output("all", &home, &data, false);
        assert_published(&second, variant);
        assert!(
            normalize(&data).contains("crush probe churn 1"),
            "{variant}: churn missing"
        );
        assert!(
            !crush_issue_kinds(&data, &db).is_empty(),
            "{variant}: disclosure dropped"
        );

        plant_crush_seed(&db);
        let healed = ingest_output("all", &home, &data, false);
        assert_published(&healed, variant);
        assert!(
            normalize(&data).contains(CRUSH_TEXT),
            "{variant}: healed store not read"
        );
        assert!(
            crush_issue_kinds(&data, &db).is_empty(),
            "{variant}: stale disclosure"
        );

        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

#[test]
fn foreign_crush_store_appearing_after_a_good_generation_publishes_other_agents() {
    let home = claude_home("crush-late-foreign-home");
    let db = crush_db(&home);
    let data = temp_dir("crush-late-foreign-data");
    ingest_into("all", &home, &data, false);

    fs::create_dir_all(db.parent().unwrap()).unwrap();
    plant_foreign_tables(&db);
    for minute in [1, 2] {
        append_churn(&claude_transcript(&home), minute);
        let output = ingest_output("all", &home, &data, false);
        assert_published(&output, "late foreign store");
        assert!(normalize(&data).contains(&format!("crush probe churn {minute}")));
        assert!(!has_crush_rows(&data));
        assert!(crush_issue_kinds(&data, &db).contains(&"unsupported-file-type".to_string()));
    }

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A foreign store with nothing indexed must not turn another agent's retryable issue into a
/// refusal of every agent: the unreadable chat keeps its cached rows and the churn publishes.
#[cfg(unix)]
#[test]
fn foreign_crush_store_beside_an_unreadable_claude_chat_still_publishes_churn() {
    use std::os::unix::fs::PermissionsExt;

    let home = claude_home("crush-foreign-sibling-home");
    let db = crush_db(&home);
    fs::create_dir_all(db.parent().unwrap()).unwrap();
    plant_foreign_tables(&db);
    let chat = plant_claude_chat(&home, "beta chat that later turns unreadable");
    let data = temp_dir("crush-foreign-sibling-data");
    ingest_into("all", &home, &data, false);
    assert!(normalize(&data).contains("beta chat that later turns unreadable"));

    fs::set_permissions(&chat, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&chat).is_ok() {
        // Privileged runners ignore the mode bits; there is no denial to observe.
        fs::set_permissions(&chat, fs::Permissions::from_mode(0o600)).unwrap();
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    let mut outputs = Vec::new();
    for minute in [1, 2, 3] {
        append_churn(&claude_transcript(&home), minute);
        outputs.push((minute, ingest_output("all", &home, &data, false)));
    }
    fs::set_permissions(&chat, fs::Permissions::from_mode(0o600)).unwrap();
    for (minute, output) in &outputs {
        assert_published(output, &format!("unreadable claude chat, run {minute}"));
    }
    let published = normalize(&data);
    assert!(
        published.contains("crush probe churn 3"),
        "claude churn never published"
    );
    assert!(published.contains("beta chat that later turns unreadable"));
    assert!(!has_crush_rows(&data));
    assert!(crush_issue_kinds(&data, &db).contains(&"unsupported-file-type".to_string()));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// One run without claude's store is retried like it is without crush, not refused for crush.
#[test]
fn foreign_crush_store_beside_a_vanished_claude_root_retains_and_publishes() {
    let home = claude_home("crush-foreign-vanished-home");
    let db = crush_db(&home);
    fs::create_dir_all(db.parent().unwrap()).unwrap();
    plant_foreign_tables(&db);
    let data = temp_dir("crush-foreign-vanished-data");
    ingest_into("all", &home, &data, false);

    let projects = home.join(".claude").join("projects");
    let parked = home.join("parked-projects");
    fs::rename(&projects, &parked).unwrap();
    let vanished = ingest_output("all", &home, &data, false);
    fs::rename(&parked, &projects).unwrap();
    assert_published(&vanished, "claude root missing for one run");
    assert!(
        normalize(&data).contains(CLAUDE_TEXT),
        "one absence dropped claude rows"
    );
    assert!(crush_issue_kinds(&data, &db).contains(&"unsupported-file-type".to_string()));

    append_churn(&claude_transcript(&home), 1);
    let restored = ingest_output("all", &home, &data, false);
    assert_published(&restored, "claude root restored");
    assert!(normalize(&data).contains("crush probe churn 1"));

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

fn cursor_db(home: &Path) -> PathBuf {
    home.join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb")
}

fn plant_empty_cursor(db: &Path) {
    remove_database(db);
    fs::create_dir_all(db.parent().unwrap()).unwrap();
    let connection = rusqlite::Connection::open(db).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB); \
             CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);",
        )
        .unwrap();
    connection.close().unwrap();
}

/// An empty token store is a deletion-shaped observation: its generation publishes once a second
/// healthy pass confirms it, and only then is `agent`'s disclosure for `db` retired.
fn index_empty_store_until_published(home: &Path, data: &Path, agent: &str, db: &Path) {
    for _pass in 0..2 {
        ingest_into("all", home, data, false);
    }
    assert!(data.join(".source_snapshot.bin").exists());
    assert!(source_issue_kinds(data, agent, db).is_empty());
}

/// A database published while it held no conversation has nothing a generation could lose, so
/// once it turns foreign every run publishes beside the disclosure instead of refusing.
#[test]
fn crush_store_published_without_conversations_then_foreign_does_not_block_other_agents() {
    let home = claude_home("crush-empty-then-foreign-home");
    let db = crush_db(&home);
    plant_empty_crush(&db);
    let data = temp_dir("crush-empty-then-foreign-data");
    index_empty_store_until_published(&home, &data, "crush", &db);
    assert!(!has_crush_rows(&data));

    remove_database(&db);
    plant_foreign_tables(&db);
    for minute in [1, 2, 3] {
        append_churn(&claude_transcript(&home), minute);
        let output = ingest_output("all", &home, &data, false);
        assert_published(
            &output,
            &format!("emptied crush store turned foreign, run {minute}"),
        );
        assert!(normalize(&data).contains(&format!("crush probe churn {minute}")));
        assert!(crush_issue_kinds(&data, &db).contains(&"unsupported-file-type".to_string()));
    }

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Cursor's one database follows the same rule: unreadable on a first index, or after a
/// generation that held none of its conversations, it is disclosed and every other agent publishes.
#[test]
fn cursor_store_without_published_conversations_turning_unreadable_does_not_block_other_agents() {
    let home = claude_home("cursor-empty-then-foreign-home");
    let db = cursor_db(&home);
    fs::create_dir_all(db.parent().unwrap()).unwrap();
    plant_not_sqlite(&db);
    let data = temp_dir("cursor-empty-then-foreign-data");
    let first = ingest_output("all", &home, &data, false);
    assert_published(&first, "unreadable cursor store on a first index");
    assert!(normalize(&data).contains(CLAUDE_TEXT));
    assert!(source_issue_kinds(&data, "cursor", &db).contains(&"unsupported-file-type".to_string()));

    plant_empty_cursor(&db);
    index_empty_store_until_published(&home, &data, "cursor", &db);

    remove_database(&db);
    plant_not_sqlite(&db);
    for minute in [1, 2, 3] {
        append_churn(&claude_transcript(&home), minute);
        let output = ingest_output("all", &home, &data, false);
        assert_published(
            &output,
            &format!("emptied cursor store unreadable, run {minute}"),
        );
        assert!(normalize(&data).contains(&format!("crush probe churn {minute}")));
        assert!(
            source_issue_kinds(&data, "cursor", &db).contains(&"unsupported-file-type".to_string())
        );
    }

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Cursor conversations already indexed stay published behind an unreadable database, and with
/// the cache gone the pass refuses rather than publishing a generation without them.
#[test]
fn unreadable_cursor_store_retains_indexed_conversations_and_refuses_without_cache() {
    const CURSOR_TEXT: &str = "the login test fails every third run, figure out why";
    let home = cursor_home();
    copy_dir(&fixture_home("claude"), &home);
    let db = cursor_db(&home);
    let data = temp_dir("cursor-retained-data");
    ingest_into("all", &home, &data, false);
    assert!(normalize(&data).contains(CURSOR_TEXT));

    remove_database(&db);
    plant_not_sqlite(&db);
    for minute in [1, 2] {
        append_churn(&claude_transcript(&home), minute);
        let output = ingest_output("all", &home, &data, false);
        assert_published(
            &output,
            "unreadable cursor store with indexed conversations",
        );
        let published = normalize(&data);
        assert!(published.contains(&format!("crush probe churn {minute}")));
        assert!(
            published.contains(CURSOR_TEXT),
            "indexed cursor rows were dropped"
        );
    }

    remove_parse_cache(&data);
    append_churn(&claude_transcript(&home), 3);
    let lost = ingest_output("all", &home, &data, false);
    assert_refused(&lost, "unreadable cursor store after a lost cache");
    assert!(
        normalize(&data).contains(CURSOR_TEXT),
        "refusal did not retain cursor rows"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// Crush rows already indexed stay published while their database is foreign, and with no cache
/// left to serve them the pass refuses rather than publishing a generation without them.
#[test]
fn foreign_crush_store_retains_previously_indexed_crush_rows() {
    let home = crush_home();
    copy_dir(&fixture_home("claude"), &home);
    let db = crush_db(&home);
    let data = temp_dir("crush-retained-data");
    ingest_into("all", &home, &data, false);
    assert!(normalize(&data).contains(CRUSH_TEXT));

    remove_database(&db);
    plant_foreign_tables(&db);
    for minute in [1, 2] {
        append_churn(&claude_transcript(&home), minute);
        let output = ingest_output("all", &home, &data, false);
        assert_published(&output, "foreign store with prior rows");
        let published = normalize(&data);
        assert!(published.contains(&format!("crush probe churn {minute}")));
        assert!(
            published.contains(CRUSH_TEXT),
            "indexed crush rows were dropped"
        );
        assert!(crush_issue_kinds(&data, &db).contains(&"unsupported-file-type".to_string()));
    }

    remove_parse_cache(&data);
    append_churn(&claude_transcript(&home), 3);
    let lost = ingest_output("all", &home, &data, false);
    assert_refused(&lost, "foreign crush store after a lost cache");
    assert!(
        normalize(&data).contains(CRUSH_TEXT),
        "refusal did not retain crush rows"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A complete pass (--full, or the first pass over an upgraded parse cache) serves a durably
/// unreadable database's cached conversations exactly as a warm pass does, so it publishes the
/// other agents' changes instead of refusing every run until the database is readable.
#[test]
fn complete_pass_beside_a_foreign_crush_store_keeps_its_rows_and_publishes_churn() {
    let home = crush_home();
    copy_dir(&fixture_home("claude"), &home);
    let db = crush_db(&home);
    let data = temp_dir("crush-complete-foreign-data");
    ingest_into("all", &home, &data, false);
    remove_database(&db);
    plant_foreign_tables(&db);
    append_churn(&claude_transcript(&home), 1);
    ingest_into("all", &home, &data, false);

    append_churn(&claude_transcript(&home), 2);
    let complete = ingest_output("all", &home, &data, true);
    assert_published(&complete, "complete pass beside a foreign crush store");
    let published = normalize(&data);
    assert!(published.contains("crush probe churn 2"));
    assert!(
        published.contains(CRUSH_TEXT),
        "indexed crush rows were dropped"
    );
    assert!(crush_issue_kinds(&data, &db).contains(&"unsupported-file-type".to_string()));

    append_churn(&claude_transcript(&home), 3);
    ingest_into("all", &home, &data, false);
    let published = normalize(&data);
    assert!(published.contains("crush probe churn 3"));
    assert!(
        published.contains(CRUSH_TEXT),
        "indexed crush rows were dropped"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
#[test]
fn unreadable_crush_store_is_disclosed_and_does_not_block_other_agents() {
    use std::os::unix::fs::PermissionsExt;

    let home = claude_home("crush-denied-home");
    let db = crush_db(&home);
    fs::create_dir_all(db.parent().unwrap()).unwrap();
    plant_crush_seed(&db);
    fs::set_permissions(&db, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&db).is_ok() {
        // Privileged runners ignore the mode bits; there is no denial to observe.
        fs::set_permissions(&db, fs::Permissions::from_mode(0o600)).unwrap();
        let _ = fs::remove_dir_all(&home);
        return;
    }
    let data = temp_dir("crush-denied-data");

    let denied = ingest_output("all", &home, &data, false);
    fs::set_permissions(&db, fs::Permissions::from_mode(0o600)).unwrap();
    assert_published(&denied, "denied crush store");
    assert!(normalize(&data).contains(CLAUDE_TEXT));
    assert!(!has_crush_rows(&data));
    assert!(crush_issue_kinds(&data, &db).contains(&"permission-denied".to_string()));

    ingest_into("all", &home, &data, false);
    assert!(normalize(&data).contains(CRUSH_TEXT));
    assert!(crush_issue_kinds(&data, &db).is_empty());

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

fn plant_empty_crush(db: &Path) {
    fs::create_dir_all(db.parent().unwrap()).unwrap();
    plant_crush_seed(db);
    let connection = rusqlite::Connection::open(db).unwrap();
    connection
        .execute_batch("DELETE FROM messages; DELETE FROM sessions;")
        .unwrap();
    connection.close().unwrap();
}

#[cfg(unix)]
fn add_crush_conversation(db: &Path, session: &str, text: &str) {
    let parts = serde_json::json!([{"type": "text", "data": {"text": text}}]);
    let connection = rusqlite::Connection::open(db).unwrap();
    connection
        .execute(
            "INSERT INTO sessions VALUES (?1, NULL, 'held', 1767349000000, 1767349000000)",
            [session],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO messages VALUES (?1, ?2, 'user', ?3, 'gpt-5.5', 1767349000000, \
             1767349000000)",
            [
                format!("{session}-m1"),
                session.to_string(),
                parts.to_string(),
            ],
        )
        .unwrap();
    connection.close().unwrap();
}

#[cfg(unix)]
fn agent_rows(data: &Path, agent: &str) -> usize {
    let field = format!("\"agent\":\"{agent}\"");
    sorted_lines(&data.join("messages.jsonl"))
        .iter()
        .filter(|line| line.contains(&field))
        .count()
}

#[cfg(unix)]
fn crush_rows(data: &Path) -> usize {
    agent_rows(data, "crush")
}

/// Adds the Cursor fixture's store to an existing home.
#[cfg(unix)]
fn plant_cursor_fixture(home: &Path) {
    let fixture = cursor_home();
    copy_dir(&fixture, home);
    let _ = fs::remove_dir_all(&fixture);
}

fn remove_parse_cache(data: &Path) {
    fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
    let _ = fs::remove_file(data.join(".ingest_cache.bin.journal"));
}

/// Loses the parse cache, then changes another agent's source: an unchanged preflight would
/// take the shortcut that publishes nothing, so this pass must decide publication without it.
#[cfg(unix)]
fn index_after_losing_cache(home: &Path, data: &Path) -> std::process::Output {
    remove_parse_cache(data);
    let transcript = claude_transcript(home);
    if !transcript.exists() {
        copy_dir(&fixture_home("claude"), home);
    }
    append_churn(&transcript, 59);
    ingest_output("all", home, data, false)
}

/// Makes `path` unreadable; false when this runner ignores mode bits, so there is no denial.
#[cfg(unix)]
fn deny(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(path).is_err() {
        return true;
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    false
}

#[cfg(unix)]
fn allow(path: &Path) {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

fn assert_refused(output: &std::process::Output, context: &str) {
    assert!(
        !output.status.success(),
        "{context}: published past rows no cache could serve"
    );
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("retained the old generation"),
        "{context}: unexpected failure:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A sibling database failing the census leaves its snapshot with no conversation tokens at all,
/// which must not read as the healthy database never having published its rows.
#[cfg(unix)]
#[test]
fn sibling_census_failure_cannot_unpublish_another_databases_rows() {
    let home = crush_home();
    let indexed = crush_db(&home);
    let sibling = home.join(".crush").join("crush.db");
    plant_empty_crush(&sibling);
    let data = temp_dir("crush-sibling-census-data");
    ingest_into("all", &home, &data, false);
    assert_eq!(crush_rows(&data), 3);

    remove_database(&sibling);
    plant_not_sqlite(&sibling);
    let foreign = ingest_output("all", &home, &data, false);
    assert_published(&foreign, "foreign sibling database");
    assert_eq!(crush_rows(&data), 3);

    remove_database(&sibling);
    if !deny(&indexed) {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    let lost = index_after_losing_cache(&home, &data);
    allow(&indexed);
    assert_refused(
        &lost,
        "unreadable database after a sibling's census failure",
    );
    assert_eq!(crush_rows(&data), 3, "published crush rows were dropped");

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[cfg(unix)]
const HELD_TEXT: &str = "crush conversation published past a held snapshot";

/// An empty crush store published beside cursor, then a crush conversation that a pass holding
/// its snapshot back (a cursor rollback journal) publishes. Returns the home, data and database.
#[cfg(unix)]
fn publish_past_held_snapshot(tag: &str) -> (PathBuf, PathBuf, PathBuf) {
    let home = cursor_home();
    let crush = crush_db(&home);
    plant_empty_crush(&crush);
    let data = temp_dir(tag);
    index_empty_store_until_published(&home, &data, "crush", &crush);

    add_crush_conversation(&crush, "held-session", HELD_TEXT);
    let journal = cursor_db(&home).with_file_name("state.vscdb-journal");
    fs::write(&journal, b"hot").unwrap();
    let held = ingest_output("all", &home, &data, false);
    fs::remove_file(&journal).unwrap();
    assert_published(&held, "cursor journal holds the snapshot back");
    assert!(normalize(&data).contains(HELD_TEXT));
    assert!(data.join(".ingest_pending.bin").exists());
    (home, data, crush)
}

/// Conversations published by a pass that held its snapshot back stay published material although
/// the published snapshot still shows their database without one.
#[cfg(unix)]
#[test]
fn conversations_published_past_a_held_snapshot_survive_a_lost_cache() {
    let (home, data, crush) = publish_past_held_snapshot("crush-held-snapshot-data");

    if !deny(&crush) {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    for pass in [1, 2] {
        let served = ingest_output("all", &home, &data, false);
        assert_published(&served, &format!("unreadable crush store, run {pass}"));
        assert!(normalize(&data).contains(HELD_TEXT));
    }
    let lost = index_after_losing_cache(&home, &data);
    allow(&crush);
    assert_refused(&lost, "rows published past a held snapshot");
    assert!(
        normalize(&data).contains(HELD_TEXT),
        "published crush rows were dropped"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// One clean read of a database without its conversation is not yet a deletion. A pending
/// snapshot taken from that read lists none, yet the published rows survive a lost cache.
#[cfg(unix)]
#[test]
fn one_observed_deletion_cannot_unpublish_rows_once_the_cache_is_lost() {
    let (home, data, crush) = publish_past_held_snapshot("crush-one-deletion-data");
    let connection = rusqlite::Connection::open(&crush).unwrap();
    connection
        .execute_batch("DELETE FROM messages; DELETE FROM sessions;")
        .unwrap();
    connection.close().unwrap();
    let observed = ingest_output("all", &home, &data, false);
    assert_published(&observed, "one observation of the deletion");
    assert!(
        normalize(&data).contains(HELD_TEXT),
        "one observation deleted rows"
    );

    if !deny(&crush) {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    let lost = index_after_losing_cache(&home, &data);
    allow(&crush);
    assert_refused(&lost, "rows seen deleted once, then a lost cache");
    assert!(
        normalize(&data).contains(HELD_TEXT),
        "published crush rows were dropped"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A database first read by a pass that held its snapshot back is listed only by the pending
/// snapshot; its published conversations survive it turning unreadable with the cache gone.
#[cfg(unix)]
#[test]
fn new_database_published_past_a_held_snapshot_survives_a_lost_cache() {
    let home = cursor_home();
    let crush = crush_db(&home);
    let data = temp_dir("crush-new-database-data");
    ingest_into("all", &home, &data, false);
    assert!(data.join(".source_snapshot.bin").exists());

    fs::create_dir_all(crush.parent().unwrap()).unwrap();
    plant_crush_seed(&crush);
    let journal = cursor_db(&home).with_file_name("state.vscdb-journal");
    fs::write(&journal, b"hot").unwrap();
    let held = ingest_output("all", &home, &data, false);
    fs::remove_file(&journal).unwrap();
    assert_published(&held, "cursor journal holds the snapshot back");
    assert_eq!(crush_rows(&data), 3);
    assert!(data.join(".ingest_pending.bin").exists());

    if !deny(&crush) {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    let served = ingest_output("all", &home, &data, false);
    assert_published(&served, "unreadable new crush store");
    assert_eq!(crush_rows(&data), 3);
    let lost = index_after_losing_cache(&home, &data);
    allow(&crush);
    assert_refused(&lost, "new database's rows after a lost cache");
    assert_eq!(crush_rows(&data), 3, "published crush rows were dropped");

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A store first read by a later pass while every pass holds its snapshot back is in neither
/// snapshot; its published conversations survive it turning unreadable with the cache gone.
#[cfg(unix)]
#[test]
fn store_first_read_by_a_later_held_pass_survives_a_lost_cache() {
    let home = crush_home();
    let sibling = home.join(".crush").join("crush.db");
    plant_empty_crush(&sibling);
    let data = temp_dir("crush-later-held-data");
    for _pass in 0..2 {
        ingest_into("all", &home, &data, false);
    }
    assert_eq!(crush_rows(&data), 3);

    remove_database(&sibling);
    plant_not_sqlite(&sibling);
    let held = ingest_output("all", &home, &data, false);
    assert_published(&held, "foreign empty sibling holds the snapshot back");
    plant_cursor_fixture(&home);
    for pass in [1, 2] {
        let output = ingest_output("all", &home, &data, false);
        assert_published(
            &output,
            &format!("cursor under a held snapshot, run {pass}"),
        );
    }
    let cursor_rows = agent_rows(&data, "cursor");
    assert!(cursor_rows > 0, "cursor fixture was not published");

    plant_empty_crush(&sibling);
    let cursor = cursor_db(&home);
    if !deny(&cursor) {
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
        return;
    }
    let lost = index_after_losing_cache(&home, &data);
    allow(&cursor);
    assert_refused(&lost, "store first read by a later held pass");
    assert_eq!(
        agent_rows(&data, "cursor"),
        cursor_rows,
        "published cursor rows were dropped"
    );

    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A database added after the pending marker was written is in neither snapshot. Its published
/// conversations survive it turning unreadable with the cache gone, with the snapshot held or not
/// while it is served, and whether the record is kept, removed, or names another generation.
#[cfg(unix)]
#[test]
fn database_added_under_a_held_snapshot_survives_a_lost_cache_and_record() {
    const ADDED_TEXT: &str = "crush conversation in a database added under a held snapshot";
    let cases = [false, true].into_iter().flat_map(|held_while_served| {
        ["kept", "removed", "stale"].map(|record| (held_while_served, record))
    });
    for (held_while_served, record) in cases {
        let context = format!("{record} record, held while served: {held_while_served}");
        let home = cursor_home();
        let crush = crush_db(&home);
        fs::create_dir_all(crush.parent().unwrap()).unwrap();
        plant_crush_seed(&crush);
        let data = temp_dir("crush-added-held-data");
        ingest_into("all", &home, &data, false);

        let journal = cursor_db(&home).with_file_name("state.vscdb-journal");
        fs::write(&journal, b"hot").unwrap();
        let marked = ingest_output("all", &home, &data, false);
        assert_published(&marked, "cursor journal writes the pending marker");
        assert!(data.join(".ingest_pending.bin").exists());
        let added = home.join(".crush").join("crush.db");
        plant_empty_crush(&added);
        add_crush_conversation(&added, "added-session", ADDED_TEXT);
        let held = ingest_output("all", &home, &data, false);
        if !held_while_served {
            fs::remove_file(&journal).unwrap();
        }
        assert_published(&held, "database added under a held snapshot");
        assert!(normalize(&data).contains(ADDED_TEXT));

        if !deny(&added) {
            let _ = fs::remove_dir_all(&home);
            let _ = fs::remove_dir_all(&data);
            return;
        }
        let served = ingest_output("all", &home, &data, false);
        let _ = fs::remove_file(&journal);
        assert_published(&served, &context);
        assert!(normalize(&data).contains(ADDED_TEXT));
        let record_path = data.join(".token_material.json");
        match record {
            "removed" => fs::remove_file(&record_path).unwrap(),
            "stale" => {
                let mut value: serde_json::Value =
                    serde_json::from_slice(&fs::read(&record_path).unwrap()).unwrap();
                value["signature"] = serde_json::json!("0:another-generation");
                fs::write(&record_path, serde_json::to_vec(&value).unwrap()).unwrap();
            }
            _ => {}
        }
        let lost = index_after_losing_cache(&home, &data);
        allow(&added);
        assert_refused(&lost, &context);
        assert!(
            normalize(&data).contains(ADDED_TEXT),
            "{context}: published crush rows were dropped"
        );

        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}
