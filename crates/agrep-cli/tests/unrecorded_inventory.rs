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
    // partial, so every pass retries it until a read completes.
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

    let parked = home.join("opencode.db.parked");
    fs::copy(&db, &parked).unwrap();
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

    // A partial read that keeps every row publishes what it adds, as the first one did.
    fs::copy(&parked, &db).unwrap();
    rusqlite::Connection::open(&db)
        .unwrap()
        .execute_batch(concat!(
            "INSERT INTO session VALUES('sess-oc-2','/work/epsilon',NULL,NULL,1767348100000);",
            "INSERT INTO message VALUES('m9','sess-oc-2','{\"role\":\"user\"}',1767348100000);",
            "INSERT INTO part VALUES('p10','m9','sess-oc-2',",
            "'{\"type\":\"text\",\"text\":\"list the yaml keys\"}',1767348100000);"
        ))
        .unwrap();
    assert_published(&ingest_output("all", &home, &data, false), "partial pass");
    let published = normalize(&data);
    assert!(published.contains(OPENCODE_TEXT) && published.contains("list the yaml keys"));
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The first index streamed `--emit-rows`, which records no source inventory, beside a claude
/// project no pass could ever read: a directory that never read published nothing.
#[cfg(unix)]
#[test]
fn an_emit_rows_first_index_beside_a_never_read_directory_settles() {
    let home = temp_dir("unrecorded-emit-locked-home");
    copy_dir(&fixture_home("claude"), &home);
    let Some(locked) = lock_claude_project(&home) else {
        let _ = fs::remove_dir_all(&home);
        return;
    };
    let data = temp_dir("unrecorded-emit-locked-data");
    assert_published(
        &ingest_emit_output("all", &home, &data),
        "--emit-rows index",
    );
    assert!(!data.join(".source_snapshot.bin").exists());
    assert_settles_beside_a_never_read_dir(&home, &data, &locked, 1, |minute| {
        churn_claude(&home, minute)
    });
    unlock_dir(&locked);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The first plain index was killed after its derived writes, before its source snapshot: the
/// preflight it took is still the pending marker, and no source inventory was recorded.
#[cfg(unix)]
#[test]
fn a_first_index_killed_before_its_snapshot_beside_a_never_read_directory_settles() {
    let home = temp_dir("unrecorded-killed-locked-home");
    copy_dir(&fixture_home("claude"), &home);
    let Some(locked) = lock_claude_project(&home) else {
        let _ = fs::remove_dir_all(&home);
        return;
    };
    let data = temp_dir("unrecorded-killed-locked-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    fs::rename(
        data.join(".source_snapshot.bin"),
        data.join(".ingest_pending.bin"),
    )
    .unwrap();
    let _ = fs::remove_file(data.join(".harness_prefixes.snapshot"));
    assert_settles_beside_a_never_read_dir(&home, &data, &locked, 1, |minute| {
        churn_claude(&home, minute)
    });
    unlock_dir(&locked);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// An `--emit-rows` pass has no preflight, so it retires no disclosed source issue: the unchanged
/// passes after it keep the disclosure of the pass that published their generation.
#[test]
fn an_emit_rows_pass_keeps_the_disclosure_unchanged_passes_stand_on() {
    let home = temp_dir("unrecorded-emit-health-home");
    copy_dir(&fixture_home("claude"), &home);
    let foreign = home
        .join(".local")
        .join("share")
        .join("crush")
        .join("crush.db");
    fs::create_dir_all(foreign.parent().unwrap()).unwrap();
    fs::write(&foreign, b"plain text where crush keeps its database\n").unwrap();
    let kinds = |data: &Path| -> Vec<String> {
        let health: serde_json::Value =
            serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap_or_default())
                .unwrap_or_default();
        health["issues"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|issue| issue["agent"] == "crush")
            .filter_map(|issue| issue["kind"].as_str().map(str::to_owned))
            .collect()
    };
    let data = temp_dir("unrecorded-emit-health-data");
    assert_published(&ingest_output("all", &home, &data, false), "first index");
    assert!(kinds(&data).contains(&"unsupported-file-type".to_string()));

    // The first-search lane streams rows when the published messages are missing.
    fs::remove_file(data.join("messages.jsonl")).unwrap();
    assert_published(&ingest_emit_output("all", &home, &data), "--emit-rows pass");
    for pass in 1..=2 {
        assert_published(&ingest_output("all", &home, &data, false), "plain pass");
        assert!(
            kinds(&data).contains(&"unsupported-file-type".to_string()),
            "pass {pass} after --emit-rows lost the disclosure: {:?}",
            kinds(&data)
        );
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}
