//! `agrep-rs why-source`: the read-only projection `agrep why` reasons over.
mod common;

use common::{copy_dir, fixture_home, ingest_into, temp_dir, BIN};
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
    assert!(pi["roots"]
        .as_array()
        .unwrap()
        .contains(&Value::String(home.join(".omp/agent/sessions").to_string_lossy().into_owned())));
    let detected: Vec<_> = payload["detected"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| (row["name"].as_str().unwrap(), row["root"].as_str().unwrap(), row["count"].as_u64()))
        .collect();
    assert_eq!(
        detected,
        [
            ("copilot", home.join(".copilot/session-state").to_str().unwrap(), Some(0)),
            ("qwen", home.join(".qwen/tmp").to_str().unwrap(), Some(0)),
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

    let advisor = home.join(
        ".omp/agent/sessions/-work-beta/2026-02-03T04-05-06-000Z_01940000-0000-7000-8000-000000000003/advisor.jsonl",
    );
    let alpha = home.join(
        ".pi/agent/sessions/-work-alpha/2026-01-02T03-04-05-000Z_01930000-0000-7000-8000-000000000001.jsonl",
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
    let renamed_dir = home.join(".pi/agent/sessions/-work-alias");
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
