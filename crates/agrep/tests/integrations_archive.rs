#[path = "../examples/support/exit.rs"]
mod exit;
mod support;
use agrep_core::{
    archive,
    data::DataRoot,
    integrations::{self, Target},
};
use rusqlite::Connection;
use std::{
    fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, Command, Output, Stdio},
};
use tempfile::TempDir;
fn make_writable(path: &Path) {
    let mut permissions = fs::metadata(path).unwrap().permissions();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        permissions.set_mode(0o600);
    }
    // Windows permissions expose only the read-only attribute; clearing it is the intent.
    #[cfg(windows)]
    #[allow(clippy::permissions_set_readonly_false)]
    permissions.set_readonly(false);
    fs::set_permissions(path, permissions).unwrap();
}
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
    fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new(support::binary());
        command
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", &self.home)
            .env("USERPROFILE", &self.home)
            .env("AGREP_HOME", &self.home)
            .env("AGREP_DATA_DIR", &self.data)
            .args(args);
        command
    }
    fn cmd(&self, args: &[&str]) -> Output {
        self.command(args).output().unwrap()
    }
    /// Starts `agrep ARGS` and returns once it reports, as an `AGREP_QUERY_TIMING` event,
    /// that it waits for `what`, which the caller holds.
    fn waiting(&self, args: &[&str], what: &str) -> Waiting {
        let mut child = self
            .command(args)
            .env("AGREP_QUERY_TIMING", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let (sender, lines) = std::sync::mpsc::channel();
        let stderr = BufReader::new(child.stderr.take().unwrap());
        std::thread::spawn(move || {
            for line in stderr.lines() {
                if sender.send(line.unwrap()).is_err() {
                    break;
                }
            }
        });
        let mut seen = String::new();
        loop {
            // A watchdog, not synchronization: a command that waited without saying so
            // would otherwise hold the test, and the lock, forever.
            match lines.recv_timeout(std::time::Duration::from_secs(120)) {
                Ok(line) => {
                    if serde_json::from_str::<serde_json::Value>(&line)
                        .is_ok_and(|e| e["wait"] == what)
                    {
                        return Waiting { child, lines };
                    }
                    seen.push_str(&line);
                    seen.push('\n');
                }
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    child.wait().unwrap();
                    panic!("{args:?} ended without waiting for {what}: {seen}")
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    child.kill().unwrap();
                    child.wait().unwrap();
                    panic!("{args:?} never reported waiting for {what}: {seen}")
                }
            }
        }
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
/// A command that reported it waits for something the test holds.
struct Waiting {
    child: Child,
    lines: std::sync::mpsc::Receiver<String>,
}
impl Waiting {
    /// The command's output, once the test has released what it waited for.
    fn finish(self) -> Output {
        let mut output = self.child.wait_with_output().unwrap();
        output.stderr = self
            .lines
            .iter()
            .map(|l| l + "\n")
            .collect::<String>()
            .into_bytes();
        output
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
// setup then remove gives back each client's settings byte for byte whatever their layout,
// and setup leaves the user's own hooks for the same event exactly as they were written.
#[test]
fn setup_then_remove_restores_hook_settings_byte_for_byte() {
    let cases = [
        "{\n  \"model\": \"opus\",\n  \"hooks\": {\n    \"EVENT\": [\n      {\n        \"matcher\": \"manual\",\n        \"hooks\": [{\"type\": \"command\", \"command\": \"echo user\"}]\n      }\n    ]\n  }\n}\n",
        r#"{"model":"opus","hooks":{"EVENT":[{"matcher":"manual","hooks":[{"type":"command","command":"echo user"}]}]}}"#,
        "{\"model\": \"opus\", \"hooks\": {\"Stop\": [{\"hooks\": [{\"type\": \"command\", \"command\": \"echo user\"}]}]}}\n",
        "{\r\n\t\"hooks\": {\r\n\t\t\"EVENT\": [{\"hooks\": [{\"type\": \"command\", \"command\": \"echo user\"}]}],\r\n\t\t\"Stop\": []\r\n\t},\r\n\t\"theme\": \"dark\"\r\n}\r\n",
        "{\n  \"hooks\": {},\n  \"model\": \"caf\\u00e9\"\n}\n",
        "{\n    \"hooks\": {\n        \"EVENT\": []\n    }\n}",
        "{}\n",
    ];
    for case in cases {
        let f = Fixture::new();
        let mut files = vec![];
        for (dir, event) in [(".claude", "PreCompact"), (".codex", "SessionStart")] {
            fs::create_dir_all(f.home.join(dir)).unwrap();
            let name = if dir == ".claude" {
                "settings.json"
            } else {
                "hooks.json"
            };
            let path = f.home.join(dir).join(name);
            let original = case.replace("EVENT", event);
            fs::write(&path, &original).unwrap();
            files.push((path, original));
        }
        let args = ["setup", "--yes", "--no-semantic", "--no-archive"];
        f.ok(&args);
        let installed: Vec<_> = files.iter().map(|(p, _)| fs::read(p).unwrap()).collect();
        for ((path, original), now) in files.iter().zip(&installed) {
            let now = String::from_utf8(now.clone()).unwrap();
            assert!(now.contains("agrep __hook"), "{now}");
            serde_json::from_str::<serde_json::Value>(&now).unwrap();
            if let Some(at) = original.find("echo user") {
                let start = original[..at].rfind('[').unwrap();
                let end = at + original[at..].find(']').unwrap() + 1;
                assert!(
                    now.contains(&original[start..end]),
                    "{}: {now}",
                    path.display()
                );
            }
        }
        f.ok(&args);
        for ((path, _), first) in files.iter().zip(&installed) {
            assert_eq!(&fs::read(path).unwrap(), first, "{}", path.display());
        }
        f.ok(&["remove"]);
        for (path, original) in &files {
            assert_eq!(
                fs::read_to_string(path).unwrap(),
                *original,
                "{}",
                path.display()
            );
        }
    }
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
    make_writable(&p);
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
fn marker_without_enrollment_removed_and_unreadable_file_left_alone() {
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
    // agrep never recorded writing these, so remove reports them and still succeeds.
    for (bytes, reason) in [
        (
            &b"user\n<!-- agrep:recall v1 -->\nno end\n"[..],
            "unclosed agrep instruction marker",
        ),
        (b"user \xff\xfe\n", "instruction file is not UTF-8"),
    ] {
        fs::write(&p, bytes).unwrap();
        for _ in 0..2 {
            let o = f.ok(&["remove"]);
            let stdout = String::from_utf8(o.stdout).unwrap();
            let note = format!(
                "  left alone (agrep has no record of writing it): {}: {reason}",
                p.display()
            );
            assert_eq!(stdout.matches(&note).count(), 1, "{stdout}");
            assert!(stdout.contains("agrep integrations removed"), "{stdout}");
            assert!(
                o.stderr.is_empty(),
                "{}",
                String::from_utf8_lossy(&o.stderr)
            );
            assert_eq!(fs::read(&p).unwrap(), bytes);
        }
    }
}
#[test]
fn setup_errors_name_the_file_to_fix() {
    let f = Fixture::new();
    let p = f.home.join(".codex/AGENTS.md");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    for (bytes, reason) in [
        (&b"user \xff\n"[..], "instruction file is not UTF-8"),
        (
            b"<!-- agrep:recall v1 -->\nno end\n",
            "unclosed agrep instruction marker",
        ),
    ] {
        fs::write(&p, bytes).unwrap();
        let o = f.cmd(&[
            "setup",
            "--yes",
            "--no-hook",
            "--no-semantic",
            "--no-archive",
        ]);
        assert_eq!(o.status.code(), Some(1));
        let stderr = String::from_utf8(o.stderr).unwrap();
        assert!(
            stderr.contains(&format!("  ! {}: {reason}", p.display())),
            "{stderr}"
        );
        assert_eq!(fs::read(&p).unwrap(), bytes);
    }
}
#[test]
fn opencode_instructions_go_where_opencode_reads_them() {
    let f = Fixture::new();
    let data = f.home.join(".local/share/opencode");
    fs::create_dir_all(&data).unwrap();
    let config = f.home.join(".config/opencode/AGENTS.md");
    let args = [
        "setup",
        "--yes",
        "--no-hook",
        "--no-semantic",
        "--no-archive",
    ];
    let o = f.ok(&args);
    assert!(String::from_utf8(o.stdout)
        .unwrap()
        .contains(&format!("  {}\n", config.display())));
    assert!(fs::read_to_string(&config)
        .unwrap()
        .contains("<!-- agrep:recall v1 -->"));
    assert!(!data.join("AGENTS.md").exists());
    f.ok(&["remove"]);
    assert!(!config.exists());
    // XDG_CONFIG_HOME moves opencode's whole config directory.
    let xdg = f.home.join("xdg");
    let moved = xdg.join("opencode/AGENTS.md");
    let run = |args: &[&str]| {
        let o = f
            .command(args)
            .env("XDG_CONFIG_HOME", &xdg)
            .output()
            .unwrap();
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    };
    run(&args);
    assert!(fs::read_to_string(&moved).unwrap().contains("agrep:recall"));
    assert!(!config.exists() && !data.join("AGENTS.md").exists());
    run(&["remove"]);
    assert!(!moved.exists());
}
#[test]
fn opencode_blocks_written_to_its_data_directory_are_cleaned_up() {
    let f = Fixture::new();
    let data = f.home.join(".local/share/opencode");
    fs::create_dir_all(&data).unwrap();
    let misplaced = data.join("AGENTS.md");
    let block = "user\n<!-- agrep:recall v1 -->\nold\n<!-- /agrep:recall -->\n";
    // Without enrollment, remove still takes the block out of the data directory.
    fs::write(&misplaced, block).unwrap();
    f.ok(&["remove"]);
    assert_eq!(fs::read_to_string(&misplaced).unwrap(), "user\n");
    // An enrolled block there is retired by the next setup.
    fs::remove_file(&misplaced).unwrap();
    let root = f.root();
    let mut c = root.config().unwrap();
    c.enrollment.push(integrations::Installed {
        path: misplaced.clone(),
        kind: integrations::Kind::Block {
            separator: String::new(),
            skill: false,
        },
        created: true,
        directories: vec![],
    });
    root.save_config(&c).unwrap();
    fs::write(
        &misplaced,
        "<!-- agrep:recall v1 -->\nold\n<!-- /agrep:recall -->\n",
    )
    .unwrap();
    f.ok(&[
        "setup",
        "--yes",
        "--no-hook",
        "--no-semantic",
        "--no-archive",
    ]);
    assert!(!misplaced.exists());
    let enrolled: Vec<PathBuf> = f
        .root()
        .config()
        .unwrap()
        .enrollment
        .into_iter()
        .map(|i| i.path)
        .collect();
    assert_eq!(enrolled, [f.home.join(".config/opencode/AGENTS.md")]);
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
    let refusal = f.cmd(&["set", "post_index", " "]);
    assert!(String::from_utf8_lossy(&refusal.stderr)
        .contains("post_index requires a value (any command, or off to disable it)"));
    for off in ["OFF", "Off"] {
        f.ok(&["set", "post_index", "echo done"]);
        let o = f.ok(&["set", "post_index", off]);
        assert_eq!(
            String::from_utf8(o.stdout).unwrap(),
            "post_index = off  (nothing runs after indexing)\n"
        );
        assert!(f.root().config().unwrap().post_index.is_none());
    }
    // A value stored by hand is read the same way.
    let root = f.root();
    let mut c = root.config().unwrap();
    c.post_index = Some("OFF".into());
    root.save_config(&c).unwrap();
    assert!(String::from_utf8(f.ok(&["set"]).stdout)
        .unwrap()
        .contains("  post_index = off   (any command, or off)\n"));
}
#[test]
fn every_set_note_says_what_changes() {
    let f = Fixture::new();
    let log = f.data.join("log").join("post_index.log");
    for (args, line) in [
        (["set", "tools", "off"], "tools = off  (searches leave out tool calls and their output unless you pass --who tool)".to_owned()),
        (["set", "tools", "on"], "tools = on  (searches include tool calls and their output)".into()),
        (["set", "embeddings", "off"], "embeddings = off  (agent searches stay keyword-only and new rows are not embedded; -s and index --semantic still work)".into()),
        (["set", "embeddings", "auto"], "embeddings = auto  (agent searches add meaning results once the model is installed; new rows are embedded in the background)".into()),
        (["set", "post_index", "true"], format!("post_index = true  (runs after each index that adds or changes rows, for at most 10 minutes; its output goes to {})", log.display())),
        (["set", "post_index", "off"], "post_index = off  (nothing runs after indexing)".into()),
    ] {
        assert_eq!(String::from_utf8(f.ok(&args).stdout).unwrap(), line + "\n");
    }
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
    // The forced restore meets the open write transaction and waits for its end.
    let restore = f.waiting(&["restore", "opencode.db", "--force"], "sqlite");
    db.execute_batch("ROLLBACK").unwrap();
    let out = restore.finish();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
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
/// `archive::status` reads the blob directory while a capture may prune it; what is gone
/// before it is examined counts as no longer stored, as status's own walk does.
#[test]
fn archive_status_counts_blobs_removed_under_it_as_gone() {
    let f = Fixture::new();
    f.source("rollout-pruned.jsonl", b"pruned");
    f.ok(&["archive"]);
    let before = archive::status(&f.root()).unwrap();
    assert!(before["stored_bytes"].as_u64().unwrap() > 0, "{before}");
    fs::remove_dir_all(f.data.join("archive/blobs")).unwrap();
    let after = archive::status(&f.root()).unwrap();
    assert_eq!(after["stored_bytes"], 0);
    assert_eq!(after["versions"], before["versions"]);
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
    let archived: Vec<String> = db
        .prepare("SELECT home_relative FROM versions WHERE home_relative IS NOT NULL")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    for p in files {
        assert_eq!(
            archived
                .iter()
                .filter(|actual| Path::new(actual) == Path::new(p))
                .count(),
            1,
            "{p}"
        );
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
fn model_provisioning_preserves_files_when_the_lock_cannot_be_opened() {
    let f = Fixture::new();
    let root = f.root();
    let dir = root.path().join("model-test");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("config.json"), b"corrupt").unwrap();
    fs::create_dir(dir.join("download.lock")).unwrap();
    assert!(agrep_core::semantic_model::provision_at(&dir).is_err());
    assert_eq!(fs::read(dir.join("config.json")).unwrap(), b"corrupt");
    assert!(!dir.join("tokenizer.json").exists());
    assert!(!dir.join("weights-f32.safetensors").exists());
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
    make_writable(&source);
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
        make_writable(&t.path);
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
    lock.lock().unwrap();
    let contender = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(dir.join("archive.lock"))
        .unwrap();
    // The capture meets the held archive lock and waits for it.
    let capture = f.waiting(&["archive"], "archive.lock");
    lock.unlock().unwrap();
    let out = capture.finish();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!String::from_utf8_lossy(&out.stdout).contains("busy"));
    contender.try_lock().unwrap();
}

fn text(o: &Output) -> (String, String) {
    (
        String::from_utf8_lossy(&o.stdout).into_owned(),
        String::from_utf8_lossy(&o.stderr).into_owned(),
    )
}
/// The stored chunks of the latest version of the archived file whose path contains `name`.
fn blobs_of(f: &Fixture, name: &str) -> Vec<PathBuf> {
    let db = Connection::open(f.data.join("archive/catalog.db")).unwrap();
    let mut stmt = db
        .prepare("SELECT sha FROM chunks WHERE version=(SELECT max(id) FROM versions WHERE path LIKE '%' || ? || '%') ORDER BY position")
        .unwrap();
    let shas = stmt
        .query_map([name], |r| r.get::<_, String>(0))
        .unwrap()
        .map(Result::unwrap);
    shas.map(|sha| f.data.join(format!("archive/blobs/{sha}.gz")))
        .collect()
}
fn corrupt_in_place(path: &Path) {
    let mut bytes = fs::read(path).unwrap();
    let middle = bytes.len() / 2;
    bytes[middle] ^= 0xff;
    fs::write(path, bytes).unwrap();
}
fn transcript_text(tag: &str) -> Vec<u8> {
    (0..400)
        .map(|i| format!("{{\"line\":{i},\"tag\":\"{tag}\"}}\n"))
        .collect::<String>()
        .into_bytes()
}

/// A capture verifies the chunks it relies on: a missing or corrupt chunk of a file whose
/// live copy is unchanged is re-stored from it, so the file stays restorable.
#[test]
fn capture_re_stores_damaged_chunks_of_unchanged_files() {
    let f = Fixture::new();
    let content = transcript_text("kept");
    let p = f.source("rollout-kept.jsonl", &content);
    f.ok(&["archive"]);
    let [blob] = &blobs_of(&f, "kept")[..] else {
        panic!("one chunk expected")
    };
    fs::remove_file(blob).unwrap();
    let (out, err) = text(&f.ok(&["archive"]));
    assert!(
        out.contains("1 unchanged") && out.contains(", 1 damaged chunk(s) re-stored in "),
        "{out}"
    );
    assert_eq!(
        err,
        "archive: re-stored 1 damaged chunk(s) of rollout-kept.jsonl\n"
    );
    assert!(blob.is_file());
    corrupt_in_place(blob);
    let (out, _) = text(&f.ok(&["archive"]));
    assert!(out.contains(", 1 damaged chunk(s) re-stored in "), "{out}");
    let (out, err) = text(&f.ok(&["archive"]));
    assert!(!out.contains("re-stored") && err.is_empty(), "{out}{err}");
    fs::remove_file(&p).unwrap();
    f.ok(&["restore", "kept"]);
    assert_eq!(fs::read(&p).unwrap(), content);
}

/// Chunks no live file holds any more are reported as archive damage, by restore, by
/// status and by the capture that found them, instead of as the restored file's absence.
#[test]
fn lost_chunks_are_reported_as_archive_damage() {
    let f = Fixture::new();
    let lost = f.source("rollout-lost.jsonl", &transcript_text("lost"));
    let corrupt = f.source("rollout-corrupt.jsonl", &transcript_text("corrupt"));
    f.ok(&["archive"]);
    fs::remove_file(&blobs_of(&f, "lost")[0]).unwrap();
    corrupt_in_place(&blobs_of(&f, "corrupt")[0]);
    fs::remove_file(&lost).unwrap();
    fs::remove_file(&corrupt).unwrap();
    let blobs = f.data.join("archive/blobs");
    let restore = f.cmd(&["restore", "lost"]);
    let (out, _) = text(&restore);
    assert_eq!(restore.status.code(), Some(1), "{out}");
    assert!(
        out.starts_with(&format!(
            "cannot restore {}: its archived copy is damaged: chunk ",
            lost.display()
        )) && out.contains(&format!(
            " is missing from {}; `agrep archive` re-stores",
            blobs.display()
        )),
        "{out}"
    );
    assert!(!out.contains("No such file"), "{out}");
    let (out, _) = text(&f.cmd(&["restore", "corrupt"]));
    assert!(
        out.contains(&format!(" in {} is corrupt (", blobs.display())),
        "{out}"
    );
    let status = f.cmd(&["archive", "--status"]);
    let (out, _) = text(&status);
    assert_eq!(status.status.code(), Some(1), "{out}");
    assert!(
        out.contains("archive damaged: 1 version(s) of 1 file(s) cannot be restored"),
        "{out}"
    );
    let capture = f.cmd(&["archive"]);
    let (out, _) = text(&capture);
    assert_eq!(capture.status.code(), Some(1), "{out}");
    assert!(
        out.contains("archive damaged: 2 version(s) of 2 file(s) cannot be restored")
            && out.contains(&format!("  {}\n", lost.display()))
            && out.contains(&format!("  {}\n", corrupt.display())),
        "{out}"
    );
    let (out, _) = text(&f.cmd(&["archive", "--status"]));
    assert!(
        out.contains("archive damaged: 2 version(s) of 2 file(s) cannot be restored"),
        "{out}"
    );
    let (out, _) = text(&f.cmd(&["doctor"]));
    assert!(
        out.contains("\narchive: failed - 2 archived version(s) of 2 file(s) cannot be restored: their stored chunks are missing or corrupt - `agrep archive` re-stores those that live files still hold\n"),
        "{out}"
    );
}

fn corrupt_catalog(f: &Fixture) {
    let catalog = f.data.join("archive/catalog.db");
    for suffix in ["-wal", "-shm"] {
        let _ = fs::remove_file(format!("{}{suffix}", catalog.display()));
    }
    fs::write(&catalog, "not a database ".repeat(40)).unwrap();
}

/// An unreadable catalog is named once with its remedy by every archive command, and
/// `--purge --yes` removes it so that captures start over.
#[test]
fn a_corrupt_catalog_is_named_and_purge_recovers() {
    let f = Fixture::new();
    let p = f.source("rollout-catalog.jsonl", b"catalog");
    f.ok(&["archive"]);
    corrupt_catalog(&f);
    let catalog = f.data.join("archive/catalog.db");
    let damaged = format!(
        "archive catalog is damaged: {} (file is not a database); `agrep archive --purge --yes` deletes the archive and the next capture starts a new one\n",
        catalog.display()
    );
    let status = f.cmd(&["archive", "--status"]);
    assert_eq!(
        (status.status.code(), text(&status)),
        (Some(2), (damaged.clone(), String::new()))
    );
    for args in [
        &["archive"][..],
        &["archive", "--prune"],
        &["restore", "catalog"],
    ] {
        let o = f.cmd(args);
        assert_eq!(
            (o.status.code(), text(&o)),
            (Some(2), (String::new(), format!("agrep: {damaged}"))),
            "{args:?}"
        );
    }
    let off = f.ok(&["archive", "--off"]);
    assert_eq!(
        text(&off).0,
        format!("archive auto-capture disabled\n{damaged}")
    );
    let (out, _) = text(&f.ok(&["archive", "--purge", "--yes"]));
    assert!(out.starts_with("archive purged: "), "{out}");
    let purged = archive::status(&f.root()).unwrap();
    assert_eq!(
        (&purged["versions"], &purged["stored_bytes"]),
        (&0.into(), &0.into())
    );
    let (out, _) = text(&f.ok(&["archive"]));
    assert!(out.contains("1 full captures"), "{out}");
    fs::remove_file(&p).unwrap();
    f.ok(&["restore", "catalog"]);
    assert_eq!(fs::read(&p).unwrap(), b"catalog");
}

/// A scheduled capture that failed is not retried by the next command's worker: the
/// worker backs off instead of failing, and logging it, on every command.
#[test]
fn a_failed_scheduled_capture_backs_off() {
    let f = Fixture::new();
    f.source("rollout-backoff.jsonl", b"backoff");
    f.ok(&["archive", "--on"]);
    f.ok(&["archive"]);
    corrupt_catalog(&f);
    assert!(archive::due(&f.root()).unwrap());
    let failed = f.cmd(&["__work"]);
    let (_, err) = text(&failed);
    assert!(!failed.status.success());
    assert!(err.contains("archive catalog is damaged"), "{err}");
    assert!(!archive::due(&f.root()).unwrap());
    let (out, err) = text(&f.ok(&["__work"]));
    assert_eq!((out.as_str(), err.as_str()), ("", ""));
    let (out, _) = text(&f.cmd(&["status"]));
    assert!(
        out.contains(
            "\narchive: enabled; the last 1 capture pass(es) failed - `agrep doctor` explains\n"
        ),
        "{out}"
    );
    let (out, _) = text(&f.cmd(&["doctor"]));
    let catalog = f.data.join("archive/catalog.db");
    assert!(
        out.starts_with("doctor: action needed\n")
            && out.contains(&format!(
                "\narchive: failed - archive catalog is damaged: {} (file is not a database) - `agrep archive --purge --yes` deletes the archive and the next capture starts a new one\n",
                catalog.display()
            )),
        "{out}"
    );
    f.ok(&["archive", "--purge", "--yes"]);
    assert!(archive::due(&f.root()).unwrap());
    f.ok(&["__work"]);
    assert_eq!(archive::status(&f.root()).unwrap()["versions"], 1);
}

/// `index` leaves a due capture to a background worker, as 0.4.9 left it to its daemon,
/// instead of capturing every store before it exits.
#[test]
fn index_leaves_a_due_capture_to_the_worker() {
    let f = Fixture::new();
    f.source("rollout-indexed.jsonl", b"indexed");
    f.ok(&["archive", "--on"]);
    let index = f
        .command(&["index"])
        .env("AGREP_QUERY_TIMING", "1")
        .output()
        .unwrap();
    let (_, err) = text(&index);
    assert!(index.status.success(), "{err}");
    assert!(err.contains(r#""archive_due":true"#), "{err}");
    let workers: Vec<u32> = err
        .lines()
        .filter_map(|l| {
            serde_json::from_str::<serde_json::Value>(l).ok()?["worker_started"]["pid"].as_u64()
        })
        .map(|pid| pid as u32)
        .collect();
    assert!(!workers.is_empty(), "{err}");
    for pid in workers {
        assert!(exit::wait(pid, std::time::Duration::from_secs(120)).unwrap());
    }
    assert_eq!(archive::status(&f.root()).unwrap()["versions"], 1);
}

#[cfg(windows)]
#[test]
fn setup_reuses_enrollment_across_windows_path_spellings() {
    let f = Fixture::new();
    for name in [".claude", ".codex", ".pi/agent", ".omp/agent"] {
        fs::create_dir_all(f.home.join(name)).unwrap();
    }
    f.ok(&["setup", "--yes", "--no-semantic", "--no-archive"]);
    let root = DataRoot(f.data.clone());
    let mut config = root.config().unwrap();
    let original = config.enrollment.clone();
    assert!(!original.is_empty());
    for item in &mut config.enrollment {
        item.path = PathBuf::from(
            item.path
                .to_string_lossy()
                .replace('\\', "/")
                .to_uppercase(),
        );
    }
    root.save_config(&config).unwrap();
    f.ok(&["setup", "--yes", "--no-semantic", "--no-archive"]);
    let current = root.config().unwrap();
    assert_eq!(current.enrollment.len(), original.len());
    for item in &original {
        let updated = current
            .enrollment
            .iter()
            .find(|other| agrep_core::ingest::registry::path_eq(&other.path, &item.path))
            .unwrap();
        assert_eq!(updated.created, item.created);
    }
    f.ok(&["remove"]);
    assert!(root.config().unwrap().enrollment.is_empty());
}
