mod common;

use common::{temp_dir, BIN};
use serde_json::Value;
use std::fs;
use std::path::Path;
use std::process::{Command, Output};

fn stores(home: &Path, data: &Path, args: &[&str]) -> Output {
    Command::new(BIN)
        .arg("stores")
        .args(args)
        .env_clear()
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("TMPDIR", std::env::temp_dir())
        .output()
        .expect("spawn store census")
}

fn payload(output: &Output) -> Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

fn assert_composition(home: &Path, data: &Path) -> Value {
    let summaries = stores(home, data, &[]);
    let paths = stores(home, data, &["--paths"]);
    let census = stores(home, data, &["--census"]);
    let combined = payload(&census);
    assert_eq!(combined["version"], 1);
    assert_eq!(combined["stores"], payload(&summaries));
    assert_eq!(combined["paths"], payload(&paths));
    let text = String::from_utf8(census.stdout).unwrap();
    for (key, standalone) in [("stores", summaries), ("paths", paths)] {
        let array = String::from_utf8(standalone.stdout).unwrap();
        assert!(text.contains(&format!("\"{key}\":{}", array.trim_end())));
    }
    combined
}

#[test]
fn empty_census_has_no_synthetic_stores_or_paths() {
    let home = temp_dir("empty-census-home");
    let data = temp_dir("empty-census-data");
    let census = assert_composition(&home, &data);
    assert_eq!(census["stores"], serde_json::json!([]));
    assert_eq!(census["paths"], serde_json::json!([]));
    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}

#[cfg(unix)]
#[test]
fn census_preserves_adapter_order_paths_and_unreadable_health() {
    use std::os::unix::fs::PermissionsExt;

    let home = temp_dir("mixed-census-home");
    let data = temp_dir("mixed-census-data");
    let files = [
        ".claude/projects/project/z.jsonl",
        ".claude/projects/project/a.jsonl",
        ".codex/sessions/2026/01/02/rollout-session.jsonl",
        ".local/share/opencode/opencode.db",
        ".pi/agent/sessions/project/session.jsonl.gz",
        ".omp/agent/sessions/project/sidecar.jsonl",
    ];
    for file in files {
        let path = home.join(file);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, b"not parsed by a census").unwrap();
    }
    fs::write(home.join(".claude/projects/project/config.json"), b"{}").unwrap();
    let blocked = home.join(".kimi/sessions");
    fs::create_dir_all(&blocked).unwrap();
    fs::write(blocked.join("hidden.jsonl"), b"{}").unwrap();
    let durable = serde_json::json!({
        "agent": "codex",
        "path": home.join(files[2]),
        "kind": "source-read-failed",
        "reason": "durable fixture failure",
    });
    fs::write(
        data.join(".source-health.json"),
        serde_json::to_vec(&serde_json::json!({
            "code": "source-unreadable",
            "issues": [durable.clone()],
        }))
        .unwrap(),
    )
    .unwrap();
    fs::set_permissions(&blocked, fs::Permissions::from_mode(0o000)).unwrap();
    let result = std::panic::catch_unwind(|| assert_composition(&home, &data));
    fs::set_permissions(&blocked, fs::Permissions::from_mode(0o700)).unwrap();
    let census = result.unwrap();

    let summaries = census["stores"].as_array().unwrap();
    assert_eq!(
        summaries
            .iter()
            .map(|row| row["name"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["claude", "codex", "opencode", "kimi", "pi"]
    );
    assert_eq!(summaries[0]["files"], 2);
    assert_eq!(summaries[0]["state"], "available");
    assert_eq!(summaries[1]["state"], "source-unreadable");
    assert_eq!(summaries[1]["issues"], serde_json::json!([durable]));
    assert_eq!(summaries[3]["files"], 0);
    assert_eq!(summaries[3]["state"], "source-unreadable");
    assert_eq!(summaries[3]["issues"][0]["kind"], "permission-denied");
    let paths = census["paths"].as_array().unwrap();
    let available: Vec<_> = paths
        .iter()
        .filter(|row| row["state"] == "available")
        .map(|row| row["path"].as_str().unwrap())
        .collect();
    let expected: Vec<_> = [files[1], files[0], files[2], files[3], files[5], files[4]]
        .map(|path| home.join(path).to_string_lossy().into_owned())
        .into_iter()
        .collect();
    assert_eq!(available, expected);
    assert!(paths.iter().any(|row| {
        row["name"] == "kimi"
            && row["path"] == blocked.to_string_lossy().as_ref()
            && row["kind"] == "permission-denied"
    }));
    let health = paths.last().unwrap();
    assert_eq!(health["name"], "codex");
    assert_eq!(health["kind"], "source-read-failed");
    assert_eq!(health["reason"], "durable fixture failure");
    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}

#[test]
fn census_applies_global_and_absent_adapter_health_to_both_arrays() {
    let home = temp_dir("health-census-home");
    let data = temp_dir("health-census-data");
    let root = home.join(".claude/projects/project");
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("session.jsonl"), b"{}").unwrap();
    for (agent, expected) in [("all", vec!["claude"]), ("codex", vec!["claude", "codex"])] {
        fs::write(
            data.join(".source-health.json"),
            serde_json::to_vec(&serde_json::json!({
                "code": "source-unreadable",
                "issues": [{"agent": agent, "path": "fixture", "kind": "io", "reason": "denied"}],
            }))
            .unwrap(),
        )
        .unwrap();
        let census = assert_composition(&home, &data);
        let summaries = census["stores"].as_array().unwrap();
        assert_eq!(
            summaries
                .iter()
                .map(|row| row["name"].as_str().unwrap())
                .collect::<Vec<_>>(),
            expected
        );
        assert_eq!(summaries.last().unwrap()["state"], "source-unreadable");
        assert_eq!(
            census["paths"].as_array().unwrap().last().unwrap()["name"],
            agent
        );
    }
    fs::remove_dir_all(home).unwrap();
    fs::remove_dir_all(data).unwrap();
}
