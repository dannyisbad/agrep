//! `agrep-rs why-source`: the read-only projection `agrep why` reasons over.
mod common;

use common::{copy_dir, crush_home, fixture_home, ingest_into, temp_dir, BIN};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::process::Command;

fn why_source(home: &Path, data: &Path, args: &[&str]) -> Value {
    let output = Command::new(BIN)
        .arg("why-source")
        .args(args)
        .env_clear()
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("TMPDIR", std::env::temp_dir())
        .output()
        .expect("spawn why-source");
    assert!(
        output.status.success(),
        "why-source failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.ends_with('\n') && text.trim_end().lines().count() == 1, "{text}");
    serde_json::from_str(text.trim_end()).unwrap()
}

fn data_listing(data: &Path) -> BTreeMap<String, (u64, u128)> {
    fs::read_dir(data)
        .unwrap()
        .flatten()
        .map(|entry| {
            let metadata = entry.metadata().unwrap();
            let modified = metadata
                .modified()
                .unwrap()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            (entry.file_name().to_string_lossy().into_owned(), (metadata.len(), modified))
        })
        .collect()
}

fn by_path<'a>(rows: &'a Value, path: &Path) -> Vec<&'a Value> {
    let path = path.to_string_lossy();
    rows.as_array()
        .unwrap()
        .iter()
        .filter(|row| row["path"].as_str() == Some(path.as_ref()))
        .collect()
}

/// The binary joins store roots component by component, so expectations must too: a slash
/// inside one component never equals Windows's native separator in a string comparison.
fn under(root: &Path, components: &[&str]) -> std::path::PathBuf {
    components.iter().fold(root.to_path_buf(), |path, part| path.join(part))
}

#[test]
fn projection_without_an_index_reports_missing_derived_files_and_touches_nothing() {
    let home = fixture_home("pi");
    let data = temp_dir("why-source-empty");
    let payload = why_source(&home, &data, &[]);
    assert_eq!(payload["version"], 1);
    assert_eq!(payload["home"], Value::String(home.to_string_lossy().into_owned()));
    assert_eq!(payload["cache"]["state"], "missing");
    assert_eq!(payload["cache"]["sessions"], serde_json::json!([]));
    assert_eq!(payload["intake"]["state"], "missing");
    assert_eq!(payload["intake"]["files"], serde_json::json!([]));
    assert_eq!(payload["issues"], serde_json::json!([]));
    assert_eq!(payload["tokens"], serde_json::json!([]));
    assert_eq!(payload["sources"].as_array().unwrap().len(), 5);
    for source in payload["sources"].as_array().unwrap() {
        assert_eq!(source["agent"], "pi");
        assert!(source["stat_key"].as_str().unwrap().starts_with("s:"));
        assert!(Path::new(source["path"].as_str().unwrap()).starts_with(&home));
    }
    let pi = payload["adapters"]
        .as_array()
        .unwrap()
        .iter()
        .find(|adapter| adapter["name"] == "pi")
        .unwrap();
    assert_eq!(pi["fingerprint"], "stat");
    assert_eq!(pi["roots"].as_array().unwrap().len(), 4);
    let omp_sessions = under(&home, &[".omp", "agent", "sessions"]);
    assert!(pi["roots"]
        .as_array()
        .unwrap()
        .contains(&Value::String(omp_sessions.to_string_lossy().into_owned())));
    let detected: Vec<_> = payload["detected"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| (row["name"].as_str().unwrap(), row["root"].as_str().unwrap(), row["count"].as_u64()))
        .collect();
    let copilot = under(&home, &[".copilot", "session-state"]);
    let qwen = under(&home, &[".qwen", "tmp"]);
    assert_eq!(
        detected,
        [
            ("copilot", copilot.to_str().unwrap(), Some(0)),
            ("qwen", qwen.to_str().unwrap(), Some(0)),
        ]
    );
    assert!(fs::read_dir(&data).unwrap().next().is_none(), "why-source wrote into the data dir");
    fs::remove_dir_all(data).unwrap();
}

#[test]
fn projection_after_ingest_joins_sources_cache_claims_and_intake_freshness() {
    let home = temp_dir("why-source-home");
    copy_dir(&fixture_home("pi"), &home);
    let data = temp_dir("why-source-data");
    ingest_into("pi", &home, &data, true);
    let before = data_listing(&data);
    let payload = why_source(&home, &data, &[]);
    assert_eq!(before, data_listing(&data), "why-source mutated the data dir");

    let advisor = under(
        &home,
        &[
            ".omp",
            "agent",
            "sessions",
            "-work-beta",
            "2026-02-03T04-05-06-000Z_01940000-0000-7000-8000-000000000003",
            "advisor.jsonl",
        ],
    );
    let alpha = under(
        &home,
        &[
            ".pi",
            "agent",
            "sessions",
            "-work-alpha",
            "2026-01-02T03-04-05-000Z_01930000-0000-7000-8000-000000000001.jsonl",
        ],
    );
    assert_eq!(payload["cache"]["state"], "ok");
    let claims = by_path(&payload["cache"]["sessions"], &advisor);
    assert_eq!(claims.len(), 1);
    assert_eq!(claims[0]["agent"], "pi");
    assert_eq!(claims[0]["session"], "01940000-0000-7000-8000-000000000004");
    assert_eq!(claims[0]["alias"], Value::Null);
    assert_eq!(payload["cache"]["sessions"].as_array().unwrap().len(), 5);

    assert_eq!(payload["intake"]["state"], "ok");
    let files = payload["intake"]["files"].as_array().unwrap();
    assert_eq!(files.len(), 5);
    for entry in files {
        assert_eq!(entry["fresh"], true, "{entry}");
        assert_eq!(entry["key"], entry["current_key"]);
        assert_eq!(entry["session"], Value::Null);
        assert_eq!(entry["agent"], "pi");
    }
    let alpha_entry = by_path(&payload["intake"]["files"], &alpha)[0];
    assert_eq!(alpha_entry["rows"], 3);
    assert_eq!(alpha_entry["seen"], 8);
    assert_eq!(alpha_entry["skips"]["unreferenced"], 2);
    assert_eq!(alpha_entry["errors"], 0);
    assert_eq!(alpha_entry["first_error"], Value::Null);

    let advisor_str = advisor.to_string_lossy().into_owned();
    let only = why_source(&home, &data, &["--path", &advisor_str]);
    assert_eq!(only["sources"].as_array().unwrap().len(), 1);
    assert_eq!(only["sources"][0]["path"], advisor_str);
    assert_eq!(only["cache"]["sessions"].as_array().unwrap().len(), 1);
    assert_eq!(only["intake"]["files"].as_array().unwrap().len(), 1);
    assert_eq!(only["intake"]["files"][0]["rows"], 1);

    // An appended source keeps its parse-time key but is no longer fresh.
    let mut text = fs::read_to_string(&alpha).unwrap();
    text.push_str(
        r#"{"type":"message","id":"a9","parentId":"a4","timestamp":"2026-01-02T03:04:13.000Z","message":{"role":"user","content":[{"type":"text","text":"late question"}],"timestamp":"2026-01-02T03:04:13.000Z"}}"#,
    );
    text.push('\n');
    fs::write(&alpha, text).unwrap();
    let changed = why_source(&home, &data, &[]);
    let stale = by_path(&changed["intake"]["files"], &alpha)[0];
    assert_eq!(stale["fresh"], false);
    assert_eq!(stale["key"], alpha_entry["key"]);
    assert_ne!(stale["current_key"], stale["key"]);
    assert_eq!(by_path(&changed["sources"], &alpha)[0]["stat_key"], stale["current_key"]);

    // A file whose header id differs from its filename id carries the filename id as alias.
    let renamed_dir = under(&home, &[".pi", "agent", "sessions", "-work-alias"]);
    fs::create_dir_all(&renamed_dir).unwrap();
    let renamed = renamed_dir.join("2026-06-01T00-00-00-000Z_aaaaaaaa-0000-7000-8000-00000000000a.jsonl");
    fs::write(
        &renamed,
        concat!(
            r#"{"type":"session","id":"bbbbbbbb-0000-7000-8000-00000000000b","version":3,"cwd":"/work/alias","timestamp":"2026-06-01T00:00:00.000Z"}"#,
            "\n",
            r#"{"type":"message","id":"m1","parentId":null,"timestamp":"2026-06-01T00:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"renamed header question"}],"timestamp":"2026-06-01T00:00:01.000Z"}}"#,
            "\n",
        ),
    )
    .unwrap();
    ingest_into("pi", &home, &data, false);
    let aliased = why_source(&home, &data, &[]);
    let claim = by_path(&aliased["cache"]["sessions"], &renamed);
    assert_eq!(claim.len(), 1);
    assert_eq!(claim[0]["session"], "bbbbbbbb-0000-7000-8000-00000000000b");
    assert_eq!(claim[0]["alias"], "aaaaaaaa-0000-7000-8000-00000000000a");
    assert_eq!(by_path(&aliased["intake"]["files"], &alpha)[0]["fresh"], true);

    // Durable source health rides along, flagged as durable, and obeys --path.
    let durable = serde_json::json!({
        "agent": "pi",
        "path": alpha,
        "kind": "source-read-failed",
        "reason": "durable fixture failure",
    });
    fs::write(
        data.join(".source-health.json"),
        serde_json::to_vec(&serde_json::json!({"code": "source-unreadable", "issues": [durable]})).unwrap(),
    )
    .unwrap();
    let unhealthy = why_source(&home, &data, &[]);
    let issues = unhealthy["issues"].as_array().unwrap();
    assert_eq!(issues.len(), 1);
    assert_eq!(issues[0]["path"], alpha.to_string_lossy().as_ref());
    assert_eq!(issues[0]["kind"], "source-read-failed");
    assert_eq!(issues[0]["reason"], "durable fixture failure");
    assert_eq!(issues[0]["durable"], true);
    let filtered = why_source(&home, &data, &["--path", &advisor_str]);
    assert_eq!(filtered["issues"], serde_json::json!([]));
    let kept = why_source(&home, &data, &["--path", &alpha.to_string_lossy()]);
    assert_eq!(kept["issues"].as_array().unwrap().len(), 1);

    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}

fn plant_crush_conversation(db: &Path, session: &str, text: &str) {
    fs::create_dir_all(db.parent().unwrap()).unwrap();
    let connection = rusqlite::Connection::open(db).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT, \
             updated_at INTEGER, created_at INTEGER); \
             CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT, role TEXT, parts TEXT, \
             model TEXT, created_at INTEGER, updated_at INTEGER);",
        )
        .unwrap();
    let parts = serde_json::json!([{"type": "text", "data": {"text": text}}]);
    connection
        .execute(
            "INSERT INTO sessions VALUES (?1, NULL, 'sibling', 1767349000000, 1767349000000)",
            [session],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO messages VALUES (?1, ?2, 'user', ?3, NULL, 1767349000000, 1767349000000)",
            [
                format!("{session}-m1"),
                session.to_string(),
                parts.to_string(),
            ],
        )
        .unwrap();
    connection.close().unwrap();
}

fn token_census(home: &Path, data: &Path) -> Value {
    let output = Command::new(BIN)
        .args(["stores", "--tokens"])
        .env_clear()
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("TMPDIR", std::env::temp_dir())
        .output()
        .expect("spawn stores --tokens");
    assert!(
        output.status.success(),
        "stores --tokens failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

/// Token ids carry their database path; the conversations listed for `db`, by session.
fn listed_sessions(tokens: &Value, db: &Path) -> Vec<String> {
    let mut sessions: Vec<String> = tokens
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["state"] == "token")
        .filter_map(|row| agrep_core::intake::parse_token_id(row["id"].as_str().unwrap()))
        .filter(|(path, _)| Path::new(path) == db)
        .map(|(_, session)| session)
        .collect();
    sessions.sort();
    sessions
}

/// One crush database the census cannot open leaves its siblings' conversations listed with
/// their freshness, under one issue naming that database alone, in both token projections.
#[test]
fn projection_lists_healthy_token_databases_beside_an_unreadable_sibling() {
    let home = crush_home();
    let data = temp_dir("why-source-scoped-data");
    let main = home
        .join(".local")
        .join("share")
        .join("crush")
        .join("crush.db");
    let sibling = home.join(".crush").join("crush.db");
    plant_crush_conversation(&sibling, "sib1", "sibling database question");
    ingest_into("crush", &home, &data, true);
    let seeded = ["sc1", "sc2", "sc3"].map(str::to_owned).to_vec();

    let healthy = why_source(&home, &data, &[]);
    assert_eq!(healthy["issues"], serde_json::json!([]));
    assert_eq!(listed_sessions(&healthy["tokens"], &main), seeded);
    let sibling_only = vec!["sib1".to_owned()];
    assert_eq!(listed_sessions(&healthy["tokens"], &sibling), sibling_only);
    let healthy_census = token_census(&home, &data);
    assert_eq!(listed_sessions(&healthy_census, &sibling), sibling_only);

    fs::write(&sibling, b"not a sqlite database").unwrap();
    let scoped = why_source(&home, &data, &[]);
    let census: Vec<&Value> = scoped["issues"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|issue| issue["kind"] == "token-census-unreadable")
        .collect();
    assert_eq!(census.len(), 1, "{:?}", scoped["issues"]);
    assert_eq!(census[0]["agent"], "crush");
    assert_eq!(census[0]["path"], sibling.to_string_lossy().as_ref());
    assert_eq!(census[0]["durable"], false);
    assert_eq!(listed_sessions(&scoped["tokens"], &main), seeded);
    assert!(listed_sessions(&scoped["tokens"], &sibling).is_empty());
    for entry in scoped["intake"]["files"].as_array().unwrap() {
        let path = Path::new(entry["path"].as_str().unwrap());
        if path == main {
            assert_eq!(entry["fresh"], true, "{entry}");
            assert_eq!(entry["current_key"], entry["key"]);
        } else {
            assert_eq!(path, sibling, "{entry}");
            assert_eq!(entry["fresh"], Value::Null, "{entry}");
            assert_eq!(entry["current_key"], Value::Null, "{entry}");
        }
    }
    let stores = token_census(&home, &data);
    assert_eq!(listed_sessions(&stores, &main), seeded);
    let unreadable: Vec<&Value> = stores
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["state"] == "source-unreadable")
        .collect();
    assert_eq!(unreadable.len(), 1, "{stores}");
    assert_eq!(unreadable[0]["name"], "crush");
    assert_eq!(unreadable[0]["path"], sibling.to_string_lossy().as_ref());
    assert_eq!(unreadable[0]["kind"], "token-census-unreadable");

    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}

#[cfg(unix)]
fn stores_paths(home: &Path, data: &Path) -> Value {
    let output = Command::new(BIN)
        .args(["stores", "--paths"])
        .env_clear()
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("TMPDIR", std::env::temp_dir())
        .output()
        .expect("spawn stores --paths");
    assert!(
        output.status.success(),
        "stores --paths failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

/// A registered data dir nobody may search hides its database from a stat. The index keeps
/// that database's published rows, the census lists it as unreadable beside its siblings'
/// tokens, and the store paths name it as a source issue, never as absent.
#[cfg(unix)]
#[test]
fn projection_keeps_a_database_under_an_uninspectable_data_dir_unreadable_not_absent() {
    use std::os::unix::fs::PermissionsExt;

    unsafe extern "C" {
        fn geteuid() -> u32;
    }
    if unsafe { geteuid() } == 0 {
        return;
    }
    let home = crush_home();
    let data = temp_dir("why-source-denied-data");
    let main = home
        .join(".local")
        .join("share")
        .join("crush")
        .join("crush.db");
    let project = home.join("project");
    let denied = project.join(".crush");
    let sibling = denied.join("crush.db");
    plant_crush_conversation(&sibling, "sib1", "sibling database question");
    fs::write(
        main.with_file_name("projects.json"),
        serde_json::json!({"projects": [{"path": project, "data_dir": denied}]}).to_string(),
    )
    .unwrap();
    ingest_into("crush", &home, &data, true);
    let before = fs::read_to_string(data.join("messages.jsonl")).unwrap();
    assert!(before.contains("sibling database question"));
    let seeded = ["sc1", "sc2", "sc3"].map(str::to_owned).to_vec();

    fs::set_permissions(&denied, fs::Permissions::from_mode(0o000)).unwrap();
    let result = std::panic::catch_unwind(|| {
        ingest_into("crush", &home, &data, false);
        ingest_into("crush", &home, &data, false);
        (
            fs::read_to_string(data.join("messages.jsonl")).unwrap(),
            why_source(&home, &data, &[]),
            token_census(&home, &data),
            stores_paths(&home, &data),
        )
    });
    fs::set_permissions(&denied, fs::Permissions::from_mode(0o700)).unwrap();
    let (after, scoped, census, paths) = result.unwrap();
    assert!(
        after.contains("sibling database question"),
        "the uninspectable database's rows were pruned"
    );
    assert_eq!(listed_sessions(&scoped["tokens"], &main), seeded);
    assert!(listed_sessions(&scoped["tokens"], &sibling).is_empty());
    let sibling_str = sibling.to_string_lossy().into_owned();
    let issues: Vec<&Value> = scoped["issues"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|issue| issue["path"] == sibling_str.as_str())
        .collect();
    let kinds: Vec<&str> = issues
        .iter()
        .map(|issue| issue["kind"].as_str().unwrap())
        .collect();
    for kind in [
        "token-census-unreadable",
        "source-read-failed",
        "permission-denied",
    ] {
        assert!(kinds.contains(&kind), "{kind}: {:?}", scoped["issues"]);
    }
    assert!(by_path(&scoped["sources"], &sibling).is_empty());
    assert!(!by_path(&scoped["sources"], &main).is_empty());
    for entry in by_path(&scoped["intake"]["files"], &sibling) {
        assert_eq!(entry["fresh"], Value::Null, "{entry}");
    }
    for entry in by_path(&scoped["intake"]["files"], &main) {
        assert_eq!(entry["fresh"], true, "{entry}");
    }
    assert_eq!(listed_sessions(&census, &main), seeded);
    assert!(census
        .as_array()
        .unwrap()
        .iter()
        .any(|row| row["state"] == "source-unreadable" && row["path"] == sibling_str.as_str()));
    // The walk, the census and the parse each recorded it; the listing names it once.
    let unreadable_store: Vec<&Value> = paths
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["name"] == "crush" && row["path"] == sibling_str.as_str())
        .collect();
    assert_eq!(unreadable_store.len(), 1, "{paths}");
    assert_eq!(unreadable_store[0]["state"], "source-unreadable");
    assert_eq!(unreadable_store[0]["kind"], "permission-denied");

    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}

/// A store behind a dotfiles link that turns into a loop (or points at a file) is misconfigured,
/// not deleted: every pass keeps its published rows, names the root unreadable, and a cold
/// `--full` or a plain pass after cache loss is refused rather than publishing nothing.
#[cfg(unix)]
#[test]
fn projection_keeps_a_store_behind_a_looping_link_unreadable_not_absent() {
    use std::os::unix::fs::symlink;

    let home = temp_dir("why-source-loop-home");
    let data = temp_dir("why-source-loop-data");
    let real = home.join("dotfiles").join("claude");
    copy_dir(&fixture_home("claude").join(".claude"), &real);
    let link = home.join(".claude");
    symlink(&real, &link).unwrap();
    ingest_into("claude", &home, &data, true);
    let before = fs::read_to_string(data.join("messages.jsonl")).unwrap();
    assert!(before.contains("\"agent\":\"claude\""));
    let root = link.join("projects");

    for (label, target) in [
        ("loop", home.join("claude-loop")),
        ("file", home.join("a-file")),
    ] {
        fs::remove_file(&link).unwrap();
        if label == "loop" {
            symlink(&link, &target).unwrap();
        } else {
            fs::write(&target, b"a file where the store link should point").unwrap();
        }
        symlink(&target, &link).unwrap();

        let warm = common::ingest_output("claude", &home, &data, false);
        assert!(
            warm.status.success(),
            "{label}: {}",
            String::from_utf8_lossy(&warm.stderr)
        );
        assert_eq!(
            fs::read_to_string(data.join("messages.jsonl")).unwrap(),
            before,
            "{label}"
        );
        let full = common::ingest_output("claude", &home, &data, true);
        assert!(
            !full.status.success(),
            "{label}: --full published past a looping link"
        );
        assert!(
            String::from_utf8_lossy(&full.stderr).contains("retained the old generation"),
            "{label}: {}",
            String::from_utf8_lossy(&full.stderr)
        );
        assert_eq!(
            fs::read_to_string(data.join("messages.jsonl")).unwrap(),
            before,
            "{label}"
        );
        fs::remove_file(data.join(".ingest_cache.bin")).unwrap();
        let _ = common::ingest_output("claude", &home, &data, false);
        assert_eq!(
            fs::read_to_string(data.join("messages.jsonl")).unwrap(),
            before,
            "{label}"
        );
        let payload = why_source(&home, &data, &[]);
        let issues: Vec<&Value> = payload["issues"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|issue| {
                issue["agent"] == "claude" && issue["path"] == root.to_string_lossy().as_ref()
            })
            .collect();
        assert!(!issues.is_empty(), "{label}: {}", payload["issues"]);
        assert_eq!(payload["sources"], serde_json::json!([]), "{label}");

        fs::remove_file(&link).unwrap();
        let _ = fs::remove_file(&target);
        symlink(&real, &link).unwrap();
        ingest_into("claude", &home, &data, true);
        assert_eq!(
            fs::read_to_string(data.join("messages.jsonl")).unwrap(),
            before,
            "{label}"
        );
    }

    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}

#[cfg(unix)]
#[test]
fn projection_reports_live_traversal_issues_as_not_durable() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("why-source-blocked-home");
    let data = temp_dir("why-source-blocked-data");
    let blocked = home.join(".claude/projects/project");
    fs::create_dir_all(&blocked).unwrap();
    fs::write(blocked.join("hidden.jsonl"), b"{}").unwrap();
    fs::set_permissions(&blocked, fs::Permissions::from_mode(0o000)).unwrap();
    let result = std::panic::catch_unwind(|| why_source(&home, &data, &[]));
    fs::set_permissions(&blocked, fs::Permissions::from_mode(0o700)).unwrap();
    let payload = result.unwrap();
    let issues = payload["issues"].as_array().unwrap();
    assert_eq!(issues.len(), 1);
    assert_eq!(issues[0]["agent"], "claude");
    assert_eq!(issues[0]["path"], blocked.to_string_lossy().as_ref());
    assert_eq!(issues[0]["kind"], "permission-denied");
    assert_eq!(issues[0]["durable"], false);
    assert_eq!(payload["sources"], serde_json::json!([]));
    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}
