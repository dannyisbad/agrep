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
    ok(&t, &["doctor", "--fix"]);
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
        exhaustible: true,
        larger_argv: None,
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
    #[cfg(windows)]
    for spelling in [
        p.to_string_lossy().replace('\\', "/"),
        p.to_string_lossy().replace('/', "\\"),
        p.to_string_lossy().to_uppercase(),
    ] {
        let out = ok(&t, &["around", &spelling, "0"]);
        assert!(String::from_utf8_lossy(&out.stdout).contains("alpha needle"));
    }
    let c = agrep_core::store::writer(&DataRoot(t.path().join("data"))).unwrap();
    c.execute("INSERT INTO sessions(agent,id,project,cwd,parent,family,side,title,first_ts,last_ts) VALUES('pi','sameprefix-session-two','/work/project','','','sameprefix-session-two',0,'',0,0)",[]).unwrap();
    let w = agrep_core::evidence::load(
        &c,
        &agrep_core::evidence::Request::new("sameprefix-session-one", Some(0), Some(0)),
    )
    .unwrap();
    assert_eq!(
        agrep_core::handles::SessionIndex::load(&c)
            .unwrap()
            .candidates(&c, &w.target)
            .unwrap(),
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
    ok(&t, &["index"]);
    ok(&t, &["__work"]);
    assert_eq!(
        fs::read(&output).unwrap(),
        if cfg!(windows) {
            b"x\r\n".as_slice()
        } else {
            b"x".as_slice()
        }
    );
    ok(&t, &["index"]);
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
    ok(&t, &["doctor", "--fix"]);
    assert!(root.db().is_file());
}

#[test]
fn help_word_prints_help_without_indexing() {
    let t = home();
    let top = ok(&t, &["--help"]);
    assert_eq!(ok(&t, &["help"]).stdout, top.stdout);
    assert_eq!(
        ok(&t, &["help", "search"]).stdout,
        ok(&t, &["search", "--help"]).stdout
    );
    assert!(!t.path().join("data").exists());
    for (words, refusal) in [
        (&["help", "deploy"][..], "unknown command 'deploy'"),
        (
            &["help", "deploy", "keys"][..],
            "help takes one command name",
        ),
    ] {
        let refused = cli(&t, words);
        assert_eq!(refused.status.code(), Some(2));
        assert_eq!(
            String::from_utf8_lossy(&refused.stderr),
            format!(
                "agrep: {refusal} (agrep --help lists the commands); to search for these words, run: agrep search {}\n",
                words.join(" ")
            )
        );
    }
    assert!(!t.path().join("data").exists());
    let searched = cli(&t, &["search", "help", "-c"]);
    assert_eq!(String::from_utf8_lossy(&searched.stdout), "0\n");
}

#[test]
fn advisor_sidecar_notes_stay_keyword_searchable() {
    let t = home();
    let chat = t
        .path()
        .join("home/.omp/agent/sessions/-work/2026-02-03T04-05-06-000Z_01940000-0000-7000-8000-000000000003");
    fs::create_dir_all(chat.join("Reviewer")).unwrap();
    fs::write(chat.with_extension("jsonl"), concat!(
        "{\"type\":\"session\",\"id\":\"01940000-0000-7000-8000-000000000003\",\"version\":3,\"cwd\":\"/work\"}\n",
        "{\"type\":\"message\",\"id\":\"u\",\"parentId\":null,\"timestamp\":\"2026-02-03T04:05:07Z\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"open the browser tab\"}]}}\n",
    )).unwrap();
    fs::write(chat.join("Reviewer/__advisor.jsonl"), concat!(
        "{\"type\":\"session\",\"id\":\"01940000-0000-7000-8000-000000000004\",\"version\":3,\"cwd\":\"/work\"}\n",
        "{\"type\":\"message\",\"id\":\"s\",\"parentId\":null,\"timestamp\":\"2026-02-03T04:05:08Z\",\"message\":{\"role\":\"user\",\"synthetic\":true,\"content\":[{\"type\":\"text\",\"text\":\"### Session update\"}]}}\n",
        "{\"type\":\"message\",\"id\":\"a\",\"parentId\":\"s\",\"timestamp\":\"2026-02-03T04:05:09Z\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"Browser tab opened, no navigation yet. Nothing to flag.\"}]}}\n",
    )).unwrap();
    ok(&t, &["index"]);
    let counted = ok(&t, &["Nothing to flag", "-c"]);
    assert_eq!(String::from_utf8_lossy(&counted.stdout), "1\n");
    let found = ok(&t, &["Nothing to flag", "--who", "subagent", "--flat"]);
    assert!(String::from_utf8_lossy(&found.stdout).contains("Browser tab opened"));
}

#[test]
fn version_word_prints_the_version_without_indexing() {
    let t = home();
    assert_eq!(ok(&t, &["version"]).stdout, ok(&t, &["--version"]).stdout);
    assert!(!t.path().join("data").exists());
    let searched = cli(&t, &["version", "bump", "-c"]);
    assert_eq!(String::from_utf8_lossy(&searched.stdout), "0\n");
}

#[test]
fn narrow_help_puts_descriptions_under_their_options() {
    let t = home();
    let help = |columns: &str| {
        let out = Command::new(support::binary())
            .env_clear()
            .env("COLUMNS", columns)
            .env("HOME", t.path().join("home"))
            .args(["search", "--help"])
            .output()
            .unwrap();
        String::from_utf8(out.stdout).unwrap()
    };
    let narrow = help("40");
    assert!(narrow.lines().count() < 200, "{narrow}");
    assert!(
        narrow.contains("  -n, --max <N>\n          show at most N hits"),
        "{narrow}"
    );
    assert!(help("80").contains("  -n, --max <N>\n"));
}

#[cfg(unix)]
fn copy_tree(from: &std::path::Path, to: &std::path::Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &to.join(entry.file_name()));
        } else {
            fs::copy(entry.path(), to.join(entry.file_name())).unwrap();
        }
    }
}

#[cfg(unix)]
#[test]
fn symlinked_store_roots_are_read_and_unreadable_ones_reported() {
    use std::os::unix::fs::symlink;
    let fixtures = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let sessions = |t: &TempDir| -> serde_json::Value {
        let status = ok(t, &["status", "--json"]);
        serde_json::from_slice::<serde_json::Value>(&status.stdout).unwrap()["index"]["counts"]
            .clone()
    };
    let plain = home();
    for agent in ["claude", "codex", "pi"] {
        copy_tree(
            &fixtures.join(agent).join("home"),
            &plain.path().join("home"),
        );
    }
    ok(&plain, &["index"]);
    let expected = sessions(&plain);
    let t = home();
    for agent in ["claude", "codex", "pi"] {
        copy_tree(&fixtures.join(agent).join("home"), &t.path().join("home"));
    }
    for (root, moved) in [
        (".codex/sessions", "elsewhere/codex"),
        (".claude/projects", "elsewhere/claude"),
        (".pi/agent/sessions", "elsewhere/pi"),
    ] {
        let target = t.path().join(moved);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::rename(t.path().join("home").join(root), &target).unwrap();
        symlink(&target, t.path().join("home").join(root)).unwrap();
    }
    ok(&t, &["index"]);
    assert_eq!(sessions(&t), expected);
    let doctor = ok(&t, &["doctor"]);
    assert!(String::from_utf8_lossy(&doctor.stdout).contains("checks passed"));
    // A root leading into another store, or nowhere, is reported rather than read twice or
    // passed off as an empty store.
    fs::remove_file(t.path().join("home/.pi/agent/sessions")).unwrap();
    symlink(
        t.path().join("home/.omp/agent/sessions"),
        t.path().join("home/.pi/agent/sessions"),
    )
    .unwrap();
    fs::create_dir_all(t.path().join("home/.gemini")).unwrap();
    symlink(t.path().join("nowhere"), t.path().join("home/.gemini/tmp")).unwrap();
    ok(&t, &["index"]);
    let status = String::from_utf8_lossy(&ok(&t, &["status"]).stdout).into_owned();
    for (root, reason) in [
        (".pi/agent/sessions", "which agrep already reads"),
        (".gemini/tmp", "its target cannot be reached"),
    ] {
        assert!(
            status
                .lines()
                .any(|line| line.starts_with("source failed: ")
                    && line.contains(root)
                    && line.contains(reason)),
            "{status}"
        );
    }
    let doctor = String::from_utf8_lossy(&ok(&t, &["doctor"]).stdout).into_owned();
    assert!(doctor.contains("sources: failed"), "{doctor}");
    // Repaired links are read again and their failures go.
    fs::remove_file(t.path().join("home/.pi/agent/sessions")).unwrap();
    symlink(
        t.path().join("elsewhere/pi"),
        t.path().join("home/.pi/agent/sessions"),
    )
    .unwrap();
    fs::remove_file(t.path().join("home/.gemini/tmp")).unwrap();
    fs::create_dir(t.path().join("nowhere")).unwrap();
    symlink(t.path().join("nowhere"), t.path().join("home/.gemini/tmp")).unwrap();
    ok(&t, &["index"]);
    assert_eq!(sessions(&t), expected);
    let doctor = ok(&t, &["doctor"]);
    assert!(String::from_utf8_lossy(&doctor.stdout).contains("checks passed"));
}

#[cfg(unix)]
#[test]
fn symlinked_file_store_roots_are_read_and_unreadable_ones_reported() {
    use std::os::unix::fs::symlink;
    use std::path::Path;
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let run = |t: &TempDir, args: &[&str]| -> String {
        let out = Command::new(support::binary())
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", t.path().join("home"))
            .env("AGREP_HOME", t.path().join("home"))
            .env("AGREP_DATA_DIR", t.path().join("data"))
            .env("AGREP_MODEL_DIR", t.path().join("model"))
            .env("AGREP_CALLER_PUBLICATION_DIR", t.path().join("callers"))
            .env("OPENCODE_DB", t.path().join("stores/explicit.db"))
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success(), "{args:?}: {out:?}");
        String::from_utf8(out.stdout).unwrap()
    };
    let indexed = |t: &TempDir| -> (serde_json::Value, serde_json::Value) {
        run(t, &["index"]);
        let status: serde_json::Value =
            serde_json::from_str(&run(t, &["status", "--json"])).unwrap();
        (status["index"]["counts"].clone(), status["agents"].clone())
    };
    let seed = |sql: &Path, db: &Path| {
        fs::create_dir_all(db.parent().unwrap()).unwrap();
        rusqlite::Connection::open(db)
            .unwrap()
            .execute_batch(&fs::read_to_string(sql).unwrap())
            .unwrap();
    };
    let cursor = ".config/Cursor/User/globalStorage/state.vscdb";
    let cursor_root = format!("home/{cursor}");
    let stores = |t: &TempDir| {
        let home = t.path().join("home");
        copy_tree(&fixtures.join("kimi/home"), &home);
        seed(
            &fixtures.join("opencode/seed.sql"),
            &home.join(".local/share/opencode/opencode.db"),
        );
        seed(
            &fixtures.join("opencode_v2/seed.sql"),
            &t.path().join("stores/explicit.db"),
        );
        seed(
            &fixtures.join("crush/seed.sql"),
            &home.join(".local/share/crush/crush.db"),
        );
        seed(&fixtures.join("cursor/global.sql"), &home.join(cursor));
    };
    // What audit and archive report, without the archive's timing.
    let coverage = |t: &TempDir| {
        let archived = run(t, &["archive"]);
        (
            run(t, &["audit"]),
            archived.split(" in ").next().unwrap().to_owned(),
        )
    };
    let plain = home();
    stores(&plain);
    let expected = indexed(&plain);
    assert_eq!(expected.1.as_array().unwrap().len(), 4, "{expected:?}");
    let covered = coverage(&plain);
    assert!(covered.1.contains("8 full captures"), "{covered:?}");
    let t = home();
    stores(&t);
    let roots = [
        ("home/.local/share/opencode", "opencode"),
        ("stores/explicit.db", "explicit.db"),
        ("home/.kimi/kimi.json", "kimi.json"),
        ("home/.local/share/crush/crush.db", "crush.db"),
        (cursor_root.as_str(), "state.vscdb"),
    ];
    let link = |root: &str, target: &Path| {
        let root = t.path().join(root);
        fs::remove_file(&root).unwrap();
        symlink(target, root).unwrap();
    };
    fs::create_dir(t.path().join("elsewhere")).unwrap();
    for (root, moved) in roots {
        let target = t.path().join("elsewhere").join(moved);
        fs::rename(t.path().join(root), &target).unwrap();
        symlink(&target, t.path().join(root)).unwrap();
    }
    assert_eq!(indexed(&t), expected);
    assert!(run(&t, &["doctor"]).contains("checks passed"));
    assert_eq!(coverage(&t), covered);
    // A link that dangles, leads to the wrong kind of entry or into another root is reported
    // with its reason, never read twice or passed off as an absent store.
    link(
        "home/.kimi/kimi.json",
        &t.path().join("elsewhere/missing.json"),
    );
    link(
        "home/.local/share/crush/crush.db",
        &t.path().join("elsewhere"),
    );
    link(
        "stores/explicit.db",
        &t.path().join("home/.local/share/opencode/opencode.db"),
    );
    link(&cursor_root, &t.path().join("elsewhere/gone"));
    run(&t, &["index"]);
    let status = run(&t, &["status"]);
    for (root, reason) in [
        (".kimi/kimi.json", "its target cannot be reached"),
        (".local/share/crush/crush.db", "which is not a regular file"),
        ("stores/explicit.db", "which agrep already reads"),
        (cursor, "its target cannot be reached"),
    ] {
        assert!(
            status
                .lines()
                .any(|line| line.starts_with("source failed: ")
                    && line.contains(root)
                    && line.contains(reason)),
            "{status}"
        );
    }
    assert!(run(&t, &["doctor"]).contains("sources: failed"));
    for (root, moved) in &roots[1..] {
        link(root, &t.path().join("elsewhere").join(moved));
    }
    assert_eq!(indexed(&t), expected);
    assert!(run(&t, &["doctor"]).contains("checks passed"));
    // Crush's data root, holding its registry and global database, may be a link as well.
    let data = "home/.local/share/crush";
    fs::rename(t.path().join(data), t.path().join("elsewhere/crush-data")).unwrap();
    symlink(t.path().join("elsewhere/crush-data"), t.path().join(data)).unwrap();
    assert_eq!(indexed(&t), expected);
    link(data, &t.path().join("elsewhere/nowhere"));
    run(&t, &["index"]);
    let status = run(&t, &["status"]);
    assert!(
        status
            .lines()
            .any(|line| line.starts_with("source failed: ")
                && line.contains(".local/share/crush: ")
                && line.contains("its target cannot be reached")),
        "{status}"
    );
    link(data, &t.path().join("elsewhere/crush-data"));
    assert_eq!(indexed(&t), expected);
    assert!(run(&t, &["doctor"]).contains("checks passed"));
}
