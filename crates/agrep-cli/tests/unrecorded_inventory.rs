//! With no recorded source inventory (0.3.2 held its snapshot back; `--emit-rows` takes none),
//! the parse cache stands in. It names token databases only: rows a whole store or partially read
//! file published uncached survive a later failed read, and one torn source freezes nothing else.

mod common;

use common::*;
use std::fs;
use std::path::{Path, PathBuf};

const CLINE_TEXT: &str = "build a cli flag parser";
const OPENCODE_TEXT: &str = "convert config to yaml";
const CHURN_SESSION: &str = "22222222-2222-4222-8222-222222222222";
const TORN_TASK: &str = r#"[{"role":"user","ts":1767348100000,"content":[{"type":"te"#;

fn cline_task(home: &Path, id: &str) -> PathBuf {
    home.join(".cline")
        .join("data")
        .join("tasks")
        .join(id)
        .join("api_conversation_history.json")
}

fn assert_published(output: &std::process::Output, context: &str) {
    assert!(
        output.status.success(),
        "{context}: exit {:?}\nstderr:\n{}",
        output.status.code(),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn disclosed(data: &Path, agent: &str) -> bool {
    fs::read_to_string(data.join(".source-health.json"))
        .is_ok_and(|health| health.contains(&format!("\"agent\":\"{agent}\"")))
}

/// A claude + cline home whose indexed task 1767348100000 is torn mid-write and stays so. Its
/// read is invalid; `readable` keeps the fixture's task 1767348000000 beside it.
fn torn_cline_home(tag: &str, readable: bool) -> PathBuf {
    let home = temp_dir(tag);
    copy_dir(&fixture_home("claude"), &home);
    copy_dir(&fixture_home("cline"), &home);
    let mut ids = vec!["1767348100000"];
    if readable {
        ids.insert(0, "1767348000000");
    } else {
        fs::remove_dir_all(cline_task(&home, "1767348000000").parent().unwrap()).unwrap();
    }
    let torn = cline_task(&home, "1767348100000");
    fs::create_dir_all(torn.parent().unwrap()).unwrap();
    fs::write(&torn, TORN_TASK).unwrap();
    let history: Vec<_> = ids
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
    home
}

/// Append a turn to a claude chat of its own and return its text.
fn churn_claude(home: &Path, minute: u32) -> String {
    let chat = home
        .join(".claude")
        .join("projects")
        .join("proj-beta")
        .join(format!("{CHURN_SESSION}.jsonl"));
    fs::create_dir_all(chat.parent().unwrap()).unwrap();
    let text = format!("claude churn {minute}");
    let row = serde_json::json!({
        "type": "user", "userType": "external", "sessionId": CHURN_SESSION,
        "timestamp": format!("2026-01-03T10:{minute:02}:00.000Z"), "cwd": "/work/beta",
        "message": {"role": "user", "content": text},
    });
    let mut body = fs::read_to_string(&chat).unwrap_or_default();
    body.push_str(&format!("{row}\n"));
    fs::write(&chat, body).unwrap();
    text
}

#[test]
fn a_torn_whole_store_task_keeps_every_pass_publishing_and_its_published_rows() {
    let home = torn_cline_home("unrecorded-cline-home", true);
    let data = temp_dir("unrecorded-cline-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    assert!(normalize(&data).contains(CLINE_TEXT));
    assert!(!data.join(".source_snapshot.bin").exists());

    for pass in 1..=4 {
        if pass == 3 {
            // The readable task tears too, as a reader racing its writer sees it.
            fs::write(cline_task(&home, "1767348000000"), TORN_TASK).unwrap();
        }
        let churn = churn_claude(&home, pass);
        let output = ingest_output("all", &home, &data, false);
        let published = normalize(&data);
        assert!(
            published.contains(CLINE_TEXT),
            "pass {pass} dropped the published cline rows (exit {:?})",
            output.status.code()
        );
        assert_published(&output, &format!("pass {pass}"));
        assert!(published.contains(&churn), "pass {pass}: {churn} froze");
        assert!(
            disclosed(&data, "cline"),
            "pass {pass}: cline not disclosed"
        );
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A store that never published a row has none at stake: a read of the published generation
/// proves it, so its torn task costs every later pass nothing.
#[test]
fn a_torn_whole_store_that_never_published_a_row_never_freezes_the_rest() {
    let home = torn_cline_home("unrecorded-cline-unpublished-home", false);
    let data = temp_dir("unrecorded-cline-unpublished-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    assert!(!data.join(".source_snapshot.bin").exists());

    for pass in 1..=2 {
        let churn = churn_claude(&home, pass);
        let output = ingest_output("all", &home, &data, false);
        assert_published(&output, &format!("pass {pass}"));
        let published = normalize(&data);
        assert!(published.contains(&churn), "pass {pass}: {churn} froze");
        assert!(!published.contains("\"agent\":\"cline\""));
        assert!(
            disclosed(&data, "cline"),
            "pass {pass}: cline not disclosed"
        );
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn a_partially_read_file_published_by_an_emit_rows_index_keeps_its_rows_once_unreadable() {
    let home = opencode_home();
    copy_dir(&fixture_home("claude"), &home);
    let db = home
        .join(".local")
        .join("share")
        .join("opencode")
        .join("opencode.db");
    // A text part caught mid-write: the read publishes the rest of the database but stays
    // partial, so it is retried rather than cached.
    rusqlite::Connection::open(&db)
        .unwrap()
        .execute(
            "INSERT INTO part VALUES('p9','m2','sess-oc-1',?1,1767348001900)",
            [r#"{"type":"text","text":"torn mid-wri"#],
        )
        .unwrap();
    let data = temp_dir("unrecorded-opencode-data");
    assert_published(
        &ingest_emit_output("all", &home, &data),
        "--emit-rows index",
    );
    assert!(normalize(&data).contains(OPENCODE_TEXT));
    assert!(!data.join(".source_snapshot.bin").exists());

    fs::write(&db, b"not a database at all\n").unwrap();
    for pass in 1..=2 {
        let output = ingest_output("all", &home, &data, false);
        assert!(
            normalize(&data).contains(OPENCODE_TEXT),
            "pass {pass} dropped the published opencode rows (exit {:?})",
            output.status.code()
        );
        assert!(
            disclosed(&data, "opencode"),
            "pass {pass}: opencode not disclosed"
        );
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}
