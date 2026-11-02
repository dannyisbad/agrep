mod support;
use agrep_core::{
    archive,
    data::DataRoot,
    integrations::{self, Target},
};
use rusqlite::Connection;
use std::{
    fs,
    path::PathBuf,
    process::{Command, Output},
};
use tempfile::TempDir;
struct Fixture {
    _tmp: TempDir,
    home: PathBuf,
    data: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        let data = t.path().join("data");
        fs::create_dir(&home).unwrap();
        Self {
            _tmp: t,
            home,
            data,
        }
    }
    fn cmd(&self, args: &[&str]) -> Output {
        Command::new(support::binary())
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", &self.home)
            .env("USERPROFILE", &self.home)
            .env("AGREP_HOME", &self.home)
            .env("AGREP_DATA_DIR", &self.data)
            .args(args)
            .output()
            .unwrap()
    }
    fn ok(&self, args: &[&str]) -> Output {
        let o = self.cmd(args);
        assert!(
            o.status.success(),
            "{args:?}: {} {}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        );
        o
    }
    fn source(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let p = self.home.join(".codex/sessions/2026/10/30").join(name);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(&p, bytes).unwrap();
        p
    }
    fn root(&self) -> DataRoot {
        DataRoot::open(&self.data, true).unwrap()
    }
}
#[test]
fn every_target_preserves_user_text_and_crlf_replaces_any_version() {
    let f = Fixture::new();
    for eol in ["\n", "\r\n"] {
        for mut t in integrations::targets_in(&f.home, &Default::default()) {
            fs::create_dir_all(t.path.parent().unwrap()).unwrap();
            let old=format!("user before{eol}<!-- agrep:recall v999 -->{eol}user edited owned block{eol}<!-- /agrep:recall -->{eol}user after{eol}");
            fs::write(&t.path, old).unwrap();
            t.detected = true;
            let installed = integrations::install_block(&t).unwrap();
            let new = fs::read_to_string(&t.path).unwrap();
            assert!(new.starts_with(&format!("user before{eol}")), "{}", t.name);
            assert!(new.ends_with(&format!("user after{eol}")), "{}", t.name);
            assert!(!new.contains("user edited"));
            assert!(new.contains("agrep postcompact"));
            if eol == "\r\n" {
                assert!(!new.replace("\r\n", "").contains('\n'));
            }
            let integrations::Kind::Block { separator, skill } = installed.kind else {
                panic!()
            };
            integrations::remove_block(&t.path, &separator, skill, installed.created).unwrap();
            assert_eq!(
                fs::read_to_string(&t.path).unwrap(),
                format!("user before{eol}user after{eol}")
            );
        }
    }
}
#[test]
fn missing_instructions_roundtrip_exact_and_duplicate_blocks_removed() {
    let f = Fixture::new();
    let p = f.home.join("AGENTS.md");
    for original in ["", "user without newline", "user\n", "user\n\n", "user\r\n"] {
        fs::write(&p, original).unwrap();
        let t = Target {
            name: "codex".into(),
            path: p.clone(),
            detected: true,
            skill: false,
        };
        let i = integrations::install_block(&t).unwrap();
        let integrations::Kind::Block { separator, skill } = i.kind else {
            panic!()
        };
        integrations::remove_block(&p, &separator, skill, i.created).unwrap();
        assert_eq!(fs::read_to_string(&p).unwrap(), original);
    }
    fs::write(&p,"a\n<!-- agrep:recall v2 -->\nx\n<!-- /agrep:recall -->\nb\n<!-- agrep:recall v3 -->\ny\n<!-- /agrep:recall -->\nc\n").unwrap();
    let t = Target {
        name: "codex".into(),
        path: p.clone(),
        detected: true,
        skill: false,
    };
    integrations::install_block(&t).unwrap();
    let raw = fs::read_to_string(p).unwrap();
    assert_eq!(raw.matches("<!-- agrep:recall").count(), 1);
    assert!(raw.ends_with("b\nc\n"));
}
#[test]
fn setup_remove_enrollment_and_unrelated_hook_edits() {
    let f = Fixture::new();
    for name in [".claude", ".codex", ".pi/agent", ".omp/agent", ".cursor"] {
        fs::create_dir_all(f.home.join(name)).unwrap();
    }
    let claude = f.home.join(".claude/settings.json");
    let codex = f.home.join(".codex/hooks.json");
    fs::write(&claude,"{\r\n  \"theme\":\"dark\",\r\n  \"hooks\":{\"PreCompact\":[{\"matcher\":\"manual\",\"hooks\":[{\"type\":\"command\",\"command\":\"echo user\"}]}]}\r\n}\r\n").unwrap();
    fs::write(&codex,r#"{"user":true,"hooks":{"SessionStart":[{"matcher":"startup","hooks":[{"type":"command","command":"echo user"}]}]}}"#).unwrap();
    let args = ["setup", "--yes", "--no-semantic", "--no-archive"];
    f.ok(&args);
    let before = f.root().config().unwrap();
    assert!(before.enrollment.len() >= 9);
    f.ok(&args);
    assert_eq!(
        f.root().config().unwrap().enrollment.len(),
        before.enrollment.len()
    );
    let s = fs::read_to_string(&claude).unwrap();
    assert!(s.contains("echo user"));
    assert!(!s.replace("\r\n", "").contains('\n'));
    let instructions = f.home.join(".codex/AGENTS.md");
    fs::OpenOptions::new()
        .append(true)
        .open(&instructions)
        .unwrap();
    let mut raw = fs::read(&instructions).unwrap();
    raw.extend_from_slice(b"after setup user text\n");
    fs::write(&instructions, raw).unwrap();
    let index = fs::read(f.data.join("index.db")).unwrap();
    f.ok(&["remove"]);
    assert_eq!(
        fs::read_to_string(instructions).unwrap(),
        "after setup user text\n"
    );
    assert_eq!(fs::read(f.data.join("index.db")).unwrap(), index);
    for p in [claude, codex] {
        let v: serde_json::Value = serde_json::from_slice(&fs::read(p).unwrap()).unwrap();
        assert!(!v.to_string().contains("agrep __hook"));
        assert!(v.to_string().contains("echo user"));
    }
    assert!(!f
        .home
        .join(".pi/agent/extensions/agrep-postcompact.ts")
        .exists());
    assert!(!f.home.join(".cursor/skills/agrep-recall/SKILL.md").exists());
    assert!(f.root().config().unwrap().enrollment.is_empty());
}
#[test]
fn setup_headless_optouts_readonly_and_user_payload() {
    let f = Fixture::new();
    fs::create_dir_all(f.home.join(".codex")).unwrap();
    f.ok(&["setup", "--no-semantic", "--no-archive"]);
    assert!(!f.home.join(".codex/AGENTS.md").exists());
    assert!(!f.home.join(".codex/hooks.json").exists());
    f.ok(&[
        "setup",
        "--yes",
        "--no-teach",
        "--no-hook",
        "--no-semantic",
        "--archive",
    ]);
    assert!(f.root().config().unwrap().archive.enabled);
    let p = f.home.join(".codex/AGENTS.md");
    fs::write(&p, "user\n").unwrap();
    let mut permissions = fs::metadata(&p).unwrap().permissions();
    permissions.set_readonly(true);
    fs::set_permissions(&p, permissions).unwrap();
    let o = f.cmd(&["setup", "--yes", "--no-semantic", "--no-archive"]);
    assert_eq!(o.status.code(), Some(1));
    assert_eq!(fs::read_to_string(&p).unwrap(), "user\n");
    assert!(f.home.join(".codex/hooks.json").exists());
    let mut permissions = fs::metadata(&p).unwrap().permissions();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        permissions.set_mode(0o600);
    }
    #[cfg(windows)]
    permissions.set_readonly(false);
    fs::set_permissions(&p, permissions).unwrap();
    fs::create_dir_all(f.home.join(".pi/agent/extensions")).unwrap();
    let payload = f.home.join(".pi/agent/extensions/agrep-postcompact.ts");
    fs::write(&payload, "user extension").unwrap();
    assert_eq!(
        f.cmd(&["setup", "--yes", "--no-semantic", "--no-archive"])
            .status
            .code(),
        Some(1)
    );
    assert_eq!(fs::read_to_string(payload).unwrap(), "user extension");
}
#[test]
fn marker_without_enrollment_removed_and_malformed_marker_refused() {
    let f = Fixture::new();
    let p = f.home.join(".codex/AGENTS.md");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(
        &p,
        "user\n<!-- agrep:recall v1 -->\nowned\n<!-- /agrep:recall -->\ntail\n",
    )
    .unwrap();
    f.ok(&["remove"]);
    assert_eq!(fs::read_to_string(&p).unwrap(), "user\ntail\n");
    fs::write(&p, "user\n<!-- agrep:recall v1 -->\nno end\n").unwrap();
    assert_eq!(f.cmd(&["remove"]).status.code(), Some(1));
    assert!(fs::read_to_string(p).unwrap().contains("no end"));
}
#[test]
fn settings_validation_and_exact_reference_lines() {
    let f = Fixture::new();
    let out = f.ok(&["set"]);
    assert!(String::from_utf8_lossy(&out.stdout).contains("  tools = on   (on/off)"));
    for args in [
        ["set", "tools", "off"],
        ["set", "embeddings", "off"],
        ["set", "post_index", "echo done"],
    ] {
        f.ok(&args);
    }
    let c = f.root().config().unwrap();
    assert!(!c.tools);
    assert_eq!(c.embeddings, "off");
    assert_eq!(c.post_index.as_deref(), Some("echo done"));
    for args in [
        &["set", "tools", "on", "extra"][..],
        &["set", "embeddings", "bad"],
        &["set", "wat", "value"],
        &["set", "tools"],
        &["set", "post_index", ""],
    ] {
        assert_eq!(f.cmd(args).status.code(), Some(2));
    }
    f.ok(&["set", "post_index", "off"]);
    assert!(f.root().config().unwrap().post_index.is_none());
}
#[test]
fn archive_exact_plain_append_rewrite_retention_purge() {
    let f = Fixture::new();
    let initial = [0, 1, 2, 255, 0, 13, 10];
    let p = f.source("rollout-12345678.jsonl", &initial);
    f.ok(&["archive"]);
    f.ok(&["archive"]);
    let catalog = f.data.join("archive/catalog.db");
    let db = Connection::open(&catalog).unwrap();
    let versions = || {
        db.query_row("SELECT count(*) FROM versions", [], |r| r.get::<_, i64>(0))
            .unwrap()
    };
    assert_eq!(versions(), 1);
    assert_eq!(f.cmd(&["restore", "12345678"]).status.code(), Some(1));
    fs::remove_file(&p).unwrap();
    f.ok(&["restore", "12345678"]);
    assert_eq!(fs::read(&p).unwrap(), initial);
    let mut appended = initial.to_vec();
    appended.extend_from_slice(b"appended");
    fs::write(&p, &appended).unwrap();
    let o = f.ok(&["archive"]);
    assert!(String::from_utf8_lossy(&o.stdout).contains("1 appended"));
    fs::write(&p, b"rewritten").unwrap();
    let o = f.ok(&["archive"]);
    assert!(String::from_utf8_lossy(&o.stdout).contains("1 full captures"));
    assert_eq!(versions(), 3);
    f.ok(&["restore", "12345678", "--force"]);
    assert_eq!(fs::read(&p).unwrap(), b"rewritten");
    f.ok(&["archive", "--keep", "1"]);
    f.ok(&["archive", "--prune"]);
    assert_eq!(versions(), 1);
    f.ok(&["archive", "--on"]);
    assert_eq!(f.cmd(&["archive", "--purge"]).status.code(), Some(1));
    assert_eq!(versions(), 1);
    f.ok(&["archive", "--purge", "--yes"]);
    assert_eq!(versions(), 0);
    assert!(f.root().config().unwrap().archive.enabled);
    assert!(p.exists());
    assert_eq!(f.cmd(&["archive", "--yes"]).status.code(), Some(2));
}
#[test]
fn archive_chunks_dedupe_and_export_tree() {
    let f = Fixture::new();
    f.ok(&["archive", "--keep", "0"]);
    let mut bytes = vec![b'a'; 1024 * 1024 + 21];
    let p = f.source("rollout-growing.jsonl", &bytes);
    f.ok(&["archive"]);
    bytes.extend(vec![b'b'; 1024 * 1024]);
    fs::write(&p, &bytes).unwrap();
    f.ok(&["archive"]);
    let db = Connection::open(f.data.join("archive/catalog.db")).unwrap();
    let common: i64 = db
        .query_row(
            "SELECT count(*) FROM (SELECT sha FROM chunks GROUP BY sha HAVING count(*)>1)",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(common >= 1);
    let to = f.home.join("export");
    f.ok(&["restore", "growing", "--to", to.to_str().unwrap()]);
    assert_eq!(
        fs::read(to.join(p.strip_prefix(&f.home).unwrap())).unwrap(),
        bytes
    );
}
#[test]
fn archive_sqlite_wal_backup_force_restore_waits_for_writer() {
    let f = Fixture::new();
    let p = f.home.join(".local/share/opencode/opencode.db");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    let db = Connection::open(&p).unwrap();
    db.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE example(value); INSERT INTO example VALUES('committed in WAL');").unwrap();
    f.ok(&["archive"]);
    let export = f.home.join("export");
    f.ok(&["restore", "opencode.db", "--to", export.to_str().unwrap()]);
    let recovered = Connection::open(export.join(".local/share/opencode/opencode.db")).unwrap();
    assert_eq!(
        recovered
            .query_row("SELECT value FROM example", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "committed in WAL"
    );
    drop(recovered);
    db.execute_batch("BEGIN IMMEDIATE; INSERT INTO example VALUES('uncommitted');")
        .unwrap();
    std::thread::scope(|scope| {
        let task = scope.spawn(|| f.cmd(&["restore", "opencode.db", "--force"]));
        std::thread::sleep(std::time::Duration::from_millis(150));
        assert!(!task.is_finished());
        db.execute_batch("ROLLBACK").unwrap();
        let out = task.join().unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    });
    db.execute("DELETE FROM example", []).unwrap();
    f.ok(&["restore", "opencode.db", "--force"]);
    assert_eq!(
        db.query_row("SELECT count(*) FROM example", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
}
#[test]
fn archive_tamper_and_traversal_refusal() {
    let f = Fixture::new();
    let p = f.source("rollout-secure.jsonl", b"data");
    f.ok(&["archive"]);
    let db = Connection::open(f.data.join("archive/catalog.db")).unwrap();
    db.execute("UPDATE versions SET home_relative='../escaped'", [])
        .unwrap();
    assert_eq!(
        f.cmd(&["restore", "secure", "--force"]).status.code(),
        Some(2)
    );
    assert_eq!(fs::read(&p).unwrap(), b"data");
    db.execute(
        "UPDATE versions SET home_relative='.codex/sessions/2026/10/30/rollout-secure.jsonl'",
        [],
    )
    .unwrap();
    let hash: String = db
        .query_row("SELECT sha FROM chunks", [], |r| r.get(0))
        .unwrap();
    fs::write(f.data.join(format!("archive/blobs/{hash}.gz")), b"tampered").unwrap();
    assert_eq!(
        f.cmd(&["restore", "secure", "--force"]).status.code(),
        Some(1)
    );
    assert_eq!(fs::read(p).unwrap(), b"data");
    for path in [
        "/Users/alice/.codex/sessions/file.jsonl",
        "/home/alice/.codex/sessions/file.jsonl",
        "C:\\Users\\alice\\.codex\\sessions\\file.jsonl",
    ] {
        assert!(archive::portable_parts(path, true).is_ok());
    }
    for path in [
        "/home/../evil",
        "C:\\Users\\..\\evil",
        "/home/alice/CON",
        "/home/alice/evil:stream",
        "/home/alice/a\0b",
    ] {
        assert!(archive::portable_parts(path, true).is_err(), "{path:?}");
    }
}
#[test]
fn scheduled_archive_runs_once_only_when_enabled() {
    let f = Fixture::new();
    f.source("rollout-scheduled.jsonl", b"scheduled");
    f.ok(&["__work"]);
    assert!(!f.data.join("archive").exists());
    f.ok(&["archive", "--on"]);
    f.ok(&["__work"]);
    f.ok(&["__work"]);
    let s = archive::status(&f.root()).unwrap();
    assert_eq!(s["versions"], 1);
    f.ok(&["archive", "--off"]);
    assert_eq!(archive::status(&f.root()).unwrap()["versions"], 1);
}
#[test]
fn source_links_are_skipped_restore_is_strict_and_instruction_links_work() {
    #[cfg(unix)]
    use std::os::unix::fs::symlink;
    #[cfg(windows)]
    use std::os::windows::fs::symlink_file as symlink;
    let f = Fixture::new();
    let p = f.source("rollout-link.jsonl", b"original");
    let outside = f.home.join("outside");
    fs::write(&outside, b"outside").unwrap();
    fs::remove_file(&p).unwrap();
    symlink(&outside, &p).unwrap();
    assert_eq!(f.cmd(&["archive"]).status.code(), Some(0));
    assert_eq!(f.cmd(&["archive", "--status"]).status.code(), Some(0));
    assert_eq!(fs::read(&outside).unwrap(), b"outside");
    fs::remove_file(&p).unwrap();
    fs::write(&p, b"safe").unwrap();
    f.ok(&["archive"]);
    fs::remove_file(&p).unwrap();
    symlink(&outside, &p).unwrap();
    assert_eq!(
        f.cmd(&["restore", "link", "--force"]).status.code(),
        Some(2)
    );
    assert_eq!(fs::read(&outside).unwrap(), b"outside");
    let instructions = f.home.join(".codex/AGENTS.md");
    symlink(&outside, &instructions).unwrap();
    assert_eq!(
        f.cmd(&[
            "setup",
            "--yes",
            "--no-hook",
            "--no-semantic",
            "--no-archive"
        ])
        .status
        .code(),
        Some(0)
    );
    assert!(fs::read_to_string(&outside).unwrap().starts_with("outside"));
    assert!(fs::read_to_string(&outside)
        .unwrap()
        .contains("<!-- agrep:recall"));
    assert!(fs::symlink_metadata(&instructions)
        .unwrap()
        .file_type()
        .is_symlink());
}
#[test]
fn hook_payload_scopes_compaction_and_preserves_session() {
    let f = Fixture::new();
    let o = f.ok(&["__hook", "claude"]);
    assert!(String::from_utf8_lossy(&o.stdout).contains("agrep postcompact --session <id>"));
    use std::io::Write;
    let mut c = Command::new(support::binary())
        .args(["__hook", "codex"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    c.stdin
        .take()
        .unwrap()
        .write_all(
            br#"{"hook_event_name":"SessionStart","source":"compact","session_id":"abc-123"}"#,
        )
        .unwrap();
    let o = c.wait_with_output().unwrap();
    assert!(String::from_utf8_lossy(&o.stdout).contains("agrep postcompact --session abc-123"));
}

#[test]
fn registry_archives_every_agent_and_auxiliary_native_files() {
    let f = Fixture::new();
    let files = [
        ".claude/projects/project/session.jsonl",
        ".codex/archived_sessions/rollout-archived.jsonl",
        ".gemini/tmp/project/chats/session-x.json",
        ".gemini/tmp/project/chats/session-x.jsonl",
        ".gemini/antigravity-cli/brain/id/chat.json",
        ".kimi/sessions/id/chat.jsonl",
        ".kimi/kimi.json",
        ".local/share/opencode/storage/session/id.json",
        ".cline/data/tasks/id/api_conversation_history.json",
        ".cline/data/tasks/id/ui_messages.json",
        ".cline/data/state/taskHistory.json",
        ".pi/agent/sessions/project/session.jsonl",
        ".omp/agent/archive/sessions/project/session.jsonl",
    ];
    for p in files {
        let p = f.home.join(p);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, b"native bytes").unwrap();
    }
    for p in [
        ".local/share/crush/crush.db",
        ".local/share/opencode/opencode.db",
        ".config/Cursor/User/globalStorage/state.vscdb",
        "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        "AppData/Roaming/Cursor/User/globalStorage/state.vscdb",
    ] {
        let p = f.home.join(p);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        let db = Connection::open(p).unwrap();
        db.execute_batch("CREATE TABLE native(value); INSERT INTO native VALUES(123);")
            .unwrap();
    }
    f.ok(&["archive"]);
    let db = Connection::open(f.data.join("archive/catalog.db")).unwrap();
    for p in files {
        let count: i64 = db
            .query_row(
                "SELECT count(*) FROM versions WHERE home_relative=?",
                [p],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1, "{p}");
    }
    let count: i64 = db
        .query_row("SELECT count(DISTINCT agent) FROM versions", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(count, 10);
}

#[test]
fn config_preserves_other_settings_and_semantic_optout_does_not_download() {
    let f = Fixture::new();
    let root = f.root();
    let mut c = root.config().unwrap();
    c.board_view = Some(agrep_core::live::board::Preference {
        agent: Some("pi".into()),
        expanded: true,
    });
    root.save_config(&c).unwrap();
    f.ok(&["set", "tools", "off"]);
    f.ok(&["set", "embeddings", "off"]);
    f.ok(&["setup", "--yes", "--no-teach", "--no-hook", "--no-archive"]);
    assert!(!f.data.join("model").exists());
    assert_eq!(
        root.config().unwrap().board_view.unwrap().agent.as_deref(),
        Some("pi")
    );
}

#[test]
fn model_provisioning_validates_before_downloading() {
    let f = Fixture::new();
    let root = f.root();
    let dir = root.path().join("model-test");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("config.json"), b"corrupt").unwrap();
    let error = agrep_core::semantic_model::provision_at(&dir)
        .unwrap_err()
        .to_string();
    assert!(error.contains("incorrect model file size"));
    assert!(!dir.join("tokenizer.json").exists());
}

#[test]
fn archive_empty_purge_and_restore_ambiguity() {
    let f = Fixture::new();
    assert_eq!(f.cmd(&["archive", "--purge"]).status.code(), Some(0));
    f.source("rollout-one.jsonl", b"one");
    f.source("rollout-two.jsonl", b"two");
    f.ok(&["archive"]);
    assert_eq!(f.cmd(&["restore", "rollout"]).status.code(), Some(1));
    let to = f.home.join("export");
    f.ok(&["restore", "rollout", "--to", to.to_str().unwrap()]);
    assert!(to
        .join(".codex/sessions/2026/10/30/rollout-one.jsonl")
        .exists());
    assert!(to
        .join(".codex/sessions/2026/10/30/rollout-two.jsonl")
        .exists());
}

#[test]
fn dynamic_client_paths_and_recorded_removal_after_environment_changes() {
    let f = Fixture::new();
    let codex = f.home.join("codex-profile");
    let xdg = f.home.join("config-custom");
    let omp = f.home.join(".omp/profiles/test/agent");
    for path in [&codex, &omp, &f.home.join(".config/amp")] {
        fs::create_dir_all(path).unwrap();
    }
    let output = Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("AGREP_HOME", &f.home)
        .env("HOME", &f.home)
        .env("AGREP_DATA_DIR", &f.data)
        .env("CODEX_HOME", &codex)
        .env("XDG_CONFIG_HOME", &xdg)
        .env("OMP_PROFILE", "test")
        .args(["setup", "--yes", "--no-semantic", "--no-archive"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(codex.join("AGENTS.md").exists());
    assert!(codex.join("hooks.json").exists());
    assert!(xdg.join("amp/AGENTS.md").exists());
    assert!(omp.join("extensions/agrep-postcompact.ts").exists());
    f.ok(&["remove"]);
    assert!(!codex.join("AGENTS.md").exists());
    assert!(!codex.join("hooks.json").exists());
    assert!(!omp.join("extensions/agrep-postcompact.ts").exists());
    assert!(!xdg.join("amp").exists());
}

#[test]
fn restore_readonly_and_orphan_wal_refusal() {
    let f = Fixture::new();
    let source = f.source("rollout-readonly.jsonl", b"before");
    f.ok(&["archive"]);
    fs::write(&source, b"changed").unwrap();
    let mut p = fs::metadata(&source).unwrap().permissions();
    p.set_readonly(true);
    fs::set_permissions(&source, p).unwrap();
    assert_eq!(
        f.cmd(&["restore", "readonly", "--force"]).status.code(),
        Some(1)
    );
    assert_eq!(fs::read(&source).unwrap(), b"changed");
    let mut p = fs::metadata(&source).unwrap().permissions();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        p.set_mode(0o600);
    }
    #[cfg(windows)]
    p.set_readonly(false);
    fs::set_permissions(&source, p).unwrap();
    let path = f.home.join(".local/share/opencode/opencode.db");
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let db = Connection::open(&path).unwrap();
    db.execute_batch("CREATE TABLE data(value)").unwrap();
    drop(db);
    f.ok(&["archive"]);
    fs::remove_file(&path).unwrap();
    fs::write(path.with_file_name("opencode.db-wal"), b"orphan").unwrap();
    assert_eq!(f.cmd(&["restore", "opencode.db"]).status.code(), Some(1));
    assert!(!path.exists());
}

#[test]
fn restore_selects_session_family_and_case_insensitive_paths() {
    let f = Fixture::new();
    let session = "12345678-abcd-abcd-abcd-123456789012";
    let a = f
        .home
        .join(format!(".claude/projects/project/{session}.jsonl"));
    let b = f.home.join(format!(
        ".claude/projects/project/{session}/subagents/agent-Abc.jsonl"
    ));
    for p in [&a, &b] {
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, b"native").unwrap();
    }
    f.ok(&["archive"]);
    fs::remove_file(&a).unwrap();
    fs::remove_file(&b).unwrap();
    f.ok(&["restore", "12345678-ABCD"]);
    assert_eq!(fs::read(&a).unwrap(), b"native");
    assert_eq!(fs::read(&b).unwrap(), b"native");
}

#[test]
fn every_target_missing_and_readonly_files() {
    let f = Fixture::new();
    for t in integrations::targets_in(&f.home, &Default::default()) {
        let installed = integrations::install_block(&t).unwrap();
        let integrations::Kind::Block { separator, skill } = installed.kind else {
            panic!()
        };
        integrations::remove_block(&t.path, &separator, skill, installed.created).unwrap();
        assert!(!t.path.exists());
        fs::create_dir_all(t.path.parent().unwrap()).unwrap();
        fs::write(
            &t.path,
            "user\r\n<!-- agrep:recall v999 -->\r\nold\r\n<!-- /agrep:recall -->\r\nafter\r\n",
        )
        .unwrap();
        let before = fs::read(&t.path).unwrap();
        let mut p = fs::metadata(&t.path).unwrap().permissions();
        p.set_readonly(true);
        fs::set_permissions(&t.path, p).unwrap();
        assert!(integrations::install_block(&t).is_err(), "{}", t.name);
        assert_eq!(fs::read(&t.path).unwrap(), before);
        let mut p = fs::metadata(&t.path).unwrap().permissions();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            p.set_mode(0o600);
        }
        #[cfg(windows)]
        p.set_readonly(false);
        fs::set_permissions(&t.path, p).unwrap();
    }
}

#[test]
fn archive_operations_wait_for_concurrent_capture() {
    let f = Fixture::new();
    f.root();
    let dir = f.data.join("archive");
    fs::create_dir_all(&dir).unwrap();
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .read(true)
        .open(dir.join("archive.lock"))
        .unwrap();
    fs2::FileExt::lock_exclusive(&lock).unwrap();
    let (sent, received) = std::sync::mpsc::channel();
    let home = f.home.clone();
    let data = f.data.clone();
    let task = std::thread::spawn(move || {
        let out = Command::new(support::binary())
            .env_clear()
            .env("HOME", &home)
            .env("AGREP_HOME", &home)
            .env("AGREP_DATA_DIR", data)
            .arg("archive")
            .output()
            .unwrap();
        sent.send(out).unwrap();
    });
    assert!(received
        .recv_timeout(std::time::Duration::from_millis(150))
        .is_err());
    fs2::FileExt::unlock(&lock).unwrap();
    let out = received
        .recv_timeout(std::time::Duration::from_secs(10))
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!String::from_utf8_lossy(&out.stdout).contains("busy"));
    task.join().unwrap();
}
