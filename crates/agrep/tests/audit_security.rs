mod support;
use agrep_core::{data::DataRoot, store};
use serde_json::{json, Value};
use std::{
    fs,
    process::{Command, Output},
};
use tempfile::TempDir;
fn command(t: &TempDir) -> Command {
    let mut command = Command::new(support::binary());
    command
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path().join("home"))
        .env("USERPROFILE", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"))
        .env("AGREP_CALLER_PUBLICATION_DIR", t.path().join("callers"));
    if let Some(root) = std::env::var_os("SystemRoot") {
        command.env("SystemRoot", root);
    }
    command
}
fn ok(t: &TempDir, args: &[&str]) -> Output {
    let out = command(t).args(args).output().unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {} {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    out
}
/// Runs `index` or `reindex` and returns the statistics it reports as an
/// `AGREP_QUERY_TIMING` event.
fn statistics(t: &TempDir, args: &[&str]) -> Value {
    let out = command(t)
        .env("AGREP_QUERY_TIMING", "1")
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stderr)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find_map(|event| event.get("index").cloned())
        .expect("index statistics event")
}
fn fixture() -> TempDir {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("home/.pi/agent/sessions/project");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("session.jsonl"),format!("{}\n{}\n",json!({"type":"session","id":"secure-session","version":3,"cwd":t.path()}),json!({"type":"message","id":"m1","parentId":null,"timestamp":"2026-10-29T10:01:00Z","message":{"role":"user","content":[{"type":"text","text":"database migration failure"}]}}))).unwrap();
    let value = statistics(&t, &["index"]);
    assert!(value.get("skipped_lock").is_none());
    t
}
#[test]
fn digestless_handles_are_rejected_but_positional_navigation_remains() {
    let t = fixture();
    for args in [
        vec!["around", "@secure-session:0"],
        vec!["recall", "@secure-session:0"],
        vec!["chats", "@secure-session:0"],
    ] {
        let out = command(&t).args(args).output().unwrap();
        assert_eq!(out.status.code(), Some(2));
        assert!(String::from_utf8_lossy(&out.stderr).contains("invalid result handle"));
    }
    ok(&t, &["around", "secure-session", "0"]);
}
#[test]
fn installed_corruption_is_disclosed_and_reported_by_diagnostics() {
    let t = fixture();
    let root = DataRoot(t.path().join("data"));
    store::writer(&root)
        .unwrap()
        .execute(
            "INSERT INTO semantic_refs SELECT id,digest,digest FROM prose",
            [],
        )
        .unwrap();
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let run = |args: &[&str]| {
        command(&t)
            .env("AGREP_PROFILE", "compact")
            .env("AGREP_DATA_READONLY", "1")
            .args(args)
            .output()
            .unwrap()
    };
    // Automatic hybrid alone keeps the keyword answer, disclosed once.
    for args in [
        vec!["search", "database migration failure"],
        vec!["recall", "database migration failure"],
        vec!["recall", "--json", "database migration failure"],
    ] {
        let out = run(&args);
        assert!(
            out.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        let text = String::from_utf8_lossy(&out.stderr);
        assert!(text.contains("semantic search failed"), "{text}");
        // Under AGREP_DATA_READONLY the cause rides on the stderr line, not the log.
        assert!(text.contains("SHA-256 mismatch"), "{text}");
        assert_eq!(text.lines().count(), 1);
        assert!(String::from_utf8_lossy(&out.stdout).contains("database migration failure"));
        if args.contains(&"--json") {
            let value: Value = serde_json::from_slice(&out.stdout).unwrap();
            assert_eq!(value["semantic_status"]["state"], "unavailable");
        }
    }
    // An explicit `-s` never answers with keyword rows: agrep 0.4.9 printed one line, or
    // under --json its `semantic-unavailable` record, and exited 2 (py/search.py:7686-7719,
    // py/recall.py:2404-2412, 2717-2747).
    for args in [
        vec!["search", "-s", "database migration failure"],
        vec!["recall", "-s", "database migration failure"],
        vec![
            "pack",
            "-s",
            "database migration failure",
            "migration failure",
        ],
    ] {
        let out = run(&args);
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        assert!(out.stdout.is_empty(), "{args:?}");
        let text = String::from_utf8_lossy(&out.stderr);
        assert_eq!(text.lines().count(), 1, "{text}");
        assert!(
            text.starts_with("semantic search unavailable: model SHA-256 mismatch: "),
            "{text}"
        );
    }
    let keys =
        |value: &Value| -> Vec<String> { value.as_object().unwrap().keys().cloned().collect() };
    let out = run(&["search", "-s", "--json", "database migration failure"]);
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stderr.is_empty());
    let record: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(
        keys(&record),
        [
            "kind",
            "query",
            "engine",
            "hits",
            "error",
            "semantic",
            "self_exclusion",
            "completeness",
            "filter_coverage",
            "freshness",
            "semantic_coverage",
        ]
    );
    let reason = record["semantic"]["reason"].as_str().unwrap().to_owned();
    assert!(reason.starts_with("model SHA-256 mismatch: "), "{reason}");
    // 0.4.9's record less corpus_age_s and the status's filtered/truncated (PORTING.md).
    assert_eq!(
        record,
        json!({"kind":"agrep-meta","query":"database migration failure",
            "engine":"semantic:unavailable","hits":[],"error":{"code":"semantic-unavailable"},
            "semantic":{"state":"unavailable","eligible":true,"reason":reason,"min_score":0.82,
                "score_kind":"unavailable","coverage":null,"complete":false,
                "fallback_recommended":true},
            "self_exclusion":{"active":false,"reason":"machine-surface"},
            "completeness":{"shown":0,"total":0,"total_basis":"floor","unit":"row",
                "truncated":true,
                "action_unavailable_reason":"the query did not produce a result set"},
            "filter_coverage":{"empty_dimensions":[],"checked":false,
                "reason":"the query returned before the index was filtered"},
            "freshness":record["freshness"],"semantic_coverage":null})
    );
    for (args, mut expected) in [
        (
            vec!["recall", "-s", "--json", "database migration failure"],
            vec!["query", "engine", "hits", "score_kind", "semantic_status"],
        ),
        (
            vec![
                "pack",
                "-s",
                "--json",
                "database migration failure",
                "migration failure",
            ],
            vec![
                "query",
                "engine",
                "hits",
                "score_kind",
                "semantic_status",
                "semantic_queries",
            ],
        ),
    ] {
        let out = run(&args);
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        assert!(out.stderr.is_empty(), "{args:?}");
        let value: Value = serde_json::from_slice(&out.stdout).unwrap();
        expected.extend([
            "self_exclusion",
            "filter_coverage",
            "freshness",
            "semantic_coverage",
            "error",
        ]);
        assert_eq!(keys(&value), expected);
        assert_eq!(value["engine"], "semantic:unavailable");
        assert_eq!(value["hits"], json!([]));
        assert_eq!(value["score_kind"], "unavailable");
        assert!(value.get("partial").is_none());
        assert_eq!(value["semantic_status"]["state"], "unavailable");
        assert_eq!(value["semantic_status"]["reason"], json!(reason));
        assert_eq!(value["semantic_coverage"], Value::Null);
        assert_eq!(value["error"], json!({"code":"semantic-unavailable"}));
    }
    assert!(!root.path().join("log").exists());
    let status: Value = serde_json::from_slice(&ok(&t, &["status", "--json"]).stdout).unwrap();
    assert!(status["semantic"]["integrity_error"]
        .as_str()
        .unwrap()
        .contains("SHA-256"));
    let doctor = command(&t)
        .args(["doctor", "--deep", "--json"])
        .output()
        .unwrap();
    // 0.4.9's doctor exits 0 for a completed report, whatever it found (py/doctor.py:15).
    assert_eq!(doctor.status.code(), Some(0));
    let doctor: Value = serde_json::from_slice(&doctor.stdout).unwrap();
    assert!(doctor["checks"]
        .as_array()
        .unwrap()
        .iter()
        .any(|c| c["check"] == "semantic_model" && c["state"] == "failed"));
}
#[test]
fn full_reindex_clears_vector_references_and_queues_every_visible_row() {
    let t = fixture();
    let out = ok(&t, &["set", "embeddings", "off"]);
    assert_eq!(
        String::from_utf8(out.stdout).unwrap(),
        "embeddings = off  (off disables model downloads and meaning search)\n"
    );
    let root = DataRoot(t.path().join("data"));
    let c = store::writer(&root).unwrap();
    c.execute_batch("INSERT INTO semantic_refs SELECT id,digest,digest FROM prose; INSERT INTO semantic_blocks VALUES('old',1); INSERT INTO semantic_chunks SELECT digest,0,'old',0 FROM prose LIMIT 1; DELETE FROM semantic_queue;").unwrap();
    drop(c);
    let value = statistics(&t, &["reindex", "--full"]);
    assert!(value.get("skipped_lock").is_none());
    let c = store::reader(&root).unwrap();
    assert_eq!(agrep_core::semantic::coverage(&c).unwrap().embedded, 0);
    assert_eq!(
        c.query_row(
            "SELECT CAST(value AS INTEGER) FROM state WHERE key='semantic_ref_count'",
            [],
            |r| r.get::<_, usize>(0)
        )
        .unwrap(),
        0
    );
    for table in ["semantic_refs", "semantic_blocks", "semantic_chunks"] {
        assert_eq!(
            c.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r
                .get::<_, usize>(0))
                .unwrap(),
            0
        );
    }
    assert_eq!(
        c.query_row("SELECT count(*) FROM semantic_queue", [], |r| r
            .get::<_, usize>(0))
            .unwrap(),
        c.query_row("SELECT count(*) FROM prose WHERE visible=1", [], |r| r
            .get::<_, usize>(0))
            .unwrap()
    );
}
#[test]
fn first_build_queues_every_visible_row_and_its_triggers_queue_later_rows() {
    let t = fixture();
    let root = DataRoot(t.path().join("data"));
    let counts = || {
        let c = store::reader(&root).unwrap();
        let count = |sql: &str| c.query_row(sql, [], |r| r.get::<_, usize>(0)).unwrap();
        (
            count("SELECT count(*) FROM prose WHERE visible=1"),
            count("SELECT count(*) FROM semantic_queue"),
            count("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN ('semantic_insert','semantic_update','semantic_delete')"),
        )
    };
    let (rows, queued, triggers) = counts();
    assert!(rows > 0);
    assert_eq!((queued, triggers), (rows, 3));
    let session = t
        .path()
        .join("home/.pi/agent/sessions/project/session.jsonl");
    let mut text = fs::read_to_string(&session).unwrap();
    text.push_str(&format!("{}\n", json!({"type":"message","id":"m2","parentId":"m1","timestamp":"2026-10-29T10:02:00Z","message":{"role":"user","content":[{"type":"text","text":"a second question for the queue"}]}})));
    fs::write(&session, text).unwrap();
    ok(&t, &["index"]);
    assert_eq!(counts(), (rows + 1, rows + 1, 3));
    let c = store::reader(&root).unwrap();
    assert_eq!(agrep_core::semantic::coverage(&c).unwrap().embedded, 0);
}
#[test]
fn stale_first_build_is_distinguished_from_a_live_one() {
    let t = tempfile::tempdir().unwrap();
    let root = DataRoot::open(t.path().join("data"), true).unwrap();
    fs::write(root.path().join("index.building.db"), b"interrupted").unwrap();
    let state = || -> String {
        let v: Value = serde_json::from_slice(&ok(&t, &["status", "--json"]).stdout).unwrap();
        v["index"]["state"].as_str().unwrap().into()
    };
    {
        let _lock = root.lock(true).unwrap();
        assert_eq!(state(), "first build in progress");
    }
    assert_eq!(state(), "first build interrupted");
}
#[test]
fn doctor_preserves_edited_extensions_and_hook_json_layout() {
    let t = tempfile::tempdir().unwrap();
    fs::create_dir_all(t.path().join("home/.pi/agent")).unwrap();
    fs::create_dir_all(t.path().join("home/.claude")).unwrap();
    let settings = t.path().join("home/.claude/settings.json");
    let original = "{\r\n\t\"z\": 1e+3,\r\n\t\"a\": { \"keep\" : [1, 2] }\r\n}\r\n";
    fs::write(&settings, original).unwrap();
    ok(
        &t,
        &[
            "setup",
            "--yes",
            "--no-teach",
            "--no-semantic",
            "--no-archive",
        ],
    );
    let installed = fs::read_to_string(&settings).unwrap();
    assert!(installed.contains("\"z\": 1e+3"));
    assert!(installed.contains("\"a\": { \"keep\" : [1, 2] }"));
    let extension = t
        .path()
        .join("home/.pi/agent/extensions/agrep-postcompact.ts");
    fs::write(&extension, b"user edits").unwrap();
    let out = command(&t).args(["doctor", "--fix"]).output().unwrap();
    // 0.4.9: "1 requested repair failed" (py/doctor.py:15).
    assert_eq!(out.status.code(), Some(1));
    let stdout = String::from_utf8_lossy(&out.stdout);
    // The failed repair names its cause, as 0.4.9's doctor rows did.
    let failed = stdout
        .lines()
        .find(|l| l.starts_with("integration_repair: failed - "))
        .unwrap_or_else(|| panic!("{stdout}"));
    // Windows renders the joined test path with mixed separators, so compare its file name.
    assert!(
        failed.contains("file has user edits: ") && failed.ends_with("agrep-postcompact.ts"),
        "{failed}"
    );
    assert_eq!(fs::read(&extension).unwrap(), b"user edits");
    fs::remove_file(&extension).unwrap();
    ok(&t, &["doctor", "--fix"]);
    assert!(extension.exists());
    ok(&t, &["remove"]);
    assert_eq!(fs::read_to_string(settings).unwrap(), original);
}
// `index` runs the post-index hook itself, after the refresh, so the hook's output exists
// once it exits.
#[test]
fn index_runs_the_post_index_hook_before_it_exits() {
    let t = fixture();
    let output = t.path().join("hook-done");
    let hook = if cfg!(windows) {
        format!("echo done>\"{}\"", output.display())
    } else {
        format!("printf done > '{}'", output.display())
    };
    ok(&t, &["set", "post_index", &hook]);
    fs::write(t.path().join("home/.pi/agent/sessions/project/second.jsonl"),format!("{}\n{}\n",json!({"type":"session","id":"second-session","version":3,"cwd":t.path()}),json!({"type":"message","id":"m2","parentId":null,"timestamp":"2026-10-29T10:02:00Z","message":{"role":"user","content":[{"type":"text","text":"new background input"}]}}))).unwrap();
    ok(&t, &["index"]);
    assert!(fs::read_to_string(&output).unwrap().starts_with("done"));
    let root = DataRoot(t.path().join("data"));
    let c = store::reader(&root).unwrap();
    assert_eq!(
        c.query_row(
            "SELECT count(*) FROM state WHERE key='post_index_pending'",
            [],
            |r| r.get::<_, usize>(0)
        )
        .unwrap(),
        0
    );
}
#[test]
fn native_resume_rejects_options_and_relative_working_directories() {
    use agrep_core::{history::Session, launch};
    let t = tempfile::tempdir().unwrap();
    let mut s = Session {
        agent: "claude".into(),
        id: "--dangerously-skip-permissions".into(),
        cwd: t.path().to_string_lossy().into(),
        project: String::new(),
        parent: String::new(),
        family: String::new(),
        side: false,
        title: String::new(),
        first_ts: 0,
        last_ts: 0,
    };
    assert!(launch::resume_args(&s).is_err());
    s.id = "native-session".into();
    s.cwd = "-project".into();
    assert!(launch::resume_args(&s).is_err());
    s.cwd = t.path().to_string_lossy().into();
    assert!(launch::resume_args(&s).is_ok());
}
#[cfg(unix)]
#[test]
fn owned_directories_are_private_and_exposure_is_reported() {
    use std::os::unix::fs::PermissionsExt;
    let t = fixture();
    let root = DataRoot(t.path().join("data"));
    let mode = |path: &std::path::Path| fs::metadata(path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(root.path()), 0o700);
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let out = command(&t)
        .env("AGREP_DATA_READONLY", "1")
        .args(["search", "database migration failure"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(mode(root.path()), 0o755);
    let out = command(&t).args(["doctor", "--json"]).output().unwrap();
    assert_eq!(out.status.code(), Some(0));
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["status"]["permissions"]["private"], false);
    assert!(v["checks"]
        .as_array()
        .unwrap()
        .iter()
        .any(|c| c["check"] == "directory_permissions" && c["state"] == "failed"));
    root.validate(false).unwrap();
    assert_eq!(mode(root.path()), 0o700);
    ok(&t, &["archive"]);
    assert_eq!(mode(&root.path().join("archive")), 0o700);
    agrep_core::logging::open(&root).unwrap();
    assert_eq!(mode(&root.path().join("log")), 0o700);
    let model_parent = root.path().join("model");
    let model = model_parent.join("granite");
    fs::create_dir_all(&model).unwrap();
    fs::set_permissions(&model_parent, fs::Permissions::from_mode(0o755)).unwrap();
    // Fail after permission repair without attempting a model download.
    fs::create_dir(model.join("download.lock")).unwrap();
    assert!(agrep_core::semantic_model::provision_at(&model).is_err());
    assert_eq!(mode(&model), 0o700);
    assert_eq!(mode(&model_parent), 0o700);
}

#[test]
fn evidence_headers_neutralize_transcript_control_and_bidi_text() {
    let t = fixture();
    let root = DataRoot(t.path().join("data"));
    let hostile = "safe\u{1b}]52;c;payload\u{7}\u{202e}tail";
    store::writer(&root)
        .unwrap()
        .execute("UPDATE sessions SET project=?", [hostile])
        .unwrap();
    let out = ok(&t, &["around", "secure-session", "0"]);
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(!text.contains(['\u{1b}', '\u{7}', '\u{202e}']));
    let packet = json!({"selection":{"caller_session":"secure-session","family_root":"secure-session","project":hostile,"agent":hostile,"boundary_turn":0},"coverage":{"shown_root_blocks":0,"eligible_root_blocks":0},"rows":[],"omissions":{"root_blocks":0}});
    let text = agrep_core::recovery::human(&packet);
    assert!(!text.contains(['\u{1b}', '\u{7}', '\u{202e}']));
}
// `doctor --deep --no-semantic` runs the deep checks "without the semantic model check"
// (help.rs): an installed model is neither verified nor given a check row.
#[test]
fn doctor_no_semantic_neither_verifies_nor_reports_the_model() {
    let t = fixture();
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let doctor = |args: &[&str]| -> Value {
        let out = command(&t).args(args).output().unwrap();
        serde_json::from_slice(&out.stdout).unwrap()
    };
    let model_rows = |report: &Value| -> Vec<Value> {
        let checks = report["checks"].as_array().unwrap();
        let rows = checks.iter().filter(|c| c["check"] == "semantic_model");
        rows.cloned().collect()
    };
    let skipped = doctor(&["doctor", "--deep", "--no-semantic", "--json"]);
    assert_eq!(model_rows(&skipped), Vec::<Value>::new());
    assert!(skipped["checks"]
        .as_array()
        .unwrap()
        .iter()
        .any(|c| c["check"] == "sqlite_integrity" && c["state"] == "passed"));
    assert_eq!(skipped["status"]["semantic"]["model_installed"], true);
    assert_eq!(
        skipped["status"]["semantic"]["integrity_error"],
        Value::Null
    );
    let checked = doctor(&["doctor", "--deep", "--json"]);
    let rows = model_rows(&checked);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["state"], "failed");
    assert!(rows[0]["detail"].as_str().unwrap().contains("SHA-256"));
}
