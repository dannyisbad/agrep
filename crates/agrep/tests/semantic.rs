mod support;
use agrep_core::{data::DataRoot, semantic, store};
use std::{
    fs,
    process::{Command, Output},
};
fn run(t: &tempfile::TempDir, args: &[&str]) -> Output {
    Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path())
        .env("AGREP_HOME", t.path())
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"))
        .env("AGREP_PROFILE", "compact")
        .env("AGREP_DATA_READONLY", "1")
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn missing_model_is_one_line_exit_two_and_hybrid_stays_lexical() {
    let t = tempfile::tempdir().unwrap();
    let r = DataRoot::open(t.path().join("data"), true).unwrap();
    store::writer(&r).unwrap();
    let out = run(
        &t,
        &["search", "--no-auto", "-s", "database migration failure"],
    );
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&out.stderr),
        "semantic model not installed: run agrep setup\n"
    );
    assert!(out.stdout.is_empty());
    let out = run(&t, &["search", "--no-auto", "database migration failure"]);
    assert!(!String::from_utf8_lossy(&out.stderr).contains("semantic"));
    assert!(!t.path().join("model").exists());
}
#[test]
fn measured_query_cost_can_refuse_one_queued_row_before_loading_weights() {
    let t = tempfile::tempdir().unwrap();
    transcript(&t);
    assert!(agrep(&t).arg("index").output().unwrap().status.success());
    let root = DataRoot::open(t.path().join("data"), false).unwrap();
    let key = format!(
        "semantic_query_cost_v2_{}_{}_1",
        std::env::consts::OS,
        std::env::consts::ARCH
    );
    let updated = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    store::writer(&root)
        .unwrap()
        .execute(
            "INSERT INTO state VALUES(?1,?2)",
            rusqlite::params![
                key,
                serde_json::json!({"load":0.01,"cpu_unit":100.,"span_unit":100.,"updated":updated})
                    .to_string()
            ],
        )
        .unwrap();
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let command = || {
        let mut cmd = agrep(&t);
        cmd.env("RAYON_NUM_THREADS", "1")
            .env("AGREP_DATA_READONLY", "1")
            .env("AGREP_PROFILE", "compact");
        cmd
    };
    let out = command()
        .args(["search", "-s", "--json", "database migration failure"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stderr.is_empty());
    let meta: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(meta["error"]["code"], "semantic-unavailable");
    assert_eq!(meta["semantic"]["progress"]["remaining_rows"], 1);
    let out = command()
        .args(["search", "database migration failure"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0));
    assert!(out.stderr.is_empty());
    assert!(String::from_utf8_lossy(&out.stdout).contains("database migration failure"));
}
#[test]
fn a_building_meaning_index_never_answers_partially() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join(".pi/agent/sessions/fixture");
    fs::create_dir_all(&dir).unwrap();
    let mut source = String::from("{\"type\":\"session\",\"id\":\"semantic-build-fixture\",\"version\":3,\"cwd\":\"/fixture\",\"timestamp\":\"2026-10-29T10:00:00Z\"}\n");
    for n in 0..1024 {
        source.push_str(&format!("{}\n", serde_json::json!({"type":"message","id":format!("m{n}"),"parentId":if n==0 {None} else {Some(format!("m{}",n-1))},"timestamp":"2026-10-29T10:01:00Z","message":{"role":"user","content":[{"type":"text","text":format!("database migration failure number {n}")}]}})));
    }
    fs::write(dir.join("case.jsonl"), source).unwrap();
    let indexed = agrep(&t).arg("index").output().unwrap();
    assert!(
        indexed.status.success(),
        "{}",
        String::from_utf8_lossy(&indexed.stderr)
    );
    let root = DataRoot::open(t.path().join("data"), false).unwrap();
    assert_eq!(
        semantic::coverage(&store::reader(&root).unwrap())
            .unwrap()
            .total,
        1024
    );
    // A large build must be refused before loading weights. These sparse files
    // satisfy discovery; verification would fail if the lane tried to answer.
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    for command in ["search", "recall", "pack"] {
        let out = run(&t, &[command, "database migration failure"]);
        assert_eq!(
            out.status.code(),
            Some(0),
            "{command}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert!(!out.stdout.is_empty(), "{command}");
        let stderr = String::from_utf8_lossy(&out.stderr);
        for notice in [
            "semantic",
            "meaning",
            "building",
            "catching up",
            "may not show",
        ] {
            assert!(!stderr.contains(notice), "{command}: {stderr}");
        }
        assert!(!String::from_utf8_lossy(&out.stdout).contains("meaning match"));
        let out = run(&t, &[command, "-s", "database migration failure"]);
        assert_eq!(out.status.code(), Some(2), "{command}");
        assert!(out.stdout.is_empty());
        assert_eq!(String::from_utf8_lossy(&out.stderr), "semantic search unavailable: the meaning index is still building (0 of 1024 rows, about 18s left)\n");
        let out = run(&t, &[command, "-s", "--json", "database migration failure"]);
        assert_eq!(out.status.code(), Some(2), "{command}");
        assert!(out.stderr.is_empty());
        let meta: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(meta["error"]["code"], "semantic-unavailable");
        let status = if command == "search" {
            &meta["semantic"]
        } else {
            &meta["semantic_status"]
        };
        assert_eq!(
            status["progress"],
            serde_json::json!({"indexed_rows":0,"total_rows":1024,"remaining_rows":1024,"estimated_seconds":18})
        );
        assert!(meta["semantic_coverage"].is_null());
        assert!(meta.get("semantic_partial").is_none());
        assert!(status.get("partial").is_none());
    }
    let out = run(&t, &["status", "--json"]);
    let status: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(status["semantic"]["state"], "building");
    assert_eq!(status["semantic"]["progress"]["remaining_rows"], 1024);
}
// Expected lines and fields are agrep 0.4.9's for `-s foo_bar` (py/search.py:7686-7719,
// py/recall.py:2717-2747).
#[test]
fn explicit_semantic_refuses_an_identifier_query_as_0_4_9() {
    let t = tempfile::tempdir().unwrap();
    let r = DataRoot::open(t.path().join("data"), true).unwrap();
    let c = store::writer(&r).unwrap();
    c.execute("INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra) VALUES(1,'codex','session','p',1,'user','',0,0,'foo_bar text','digest','')",[]).unwrap();
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let refused = serde_json::json!({"state":"query-rejected","complete":false,
        "fallback_recommended":true,"eligible":false,"reason":"identifier-query",
        "min_score":0.82,"coverage":null});
    for command in ["search", "recall"] {
        let out = run(&t, &[command, "--no-auto", "-s", "foo_bar"]);
        assert_eq!(out.status.code(), Some(2), "{command}");
        assert!(out.stdout.is_empty(), "{command}");
        assert_eq!(
            String::from_utf8_lossy(&out.stderr),
            "semantic search unavailable: identifier-query\n"
        );
    }
    let out = run(&t, &["search", "--no-auto", "-s", "--json", "foo_bar"]);
    assert_eq!(out.status.code(), Some(2));
    let record: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(record["engine"], "semantic:policy");
    assert_eq!(
        record["error"],
        serde_json::json!({"code":"semantic-unavailable"})
    );
    assert_eq!(record["semantic"], refused);
    assert_eq!(record["semantic_coverage"], serde_json::Value::Null);
    assert!(record.get("semantic_partial").is_none());
    let out = run(&t, &["recall", "--no-auto", "-s", "--json", "foo_bar"]);
    assert_eq!(out.status.code(), Some(2));
    let record: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(record["engine"], "semantic:policy");
    assert_eq!(record["score_kind"], "cosine");
    assert!(record.get("partial").is_none());
    assert_eq!(record["semantic_status"], refused);
    assert_eq!(
        record["error"],
        serde_json::json!({"code":"semantic-unavailable"})
    );
}

#[test]
fn optional_model_failure_keeps_a_completed_keyword_answer() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join(".pi/agent/sessions/fixture");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("case.jsonl"), concat!(
        "{\"type\":\"session\",\"id\":\"semantic-failure-fixture\",\"version\":3,\"cwd\":\"/fixture\",\"timestamp\":\"2026-10-29T10:00:00Z\"}\n",
        "{\"type\":\"message\",\"id\":\"m1\",\"parentId\":null,\"timestamp\":\"2026-10-29T10:01:00Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"database migration failure\"}]}}\n"
    )).unwrap();
    let out = Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path())
        .env("AGREP_HOME", t.path())
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"))
        .arg("index")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let r = DataRoot::open(t.path().join("data"), false).unwrap();
    store::writer(&r)
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
    let failure = |out: &Output| -> Vec<String> {
        String::from_utf8_lossy(&out.stderr)
            .lines()
            .filter(|line| line.starts_with("semantic search failed"))
            .map(str::to_owned)
            .collect()
    };
    let out = run(&t, &["search", "--no-auto", "database migration failure"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("database migration failure"));
    // A read-only root is never written, so the cause rides on the one failure line.
    let lines = failure(&out);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].starts_with("semantic search failed; returning keyword results: "),
        "{lines:?}"
    );
    assert!(lines[0].contains("SHA-256 mismatch"), "{lines:?}");
    assert!(!t.path().join("data/log").exists());
    let out = agrep(&t)
        .env("AGREP_PROFILE", "compact")
        .args(["search", "--no-auto", "database migration failure"])
        .output()
        .unwrap();
    assert_eq!(
        out.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("database migration failure"));
    assert_eq!(
        failure(&out),
        ["semantic search failed; returning keyword results (details in log/agrep.log)"]
    );
    assert!(fs::read_to_string(t.path().join("data/log/agrep.log"))
        .unwrap()
        .contains("SHA-256 mismatch"));
}

/// The binary over `t` as home and data root, with nothing read-only.
fn agrep(t: &tempfile::TempDir) -> Command {
    let mut command = Command::new(support::binary());
    command
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path())
        .env("AGREP_HOME", t.path())
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"));
    command
}
fn transcript(t: &tempfile::TempDir) -> std::path::PathBuf {
    let dir = t.path().join(".pi/agent/sessions/fixture");
    fs::create_dir_all(&dir).unwrap();
    let session = dir.join("case.jsonl");
    fs::write(&session, concat!(
        "{\"type\":\"session\",\"id\":\"background-fixture\",\"version\":3,\"cwd\":\"/fixture\",\"timestamp\":\"2026-10-29T10:00:00Z\"}\n",
        "{\"type\":\"message\",\"id\":\"m1\",\"parentId\":null,\"timestamp\":\"2026-10-29T10:01:00Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"database migration failure\"}]}}\n"
    )).unwrap();
    session
}

#[test]
fn plain_index_neither_embeds_nor_waits_for_background_work() {
    let t = tempfile::tempdir().unwrap();
    let session = transcript(&t);
    let index = || {
        let out = agrep(&t).arg("index").output().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    };
    index();
    // An installed model whose weights fail verification: an inline drain would fail.
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let mut file = fs::OpenOptions::new().append(true).open(&session).unwrap();
    std::io::Write::write_all(&mut file, b"{\"type\":\"message\",\"id\":\"m2\",\"parentId\":\"m1\",\"timestamp\":\"2026-10-29T10:02:00Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"rollback plan for the failed migration\"}]}}\n").unwrap();
    drop(file);
    let r = DataRoot::open(t.path().join("data"), false).unwrap();
    // A background drain holds work.lock with rows queued.
    let work = r.work_lock(false).unwrap().unwrap();
    index();
    let coverage = semantic::coverage(&store::reader(&r).unwrap()).unwrap();
    assert_eq!((coverage.embedded, coverage.total), (0, 2));
    drop(work);
    index();
    // Holding work.lock until the directory goes keeps any worker the index started idle.
    let _quiet = r.work_lock(true).unwrap().unwrap();
}

// agrep 0.4.9's `reindex --max-new` refusal word for word (reindex.py:190-194), given
// before any work.
#[test]
fn reindex_max_new_is_a_count_of_at_least_one() {
    let t = tempfile::tempdir().unwrap();
    let reindex = |value: &str| {
        agrep(&t)
            .args(["reindex", "--max-new", value])
            .output()
            .unwrap()
    };
    for value in ["0", "-1"] {
        let out = reindex(value);
        assert_eq!(out.status.code(), Some(2), "{value}");
        assert!(out.stdout.is_empty());
        assert_eq!(
            String::from_utf8_lossy(&out.stderr),
            "agrep reindex: --max-new must be at least 1\n"
        );
    }
    assert!(!t.path().join("data").exists());
    let out = reindex("5");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

// `--no-auto` skips automatic index/freshness work (help.rs), and so every worker: with the
// archive on, a command that checks the stores would start one.
#[test]
fn no_auto_starts_no_background_work() {
    let t = tempfile::tempdir().unwrap();
    transcript(&t);
    for args in [&["index"][..], &["archive", "--on"]] {
        let out = agrep(&t).args(args).output().unwrap();
        assert!(out.status.success(), "{args:?}");
    }
    for args in [
        &["search", "--no-auto", "database migration"][..],
        &["status", "--no-auto"],
    ] {
        let out = agrep(&t)
            .env("AGREP_QUERY_TIMING", "1")
            .args(args)
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(0), "{args:?}");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(!stderr.contains("worker_started"), "{args:?}: {stderr}");
    }
}

// With the archive on, a command starts a worker only once a scheduled capture is due,
// not for every query.
#[test]
fn archive_starts_no_worker_until_a_capture_is_due() {
    let t = tempfile::tempdir().unwrap();
    transcript(&t);
    for args in [&["index"][..], &["archive", "--on"], &["archive"]] {
        let out = agrep(&t).args(args).output().unwrap();
        assert!(
            out.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let out = agrep(&t)
        .env("AGREP_QUERY_TIMING", "1")
        .args(["search", "database migration"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(stderr.contains(r#""archive_due":false"#), "{stderr}");
    assert!(!stderr.contains("worker_started"), "{stderr}");
}

// 0.4.9 left an empty recall unverified (exit 2) when its automatic meaning lane failed
// (py/recall.py:2353-2357, 2766-2769); a keyword answer still exits 0.
#[test]
fn recall_with_a_failed_meaning_lane_and_no_match_is_unverified() {
    let t = tempfile::tempdir().unwrap();
    transcript(&t);
    let out = agrep(&t).arg("index").output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let r = DataRoot::open(t.path().join("data"), false).unwrap();
    // Every row embedded, so no worker starts and the lane loads the model.
    store::writer(&r)
        .unwrap()
        .execute_batch(
            "INSERT INTO semantic_refs SELECT id,digest,digest FROM prose; DELETE FROM semantic_queue;",
        )
        .unwrap();
    // An installed model whose weights fail verification.
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    for (query, code) in [
        ("quantum entanglement teleportation", 2),
        ("database migration failure", 0),
    ] {
        let out = agrep(&t).args(["recall", query]).output().unwrap();
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert_eq!(out.status.code(), Some(code), "{query}: {stderr}");
        assert!(
            stderr.contains("semantic search failed"),
            "{query}: {stderr}"
        );
    }
}

// 0.4.9 served the compact page, and so the automatic meaning lane, only on the default row
// surface (py/compact.py:128-139, py/search.py:7375-7381, 7653-7658): `--json`, `--flat`, `-l`
// and `-c` stay keyword-only for an agent, and `-l` keeps its chat listing.
#[test]
fn compact_profile_machine_surfaces_never_start_the_meaning_lane() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join(".pi/agent/sessions/fixture");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("case.jsonl"), concat!(
        "{\"type\":\"session\",\"id\":\"semantic-surface-fixture\",\"version\":3,\"cwd\":\"/fixture\",\"timestamp\":\"2026-10-29T10:00:00Z\"}\n",
        "{\"type\":\"message\",\"id\":\"m1\",\"parentId\":null,\"timestamp\":\"2026-10-29T10:01:00Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"database migration failure\"}]}}\n"
    )).unwrap();
    let out = Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path())
        .env("AGREP_HOME", t.path())
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"))
        .arg("index")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let r = DataRoot::open(t.path().join("data"), false).unwrap();
    store::writer(&r)
        .unwrap()
        .execute(
            "INSERT INTO semantic_refs SELECT id,digest,digest FROM prose",
            [],
        )
        .unwrap();
    // A started lane would fail on these weights and say so on stderr.
    fs::create_dir(t.path().join("model")).unwrap();
    for (name, size, _) in agrep_core::semantic_model::FILES {
        fs::File::create(t.path().join("model").join(name))
            .unwrap()
            .set_len(size)
            .unwrap();
    }
    let query = "database migration failure";
    let json = run(&t, &["search", "--no-auto", "--json", query]);
    let lines: Vec<serde_json::Value> = String::from_utf8_lossy(&json.stdout)
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(lines.len(), 2);
    assert_eq!(lines[0]["engine"], "sqlite");
    assert_eq!(lines[0]["semantic_coverage"], serde_json::Value::Null);
    assert!(lines[0].get("semantic_partial").is_none());
    assert!(lines[1].get("sem_score").is_none());
    let listing = run(&t, &["search", "--no-auto", "-l", query]);
    let listing = String::from_utf8_lossy(&listing.stdout).into_owned();
    assert!(
        listing.starts_with("pi · fixture  [@") && listing.contains(" · 1 hit · "),
        "{listing}"
    );
    for out in [
        json,
        run(&t, &["search", "--no-auto", "-l", query]),
        run(&t, &["search", "--no-auto", "--flat", query]),
        run(&t, &["search", "--no-auto", "-c", query]),
    ] {
        assert_eq!(out.status.code(), Some(0));
        assert!(
            !String::from_utf8_lossy(&out.stderr).contains("semantic"),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
}

#[test]
fn first_missing_model_request_has_only_the_setup_instruction() {
    let t = tempfile::tempdir().unwrap();
    let out = run(&t, &["search", "-s", "database migration failure"]);
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&out.stderr),
        "semantic model not installed: run agrep setup\n"
    );
    assert!(!t.path().join("data").exists());
}
