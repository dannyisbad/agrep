use agrep_core::{data::DataRoot, diagnostics, store};
use anyhow::Result;
use serde_json::json;
use std::io::{IsTerminal, Write};
pub fn run(deep: bool, fix: bool, no_semantic: bool, json_output: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    let mut s = diagnostics::status(&root)?;
    let mut checks = vec![];
    let mut repaired = vec![];
    // 0.4.9's contract (py/doctor.py:15): a completed report exits 0 whatever it found, and
    // only a repair that --fix attempted and could not finish exits 1.
    let mut repair_failed = false;
    if fix && s["index"]["state"] == "missing" && (s["format"] == "valid" || !root.0.exists()) {
        match root
            .validate(true)
            .and_then(|()| store::refresh(&root, true, false))
        {
            Ok(_) => repaired.push("created index"),
            Err(e) => {
                repair_failed = true;
                checks.push(
                    json!({"check":"index_creation","state":"failed","detail":format!("{e:#}")}),
                );
            }
        }
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
        match root
            .validate(false)
            .and_then(|()| store::refresh(&root, true, false))
        {
            Ok(_) => repaired.push("refreshed source files"),
            Err(e) => {
                repair_failed = true;
                checks.push(
                    json!({"check":"source_refresh","state":"failed","detail":format!("{e:#}")}),
                );
            }
        }
        s = diagnostics::status(&root)?;
    }
    let drifted = s["integrations"]
        .as_array()
        .is_some_and(|entries| entries.iter().any(|e| e["drifted"] == true));
    if fix && drifted {
        match agrep_core::integrations::repair(&root) {
            Ok(errors) if errors.is_empty() => repaired.push("repaired enrolled integrations"),
            Ok(errors) => {
                repair_failed = true;
                checks.push(json!({"check":"integration_repair","state":"failed","detail":errors}));
            }
            Err(e) => {
                repair_failed = true;
                checks.push(json!({"check":"integration_repair","state":"failed","detail":format!("{e:#}")}));
            }
        }
        s = diagnostics::status(&root)?;
    }
    let corrupt = s["index"]["state"] == "unreadable"
        || checks
            .iter()
            .any(|c| c["check"] == "sqlite_integrity" && c["state"] == "failed");
    if fix && corrupt {
        if !std::io::stdin().is_terminal() {
            repair_failed = true;
            checks.push(json!({"check":"repair","state":"failed","detail":"rebuilding a corrupt index requires confirmation on a terminal; run agrep doctor --fix"}));
        } else {
            eprint!(
                "Rebuild corrupt index at {} from source files? [y/N] ",
                root.db().display()
            );
            std::io::stderr().flush()?;
            let mut answer = String::new();
            std::io::stdin().read_line(&mut answer)?;
            if matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
                match rebuild(&root, |repair| {
                    store::refresh(repair, true, true).map(|_| ())
                }) {
                    Ok(()) => {
                        repaired.push("rebuilt index; corrupt database retained");
                        s = diagnostics::status(&root)?;
                        checks.retain(|c| c["check"] != "sqlite_integrity");
                    }
                    Err(e) => {
                        repair_failed = true;
                        checks.push(
                            json!({"check":"repair","state":"failed","detail":format!("{e:#}")}),
                        );
                    }
                }
            }
        }
    }
    if s["permissions"]["private"] == false {
        checks.push(json!({"check":"directory_permissions","state":"failed","detail":s["permissions"]["exposed"]}));
    }
    if !no_semantic {
        diagnostics::verify_model(&root, &mut s);
        let semantic = if s["semantic"]["integrity_error"].is_string() {
            "failed"
        } else if deep {
            if s["semantic"]["model_present"] == true {
                "present"
            } else {
                "not installed"
            }
        } else {
            "not_checked"
        };
        checks.push(json!({"check":"semantic_model","state":semantic,"detail":s["semantic"]["integrity_error"]}));
    }
    report(&s, &checks, &repaired, repair_failed, json_output)
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

fn report(
    s: &serde_json::Value,
    checks: &[serde_json::Value],
    repaired: &[&str],
    repair_failed: bool,
    json_output: bool,
) -> Result<i32> {
    let integration_errors = s["integrations"].as_array().is_some_and(|entries| {
        entries
            .iter()
            .any(|e| e["drifted"] == true || e["state"] == "unreadable")
    });
    let errors = s["sources"]
        .as_array()
        .is_some_and(|a| a.iter().any(|v| !v["last_error"].is_null()));
    let healthy = s["format"] == "valid"
        && s["index"]["state"] == "ready"
        && !checks.iter().any(|c| c["state"] == "failed")
        && !errors
        && !integration_errors;
    let exit = i32::from(repair_failed);
    let v = json!({"version":1,"status":&s,"checks":&checks,"repairs":&repaired,"exit":exit});
    if json_output {
        println!("{}", agrep_core::json_output::to_string(&v)?)
    } else {
        println!(
            "doctor: {}",
            if healthy {
                "checks passed"
            } else {
                "action needed"
            }
        );
        // A failed check names its cause, as 0.4.9's rows did.
        for check in checks {
            let state = check["state"].as_str().unwrap_or("unknown");
            let detail = match &check["detail"] {
                serde_json::Value::String(s) => s.clone(),
                serde_json::Value::Array(items) => items
                    .iter()
                    .map(|v| v.as_str().map_or_else(|| v.to_string(), str::to_owned))
                    .collect::<Vec<_>>()
                    .join("; "),
                _ => String::new(),
            };
            let name = check["check"].as_str().unwrap_or("check");
            if state == "failed" && !detail.is_empty() {
                println!("{name}: {state} - {}", agrep_core::render::safe(&detail));
            } else {
                println!("{name}: {state}");
            }
        }
        for repair in repaired {
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
