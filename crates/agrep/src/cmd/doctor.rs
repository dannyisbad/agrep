use agrep_core::{data::DataRoot, diagnostics, store};
use anyhow::Result;
use serde_json::json;
use std::io::{IsTerminal, Write};
pub fn run(deep: bool, fix: bool, no_semantic: bool, json_output: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    let mut s = diagnostics::status(&root)?;
    let mut checks = vec![];
    let mut repaired = vec![];
    if fix && s["index"]["state"] == "missing" && (s["format"] == "valid" || !root.0.exists()) {
        root.validate(true)?;
        store::refresh(&root, true, false)?;
        repaired.push("created index");
        s = diagnostics::status(&root)?;
    }
    if (deep || fix) && s["index"]["state"] == "ready" {
        let c = store::reader(&root)?;
        let integrity: String = c
            .query_row("PRAGMA integrity_check", [], |r| r.get(0))
            .unwrap_or_else(|e| e.to_string());
        checks.push(json!({"check":"sqlite_integrity","state":if integrity=="ok"{"passed"}else{"failed"},"detail":integrity}));
    }
    if fix
        && s["index"]["state"] == "ready"
        && !checks
            .iter()
            .any(|c| c["check"] == "sqlite_integrity" && c["state"] == "failed")
    {
        root.validate(false)?;
        store::refresh(&root, true, false)?;
        repaired.push("refreshed source files");
        s = diagnostics::status(&root)?;
    }
    let drifted = s["integrations"]
        .as_array()
        .is_some_and(|entries| entries.iter().any(|e| e["drifted"] == true));
    if fix && drifted {
        let errors = agrep_core::integrations::repair(&root)?;
        if errors.is_empty() {
            repaired.push("repaired enrolled integrations");
        } else {
            checks.push(json!({"check":"integration_repair","state":"failed","detail":errors}));
        }
        s = diagnostics::status(&root)?;
    }
    let corrupt = s["index"]["state"] == "unreadable"
        || checks
            .iter()
            .any(|c| c["check"] == "sqlite_integrity" && c["state"] == "failed");
    if fix && corrupt {
        if !std::io::stdin().is_terminal() {
            checks.push(json!({"check":"repair","state":"not_checked","detail":"rebuilding a corrupt index requires confirmation on a terminal; run agrep doctor --fix"}));
        } else {
            eprint!(
                "Rebuild corrupt index at {} from source files? [y/N] ",
                root.db().display()
            );
            std::io::stderr().flush()?;
            let mut answer = String::new();
            std::io::stdin().read_line(&mut answer)?;
            if matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
                rebuild(&root, |repair| {
                    store::refresh(repair, true, true).map(|_| ())
                })?;
                repaired.push("rebuilt index; corrupt database retained");
                s = diagnostics::status(&root)?;
                checks.retain(|c| c["check"] != "sqlite_integrity");
            }
        }
    }
    let semantic = if deep && !no_semantic {
        if s["semantic"]["model_present"] == true {
            "present"
        } else {
            "not installed"
        }
    } else {
        "not_checked"
    };
    checks.push(json!({"check":"semantic_model","state":semantic}));
    let integration_errors = s["integrations"].as_array().is_some_and(|entries| {
        entries
            .iter()
            .any(|e| e["drifted"] == true || e["state"] == "unreadable")
    });
    let errors = s["sources"]
        .as_array()
        .is_some_and(|a| a.iter().any(|v| !v["last_error"].is_null()));
    let exit = if s["format"] != "valid"
        || s["index"]["state"] != "ready"
        || checks.iter().any(|c| c["state"] == "failed")
    {
        2
    } else if errors || integration_errors {
        1
    } else {
        0
    };
    let v = json!({"version":1,"status":&s,"checks":&checks,"repairs":&repaired,"exit":exit});
    if json_output {
        println!("{v}")
    } else {
        println!(
            "doctor: {}",
            if exit == 0 {
                "checks passed"
            } else {
                "action needed"
            }
        );
        for check in &checks {
            println!(
                "{}: {}",
                check["check"].as_str().unwrap_or("check"),
                check["state"].as_str().unwrap_or("unknown")
            );
        }
        for repair in &repaired {
            println!("repaired: {repair}");
        }
        for entry in s["integrations"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|e| e["drifted"] == true)
        {
            println!(
                "integration needs repair: {}",
                entry["path"].as_str().unwrap_or("")
            );
        }
    }
    Ok(exit)
}

fn rebuild(root: &DataRoot, build: impl FnOnce(&DataRoot) -> Result<()>) -> Result<()> {
    let _lock = root.lock(true)?;
    for entry in std::fs::read_dir(root.path())? {
        let entry = entry?;
        if entry.file_type()?.is_dir() && entry.file_name().to_string_lossy().starts_with("repair-")
        {
            std::fs::remove_dir_all(entry.path())?;
        }
    }
    let repair = DataRoot(root.0.join(format!("repair-{}", std::process::id())));
    repair.validate(true)?;
    if root.path().join("harness_prefixes.txt").exists() {
        std::fs::copy(
            root.path().join("harness_prefixes.txt"),
            repair.path().join("harness_prefixes.txt"),
        )?;
    }
    let outcome = (|| -> Result<()> {
        build(&repair)?;
        let backup = root.0.join(format!(
            "index.corrupt-{}",
            chrono::Utc::now().timestamp_millis()
        ));
        std::fs::rename(root.db(), &backup)?;
        for suffix in ["-wal", "-shm"] {
            let p = root.0.join(format!("index.db{suffix}"));
            if p.exists() {
                std::fs::rename(
                    p,
                    std::path::PathBuf::from(format!("{}{suffix}", backup.display())),
                )?;
            }
        }
        std::fs::rename(repair.db(), root.db())?;
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&repair.0);
    outcome
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rebuild_preserves_wal_and_rules_on_every_platform() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path().join("data"), true).unwrap();
        std::fs::write(root.db(), b"old index").unwrap();
        std::fs::write(root.path().join("index.db-wal"), b"old wal").unwrap();
        std::fs::write(root.path().join("index.db-shm"), b"old shm").unwrap();
        std::fs::write(root.path().join("harness_prefixes.txt"), b"harness-only").unwrap();
        std::fs::create_dir(root.path().join("repair-stale")).unwrap();
        rebuild(&root, |repair| {
            assert_eq!(
                std::fs::read(repair.path().join("harness_prefixes.txt"))?,
                b"harness-only"
            );
            std::fs::write(repair.db(), b"new index")?;
            Ok(())
        })
        .unwrap();
        assert_eq!(std::fs::read(root.db()).unwrap(), b"new index");
        assert!(!root.path().join("repair-stale").exists());
        let backup = std::fs::read_dir(root.path())
            .unwrap()
            .map(|e| e.unwrap().path())
            .find(|p| {
                p.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("index.corrupt-")
                    && !p.to_string_lossy().ends_with("-wal")
                    && !p.to_string_lossy().ends_with("-shm")
            })
            .unwrap();
        assert_eq!(std::fs::read(&backup).unwrap(), b"old index");
        assert_eq!(
            std::fs::read(format!("{}-wal", backup.display())).unwrap(),
            b"old wal"
        );
        assert_eq!(
            std::fs::read(format!("{}-shm", backup.display())).unwrap(),
            b"old shm"
        );
    }
}
