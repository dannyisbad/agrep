//! A file at crush's store path that is not a readable crush database (foreign tables, empty, not
//! SQLite, partial schema, unreadable) is disclosed as a source issue while every other agent keeps
//! publishing, and never reads as a cleanly empty store that could converge away crush's rows.

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

/// Kinds of the disclosed source-health issues naming `db` for crush.
fn crush_issue_kinds(data: &Path, db: &Path) -> Vec<String> {
    let Ok(body) = fs::read(data.join(".source-health.json")) else {
        return Vec::new();
    };
    let health: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let db = db.to_string_lossy();
    health["issues"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|issue| issue["agent"] == "crush" && issue["path"] == db.as_ref())
        .filter_map(|issue| issue["kind"].as_str().map(str::to_owned))
        .collect()
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

    fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
    let _ = fs::remove_file(data.join(".ingest_cache.bin.journal"));
    append_churn(&claude_transcript(&home), 3);
    let lost = ingest_output("all", &home, &data, false);
    assert!(
        !lost.status.success(),
        "published past rows no cache could serve"
    );
    assert!(String::from_utf8_lossy(&lost.stderr).contains("retained the old generation"));
    assert!(
        normalize(&data).contains(CRUSH_TEXT),
        "refusal did not retain crush rows"
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
