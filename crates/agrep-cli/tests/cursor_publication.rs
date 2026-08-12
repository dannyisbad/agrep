//! Real-process controls for Cursor token discovery and publication refusal.

mod common;

use common::*;
use rusqlite::Connection;
use std::fs;

fn cursor_db(home: &std::path::Path) -> std::path::PathBuf {
    home.join(".config")
        .join("Cursor")
        .join("User")
        .join("globalStorage")
        .join("state.vscdb")
}

#[test]
fn cursor_schema_absent_is_a_successful_empty_publication() {
    let home = temp_dir("cursor-empty-home");
    let data = temp_dir("cursor-empty-data");
    let database = cursor_db(&home);
    fs::create_dir_all(database.parent().unwrap()).unwrap();
    let connection = Connection::open(&database).unwrap();
    connection
        .execute(
            "CREATE TABLE ItemTable(key TEXT PRIMARY KEY, value TEXT)",
            [],
        )
        .unwrap();
    connection.close().unwrap();

    let output = ingest_output("cursor", &home, &data, true);
    assert!(
        output.status.success(),
        "schema-absent Cursor store failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(data.join("messages.jsonl").is_file());
    assert!(sorted_lines(&data.join("messages.jsonl")).is_empty());
    assert!(data.join(".ingest.sig").is_file());
    assert!(data.join("sessions.jsonl").is_file());
    assert!(data.join("session_family.meta.json").is_file());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}

#[test]
fn cursor_garbage_database_is_unreadable_and_aborts_publication() {
    let home = temp_dir("cursor-garbage-home");
    let data = temp_dir("cursor-garbage-data");
    let database = cursor_db(&home);
    fs::create_dir_all(database.parent().unwrap()).unwrap();
    fs::write(&database, b"not a sqlite database\0fixture").unwrap();

    let output = ingest_output("cursor", &home, &data, true);
    assert!(
        !output.status.success(),
        "garbage Cursor store published successfully"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("cursor"),
        "adapter absent from error: {stderr}"
    );
    assert!(
        stderr.contains("state.vscdb"),
        "source path absent from error: {stderr}"
    );
    assert!(!data.join("messages.jsonl").exists());
    assert!(!data.join(".ingest.sig").exists());
    assert!(!data.join("sessions.jsonl").exists());

    let _ = fs::remove_dir_all(data);
    let _ = fs::remove_dir_all(home);
}
