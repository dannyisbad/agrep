mod support;
use agrep_core::{data::DataRoot, pages};
use std::{
    fs,
    process::{Command, Output},
};
use tempfile::TempDir;
fn cli(t: &TempDir, args: &[&str]) -> Output {
    Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_MODEL_DIR", t.path().join("model"))
        .env("AGREP_CALLER_PUBLICATION_DIR", t.path().join("callers"))
        .args(args)
        .output()
        .unwrap()
}
fn ok(t: &TempDir, args: &[&str]) -> Output {
    let out = cli(t, args);
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    out
}
fn home() -> TempDir {
    let t = tempfile::tempdir().unwrap();
    fs::create_dir_all(t.path().join("home/.pi/agent")).unwrap();
    t
}
#[test]
fn setup_yes_preserves_archive_optout_and_doctor_repairs_only_enrollment() {
    let t = home();
    ok(
        &t,
        &[
            "setup",
            "--yes",
            "--no-hook",
            "--no-semantic",
            "--no-archive",
        ],
    );
    let root = DataRoot(t.path().join("data"));
    let enrolled = root.config().unwrap().enrollment;
    fs::write(t.path().join("home/.pi/agent/AGENTS.md"), "user text\n").unwrap();
    ok(&t, &["doctor", "--fix", "--json"]);
    assert!(!root.config().unwrap().archive.enabled);
    assert_eq!(enrolled.len(), root.config().unwrap().enrollment.len());
    assert!(
        fs::read_to_string(t.path().join("home/.pi/agent/AGENTS.md"))
            .unwrap()
            .starts_with("user text\n")
    );
    assert!(!t.path().join("home/.pi/agent/extensions").exists());
    ok(
        &t,
        &["setup", "--yes", "--no-teach", "--no-hook", "--no-semantic"],
    );
    assert!(!root.config().unwrap().archive.enabled);
}
#[test]
fn zero_byte_capture_never_replaces_latest_nonempty_source() {
    let t = home();
    let path = t.path().join("home/.codex/sessions/rollout-test.jsonl");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, b"retained content").unwrap();
    ok(&t, &["archive"]);
    fs::write(&path, b"").unwrap();
    ok(&t, &["archive"]);
    ok(&t, &["restore", "rollout-test", "--force"]);
    assert_eq!(fs::read(path).unwrap(), b"retained content");
}
#[test]
fn pages_advance_with_zero_limit_and_do_not_need_writer_lock() {
    let t = home();
    let root = DataRoot::open(t.path().join("data"), true).unwrap();
    let frozen = pages::Frozen {
        records: (0..24)
            .map(|n| pages::Record {
                line: format!("@chat:{n} result"),
                family: n.to_string(),
                score: 1.,
                lane: "phrase".into(),
                tool: false,
                identity: n.to_string(),
            })
            .collect(),
        query: "result".into(),
        total: 24,
        exact: true,
        shown: 0,
        requested: Some(0),
        chat_scoped: false,
        full_argv: vec![],
    };
    let page = pages::select(frozen);
    assert_eq!(page.remainder.shown, 16);
    assert_eq!(page.remainder.records.len(), 8);
    let _lock = root.lock(true).unwrap();
    let handle = pages::save(&root, &page.remainder).unwrap().unwrap();
    assert_eq!(pages::load(&root, &handle).unwrap().records.len(), 8);
}
#[test]
fn no_home_path_component_can_expand_archive_selection() {
    let t = home();
    let storage_home = t.path().join("storage/home");
    let root = storage_home.join(".local/share/opencode");
    fs::create_dir_all(root.join("storage/session")).unwrap();
    fs::write(root.join("auth.json"), b"secret").unwrap();
    fs::write(root.join("storage/session/session.json"), b"history").unwrap();
    let out = Command::new(support::binary())
        .env_clear()
        .env("HOME", &storage_home)
        .env("AGREP_HOME", &storage_home)
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .arg("archive")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let c = rusqlite::Connection::open(t.path().join("data/archive/catalog.db")).unwrap();
    let paths: Vec<String> = c
        .prepare("SELECT path FROM versions")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(paths.len(), 1);
    assert!(paths[0].ends_with("session.json"));
}
#[test]
fn human_diagnostics_and_help_are_readable() {
    let t = home();
    ok(&t, &["index"]);
    for command in ["status", "doctor"] {
        let out = ok(&t, &[command]);
        assert!(!String::from_utf8_lossy(&out.stdout).contains("{\""));
    }
    for command in ["setup", "remove", "archive", "restore", "set"] {
        let out = ok(&t, &[command, "--help"]);
        assert!(out.stdout.len() > 160);
    }
}

#[test]
fn alias_resolution_uses_source_metadata_and_safe_followups() {
    let t = home();
    let p = t
        .path()
        .join("home/.pi/agent/sessions/project/2026-01-01_original-name.jsonl");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(&p, concat!("{\"type\":\"session\",\"version\":3,\"id\":\"sameprefix-session-one\",\"cwd\":\"/work/project\"}\n", "{\"type\":\"message\",\"id\":\"u\",\"parentId\":null,\"timestamp\":\"2026-01-01T00:00:00Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"alpha needle\"}]}}\n")).unwrap();
    ok(&t, &["index"]);
    let out = ok(&t, &["around", p.to_str().unwrap(), "0"]);
    assert!(String::from_utf8_lossy(&out.stdout).contains("alpha needle"));
    let c = agrep_core::store::writer(&DataRoot(t.path().join("data"))).unwrap();
    c.execute("INSERT INTO sessions(agent,id,project,cwd,parent,family,side,title,first_ts,last_ts) VALUES('pi','sameprefix-session-two','/work/project','','','sameprefix-session-two',0,'',0,0)",[]).unwrap();
    let w = agrep_core::evidence::load(
        &c,
        &agrep_core::evidence::Request::new("sameprefix-session-one", Some(0), Some(0)),
    )
    .unwrap();
    assert_eq!(
        agrep_core::handles::candidates(&c, &w.target).unwrap(),
        vec!["sameprefix-session-one"]
    );
}
#[test]
fn relative_data_root_settings_work() {
    let t = home();
    let out = Command::new(support::binary())
        .env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", "./data")
        .current_dir(t.path())
        .args(["set", "tools", "off"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!DataRoot(t.path().join("data")).config().unwrap().tools);
}

#[test]
fn post_index_hook_runs_after_changed_rows_and_only_once() {
    let t = home();
    let p = t.path().join("home/.pi/agent/sessions/project/chat.jsonl");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, concat!("{\"type\":\"session\",\"version\":3,\"id\":\"hook-session\",\"cwd\":\"/work/project\"}\n", "{\"type\":\"message\",\"id\":\"u\",\"parentId\":null,\"timestamp\":\"2026-01-01T00:00:00Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"hook input\"}]}}\n")).unwrap();
    let output = t.path().join("hook-output");
    let command = if cfg!(windows) {
        format!("echo x>>\"{}\"", output.display())
    } else {
        format!("printf x >> '{}'", output.display())
    };
    ok(&t, &["set", "post_index", &command]);
    let root = DataRoot(t.path().join("data"));
    {
        let _lock = root.work_lock(true).unwrap();
        ok(&t, &["index"]);
    }
    ok(&t, &["__work"]);
    assert_eq!(
        fs::read(&output).unwrap(),
        if cfg!(windows) {
            b"x\r\n".as_slice()
        } else {
            b"x".as_slice()
        }
    );
    {
        let _lock = root.work_lock(true).unwrap();
        ok(&t, &["index"]);
    }
    ok(&t, &["__work"]);
    assert_eq!(
        fs::read(output).unwrap(),
        if cfg!(windows) {
            b"x\r\n".as_slice()
        } else {
            b"x".as_slice()
        }
    );
}

#[test]
fn summary_handles_a_skipped_local_midnight() {
    let t = home();
    let path = t.path().join("home/.pi/agent/sessions/project/dst.jsonl");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut text =
        serde_json::json!({"type":"session","version":3,"id":"dst-session","cwd":"/work/dst"})
            .to_string()
            + "\n";
    for (i, ts) in ["2024-09-08T03:50:00Z", "2024-09-08T04:10:00Z"]
        .iter()
        .enumerate()
    {
        text += &(serde_json::json!({"type":"message","id":format!("u{i}"),"parentId":if i==0{None}else{Some("u0")},"timestamp":ts,"message":{"role":"user","content":[{"type":"text","text":format!("DST request {i}")}]}}).to_string()+"\n");
    }
    fs::write(path, text).unwrap();
    ok(&t, &["index"]);
    let out = Command::new(support::binary())
        .env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("AGREP_CALLER_PUBLICATION_DIR", t.path().join("callers"))
        .env("TZ", "America/Santiago")
        .args([
            "summary",
            "time",
            "--json",
            "--no-auto",
            "--since",
            "2024-09-07",
            "--until",
            "2024-09-09",
        ])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let meta: serde_json::Value =
        serde_json::from_str(String::from_utf8_lossy(&out.stdout).lines().next().unwrap()).unwrap();
    assert_eq!(meta["total_estimated_active_ms"], 1_200_000);
}

#[test]
fn doctor_creates_an_index_in_an_initialized_empty_root() {
    let t = home();
    let root = DataRoot::open(t.path().join("data"), true).unwrap();
    assert!(!root.db().exists());
    ok(&t, &["doctor", "--fix", "--json"]);
    assert!(root.db().is_file());
}
