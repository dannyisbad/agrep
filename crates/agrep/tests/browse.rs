#[cfg(unix)]
#[path = "../examples/support/exit.rs"]
mod exit;
#[cfg(unix)]
#[path = "support/job.rs"]
mod job;
mod support;
use agrep_core::{data::DataRoot, store};
use rusqlite::params;
use serde_json::{json, Value};
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
use tempfile::TempDir;
fn command(t: &TempDir, args: &[&str]) -> Command {
    let mut command = Command::new(support::binary());
    command
        .env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("TZ", "UTC")
        .args(args);
    command
}
fn run(t: &TempDir, args: &[&str]) -> Output {
    command(t, args).output().unwrap()
}
fn ok(out: Output) -> Output {
    assert!(
        out.status.success(),
        "stderr={} stdout={}",
        String::from_utf8_lossy(&out.stderr),
        String::from_utf8_lossy(&out.stdout)
    );
    out
}
fn records(out: &Output) -> Vec<Value> {
    String::from_utf8(out.stdout.clone())
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
fn write_chat(path: &Path, id: &str, cwd: &Path, turns: &[(&str, &str, &str)]) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut lines = vec![
        json!({"type":"session","id":id,"version":3,"cwd":cwd,"timestamp":"2026-01-02T00:00:00Z"})
            .to_string(),
    ];
    let mut parent = Value::Null;
    for (i, (ts, text, reply)) in turns.iter().enumerate() {
        let uid = format!("u{i}");
        lines.push(json!({"type":"message","id":uid,"parentId":parent,"timestamp":ts,"message":{"role":"user","content":[{"type":"text","text":text}],"timestamp":ts}}).to_string());
        parent = json!(uid);
        if !reply.is_empty() {
            let aid = format!("a{i}");
            lines.push(json!({"type":"message","id":aid,"parentId":parent,"timestamp":ts,"message":{"role":"assistant","model":"test","content":[{"type":"text","text":reply}],"timestamp":ts}}).to_string());
            parent = json!(aid);
        }
    }
    fs::write(path, lines.join("\n") + "\n").unwrap();
}
fn fixture() -> TempDir {
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let cwd = t.path().join("project alpha");
    fs::create_dir_all(&cwd).unwrap();
    write_chat(
        &home.join(".pi/agent/sessions/alpha/2026-01-02_root-alpha-0001.jsonl"),
        "root-alpha-0001",
        &cwd,
        &[
            ("2026-01-02T00:00:00Z", "design the cache", "Finished."),
            (
                "2026-01-02T00:30:00Z",
                "change the retry ceiling",
                "Would you like me to deploy?",
            ),
        ],
    );
    write_chat(
        &home.join(".pi/agent/sessions/alpha/2026-01-03_root-alpha-0002.jsonl"),
        "root-alpha-0002",
        &cwd,
        &[
            (
                "2026-01-03T00:00:00Z",
                "older title with newer activity",
                "The exact hidden quartz token is here.",
            ),
            ("2026-01-03T00:05:00Z", "finish", "Completed."),
        ],
    );
    write_chat(
        &home.join(".pi/agent/sessions/alpha/2026-01-02_root-alpha-0001/helper.jsonl"),
        "side-helper-0001",
        &cwd,
        &[
            ("2026-01-02T00:10:00Z", "delegated cache task", "Done."),
            ("2026-01-02T00:40:00Z", "more cache work", "Done."),
        ],
    );
    ok(run(&t, &["index"]));
    t
}
#[test]
fn chats_order_side_filter_content_and_envelope() {
    let t = fixture();
    let rows = records(&ok(run(&t, &["chats", "--json"])));
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[0]["kind"], "agrep-meta");
    assert_eq!(rows[1]["session"], "root-alpha-0002");
    assert!(!rows.iter().skip(1).any(|v| v["side"] == true));
    let rows = records(&ok(run(&t, &["chats", "--json", "quartz"])));
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[1]["match_source"], "content");
    assert_eq!(rows[1]["match_turn"], 0);
    assert!(rows[1]["match_handle"]
        .as_str()
        .unwrap()
        .starts_with("@root-alpha-0002:0."));
    assert!(rows[1]["score"].is_number());
    let rows = records(&ok(run(&t, &["chats", "--json", "side-helper"])));
    assert_eq!(rows[1]["side"], true);
    let rows = records(&ok(run(&t, &["chats", "--json", "--side"])));
    assert_eq!(rows.len(), 4);
    let text = String::from_utf8(ok(run(&t, &["chats"])).stdout).unwrap();
    assert_eq!(text.matches("agrep around @").count(), 2);
    let none = run(&t, &["chats", "--json", "--project", "absent"]);
    assert_eq!(none.status.code(), Some(1));
    assert_eq!(records(&none)[0]["hits"], json!([]));
    assert_eq!(
        run(&t, &["chats", "--no-auto", "absent-query"])
            .status
            .code(),
        Some(2)
    );
}
#[test]
fn summary_time_union_and_pending_evidence() {
    let t = fixture();
    let rows = records(&ok(run(
        &t,
        &[
            "summary",
            "time",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-04",
            "--json",
        ],
    )));
    assert_eq!(rows[0]["metric"], "estimated active time");
    assert_eq!(rows[0]["family_dedup_ms"], 600000);
    assert_eq!(rows[0]["total_estimated_active_ms"], 2100000);
    assert_eq!(rows[0]["side_chats"], 1);
    assert!(rows.iter().skip(1).all(|r| r["kind"] == "time"));
    let rows = records(&ok(run(
        &t,
        &[
            "summary",
            "pending",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-04",
            "--json",
        ],
    )));
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[1]["status"], "waiting_on_user");
    assert_eq!(rows[1]["confidence"], "high");
    assert!(rows[1]["handle"]
        .as_str()
        .unwrap()
        .starts_with("@root-alpha-0001:1."));
    assert!(rows[1]["evidence"].as_str().unwrap().contains("deploy"));
    let projects = records(&ok(run(
        &t,
        &[
            "summary",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-04",
            "--json",
            "-n",
            "1",
        ],
    )));
    assert_eq!(projects[1]["worked_on"].as_array().unwrap().len(), 1);
    assert_eq!(projects[1]["chats"], 2);
}
#[test]
fn summary_validates_windows_and_idle_caps() {
    let t = fixture();
    for args in [
        vec!["summary", "time", "--idle-cap", "0m"],
        vec!["summary", "--since", "2026-02-01", "--until", "2026-01-01"],
    ] {
        assert_eq!(run(&t, &args).status.code(), Some(2));
    }
}
/// A pi chat on `day` of `turns` turns, each a prompt, a shell call, its output and a reply
/// asking to go on.
fn shell_chat(home: &Path, id: &str, day: &str, turns: usize) {
    let path = home.join(format!(".pi/agent/sessions/work/{day}_{id}.jsonl"));
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let at =
        |turn: usize, step: usize| format!("{day}T{:02}:{:02}:{step:02}Z", turn / 60, turn % 60);
    let mut lines =
        vec![json!({"type":"session","id":id,"version":3,"cwd":"/work","timestamp":at(0, 0)})];
    let mut parent = Value::Null;
    for turn in 0..turns {
        let call = format!("call-{turn}");
        for (step, message) in [
            json!({"role":"user","content":[{"type":"text","text":format!("check part {turn}")}]}),
            json!({"role":"assistant","model":"test","content":[{"type":"toolCall","id":call,"name":"bash","arguments":{"command":format!("make check-{turn}")}}]}),
            json!({"role":"toolResult","toolCallId":call,"toolName":"bash","content":[{"type":"text","text":format!("part {turn} passed")}]}),
            json!({"role":"assistant","model":"test","content":[{"type":"text","text":format!("Part {turn} passed. Shall I continue?")}]}),
        ]
        .into_iter()
        .enumerate()
        {
            let id = format!("m{turn}-{step}");
            lines.push(json!({"type":"message","id":id,"parentId":parent,"timestamp":at(turn, step),"message":message}));
            parent = json!(id);
        }
    }
    let body: Vec<String> = lines.iter().map(Value::to_string).collect();
    fs::write(path, body.join("\n") + "\n").unwrap();
}
// 0.4.9 read only the chats a summary window admits (py/summary.py:2116-2130): history
// outside the window, however long, adds no work.
#[test]
fn summary_work_is_bounded_by_its_window() {
    let mut work = vec![];
    for before in [2, 200] {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        shell_chat(&home, "inside-0001", "2099-01-10", 3);
        shell_chat(&home, "before-0001", "2098-06-01", before);
        ok(run(&t, &["index"]));
        let mut ops = vec![];
        for mode in [None, Some("pending"), Some("time")] {
            let mut args = vec!["summary"];
            args.extend(mode);
            args.extend(["--since", "2099-01-01", "--until", "2099-02-01"]);
            args.extend(["--json", "--no-auto"]);
            let out = ok(command(&t, &args)
                .env("AGREP_QUERY_TIMING", "1")
                .output()
                .unwrap());
            assert_eq!(records(&out)[0]["chats"], 1);
            let stats = String::from_utf8_lossy(&out.stderr)
                .lines()
                .filter_map(|line| serde_json::from_str::<Value>(line).ok())
                .find_map(|event| event.get("summary").cloned())
                .expect("summary work event");
            ops.push(stats["sqlite_ops"].as_u64().unwrap());
        }
        work.push(ops);
    }
    assert_eq!(
        work[0], work[1],
        "summary work grew with history outside its window"
    );
}
#[test]
fn diagnostics_are_read_only_and_prove_index_and_absence() {
    let t = fixture();
    let before = fs::metadata(t.path().join("data/index.db"))
        .unwrap()
        .modified()
        .unwrap();
    let why = records(&ok(run(&t, &["why", "root-alpha-0001", "--json"])));
    assert_eq!(why[0]["version"], 1);
    assert_eq!(why[0]["verdict"], "indexed");
    assert!(!why[0]["evidence"]["sources"].as_array().unwrap().is_empty());
    let ambiguous = run(&t, &["why", "root-alpha", "--json"]);
    assert_eq!(ambiguous.status.code(), Some(2));
    assert_eq!(records(&ambiguous)[0]["verdict"], "ambiguous");
    let absent = run(&t, &["why", "missing-session", "--json"]);
    assert_eq!(absent.status.code(), Some(1));
    assert_eq!(records(&absent)[0]["verdict"], "not-indexed");
    let status = records(&ok(run(&t, &["status", "--json"])));
    assert_eq!(status[0]["format"], "valid");
    assert_eq!(status[0]["index"]["counts"]["sessions"], 3);
    assert_eq!(status[0]["semantic"]["indexed_rows"], 0);
    let doctor = records(&ok(run(&t, &["doctor", "--deep", "--json"])));
    assert!(doctor[0]["checks"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v["check"] == "sqlite_integrity" && v["state"] == "passed"));
    assert_eq!(
        fs::metadata(t.path().join("data/index.db"))
            .unwrap()
            .modified()
            .unwrap(),
        before
    );
}
#[test]
fn audit_recounts_and_strict_gaps_fail() {
    let t = fixture();
    let audit = records(&ok(run(&t, &["audit", "--full", "--strict", "--json"])));
    assert_eq!(audit[0]["gaps"], json!(0));
    assert_eq!(audit[0]["status"], "clean");
    let root = DataRoot(t.path().join("data"));
    let c = store::writer(&root).unwrap();
    c.execute(
        "DELETE FROM prose WHERE session='root-alpha-0002' AND who='agent'",
        [],
    )
    .unwrap();
    drop(c);
    // py/audit.py:2515-2524: gaps are an evidence warning (exit 1, "warning"), and
    // --strict makes them errors (exit 2, "error").
    let out = run(&t, &["audit", "--full", "--strict", "--json"]);
    assert_eq!(out.status.code(), Some(2));
    let strict = records(&out);
    assert_eq!(
        (&strict[0]["status"], &strict[0]["exit_code"]),
        (&json!("error"), &json!(2))
    );
    assert!(!strict[0]["gap_details"].as_array().unwrap().is_empty());
    let out = run(&t, &["audit", "--full", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    let warning = records(&out);
    assert_eq!(
        (&warning[0]["status"], &warning[0]["exit_code"]),
        (&json!("warning"), &json!(1))
    );
    assert_eq!(
        run(&t, &["audit", "--agent", "unknown"]).status.code(),
        Some(2)
    );
}
#[test]
fn audit_warns_about_a_source_written_since_the_refresh() {
    // 0.4.9 (agrep audit on a pi chat appended after `agrep index`): exit 1 "warning", and
    // 2 "error" under --strict.
    let t = fixture();
    assert_eq!(run(&t, &["audit"]).status.code(), Some(0));
    let source = t
        .path()
        .join("home/.pi/agent/sessions/alpha/2026-01-02_root-alpha-0001.jsonl");
    let mut text = fs::read_to_string(&source).unwrap();
    text.push_str(&format!("{}\n", json!({"type":"message","id":"u9","parentId":"a1","timestamp":"2026-01-02T01:00:00Z","message":{"role":"user","content":[{"type":"text","text":"appended later"}],"timestamp":"2026-01-02T01:00:00Z"}})));
    fs::write(&source, text).unwrap();
    let out = run(&t, &["audit"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stdout).contains("source changed since refresh"));
    let out = run(&t, &["audit", "--strict", "--json"]);
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(records(&out)[0]["status"], "error");
}
#[test]
fn audit_without_source_files_certifies_nothing() {
    // 0.4.9's words and exit for an audit with nothing to count (py/audit.py:2383-2388,
    // 2638-2643), from its output for an empty home and for `--agent codex` beside pi chats.
    let t = TempDir::new().unwrap();
    fs::create_dir_all(t.path().join("home")).unwrap();
    for indexed in [false, true] {
        if indexed {
            ok(run(&t, &["index"]));
        }
        let out = run(&t, &["audit"]);
        assert_eq!(out.status.code(), Some(2));
        assert_eq!(
            String::from_utf8_lossy(&out.stdout),
            "audit unavailable: no supported source files were found; no completeness claim was made.\n"
        );
        assert_eq!(t.path().join("data").exists(), indexed);
    }
    let out = run(&t, &["audit", "--json"]);
    assert_eq!(out.status.code(), Some(2));
    let v = &records(&out)[0];
    assert_eq!(
        (&v["status"], &v["exit_code"]),
        (&json!("error"), &json!(2))
    );
    assert_eq!(
        v["problems"][0]["reason"],
        "no source file census completed; audit cannot certify completeness"
    );
    // `--agent agy` is antigravity's alias, refused by name before any index exists.
    let t = TempDir::new().unwrap();
    fs::create_dir_all(t.path().join("home")).unwrap();
    let out = run(&t, &["audit", "--agent", "agy"]);
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&out.stdout),
        "audit unavailable: no antigravity source files were found; no completeness claim was made.\n"
    );
    assert!(!t.path().join("data").exists());
    let t = fixture();
    let out = run(&t, &["audit", "--agent", "codex"]);
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&out.stdout),
        "audit unavailable: no codex source files were found; no completeness claim was made.\n"
    );
}
#[test]
fn audit_before_the_first_index_reports_every_source_as_a_gap() {
    // 0.4.9 before any `agrep index`: "pi: never tallied: <path>" as a gap, exit 1.
    let t = TempDir::new().unwrap();
    let source = t.path().join("home/.pi/agent/sessions/alpha/a.jsonl");
    let cwd = t.path().join("project");
    write_chat(
        &source,
        "root-alpha-0001",
        &cwd,
        &[("2026-01-02T00:00:00Z", "design the cache", "Finished.")],
    );
    let out = run(&t, &["audit"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stdout).contains("source not indexed"));
    assert_eq!(run(&t, &["audit", "--strict"]).status.code(), Some(2));
    assert!(!t.path().join("data").exists());
}
#[test]
fn doctor_reports_and_fixes_source_parse_errors() {
    let t = fixture();
    let bad = t.path().join("home/.pi/agent/sessions/bad/bad.jsonl");
    fs::create_dir_all(bad.parent().unwrap()).unwrap();
    fs::write(&bad, "not json\n").unwrap();
    ok(run(&t, &["index"]));
    // 0.4.9 exits 0 once the report is complete, whatever it found (py/doctor.py:15,
    // 4194-4208); its audit counts the parse error as an accounting error (exit 2).
    let out = ok(run(&t, &["doctor", "--json"]));
    assert!(records(&out)[0]["status"]["sources"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| {
            // Path equality treats `/` and `\` alike on Windows; the rendered strings differ.
            s["path"].as_str().map(std::path::Path::new) == Some(bad.as_path())
                && s["last_error"].is_string()
        }));
    let out = ok(run(&t, &["doctor"]));
    assert!(String::from_utf8_lossy(&out.stdout).starts_with("doctor: action needed\n"));
    assert_eq!(run(&t, &["audit"]).status.code(), Some(2));
    assert_eq!(run(&t, &["audit", "--strict"]).status.code(), Some(2));
    fs::remove_file(bad).unwrap();
    let out = ok(run(&t, &["doctor", "--fix"]));
    assert!(String::from_utf8_lossy(&out.stdout).starts_with("doctor: checks passed\n"));
}
#[test]
fn empty_diagnostics_do_not_create_data_root() {
    let t = TempDir::new().unwrap();
    let out = ok(run(&t, &["status", "--json"]));
    assert_eq!(records(&out)[0]["format"], "missing");
    assert!(!t.path().join("data").exists());
    let out = run(&t, &["why", "nothing", "--json"]);
    assert_eq!(out.status.code(), Some(2));
    assert!(!t.path().join("data").exists());
}
#[test]
fn native_capabilities_and_opaque_handles() {
    use agrep_core::{history, launch};
    for agent in ["claude", "codex", "opencode", "antigravity", "pi", "omp"] {
        assert!(launch::capability(agent).is_ok())
    }
    for agent in ["gemini", "cursor", "cline", "crush", "kimi", "shell"] {
        assert!(launch::capability(agent).is_err())
    }
    for id in ["opaque /😀:id", "native-id", ""] {
        let target = history::target(id);
        if !id.is_empty() {
            assert_eq!(history::identity(&target).unwrap(), id);
        }
    }
}
#[cfg(unix)]
fn fake_agent(t: &TempDir, name: &str, script: &str) {
    use std::os::unix::fs::PermissionsExt;
    let dir = t.path().join("bin");
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join(name);
    fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}
#[cfg(unix)]
fn launch_cmd(t: &TempDir) -> Command {
    let mut cmd = Command::new(support::binary());
    cmd.env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("PATH", t.path().join("bin"))
        .env("OUTPUT", t.path().join("argv.txt"));
    cmd
}
#[cfg(unix)]
#[test]
fn run_preserves_argv_cwd_status_and_liveness() {
    let t = fixture();
    fake_agent(
        &t,
        "codex",
        "pwd > \"$OUTPUT\"; for a in \"$@\"; do printf '%s\\n' \"$a\" >> \"$OUTPUT\"; done; exit 7",
    );
    let cwd = t.path().join("project alpha");
    let out = launch_cmd(&t)
        .env("AGREP_DATA_READONLY", "1")
        .args([
            "run",
            "codex",
            "--cwd",
            cwd.to_str().unwrap(),
            "--",
            "one two",
            "$(do not execute)",
            "",
            "-x",
        ])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(7));
    assert_eq!(
        fs::read_to_string(t.path().join("argv.txt")).unwrap(),
        format!(
            "{}\none two\n$(do not execute)\n\n-x\n",
            cwd.canonicalize().unwrap().display()
        )
    );
    assert!(!t.path().join("data/runs").exists());
}
// Expected argv is what agrep 0.4.9's `run` passed the agent (cli.py:1786-1813): unknown
// flags pass through in order, `--cwd` counts anywhere before a `--`, and words after a
// `--` pass verbatim, `--` itself kept unless it came first; argparse kept a second `--`
// right after the first, which 0.4.9 then dropped.
#[cfg(unix)]
#[test]
fn run_passes_unknown_flags_through_as_0_4_9() {
    let t = fixture();
    fake_agent(
        &t,
        "codex",
        "pwd > \"$OUTPUT\"; for a in \"$@\"; do printf '[%s]\\n' \"$a\" >> \"$OUTPUT\"; done",
    );
    let here = t.path().join("project alpha").canonicalize().unwrap();
    let elsewhere = t.path().join("home").canonicalize().unwrap();
    let (here, elsewhere) = (here.to_str().unwrap(), elsewhere.to_str().unwrap());
    let attached = format!("--cwd={elsewhere}");
    let verbatim = format!("[--cwd]\n[{elsewhere}]\n[-h]\n");
    for (args, cwd, passed) in [
        (vec!["--bogus-flag"], here, "[--bogus-flag]\n"),
        (
            vec!["-p", "hi there", "-x"],
            here,
            "[-p]\n[hi there]\n[-x]\n",
        ),
        (
            vec!["--model", "o3", "--cwd", elsewhere],
            elsewhere,
            "[--model]\n[o3]\n",
        ),
        (
            vec!["--model", "o3", attached.as_str()],
            elsewhere,
            "[--model]\n[o3]\n",
        ),
        (
            vec!["--", "--cwd", elsewhere, "-h"],
            here,
            verbatim.as_str(),
        ),
        (
            vec!["--bogus", "--", "--x"],
            here,
            "[--bogus]\n[--]\n[--x]\n",
        ),
        (vec!["--", "--"], here, ""),
        (vec!["--", "--", "--"], here, "[--]\n"),
        (vec!["--", "--", "x"], here, "[x]\n"),
    ] {
        let out = launch_cmd(&t)
            .current_dir(here)
            .args(["run", "codex"])
            .args(&args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        assert_eq!(
            fs::read_to_string(t.path().join("argv.txt")).unwrap(),
            format!("{cwd}\n{passed}"),
            "{args:?}"
        );
    }
    let help = launch_cmd(&t)
        .args(["run", "codex", "--model", "-h"])
        .output()
        .unwrap();
    assert!(help.status.success());
    assert!(String::from_utf8_lossy(&help.stdout)
        .contains("\nUsage: agrep run [-h] [--cwd DIR] AGENT [-- AGENT_ARGS...]\n"));
    let missing = launch_cmd(&t)
        .args(["run", "codex", "--model", "o3", "--cwd"])
        .output()
        .unwrap();
    assert_eq!(missing.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&missing.stderr),
        "agrep run: argument --cwd: expected one argument\n"
    );
}
#[cfg(unix)]
#[test]
fn resume_side_uses_parent_id_origin_cwd_and_native_arguments() {
    let t = fixture();
    fake_agent(
        &t,
        "pi",
        "pwd > \"$OUTPUT\"; for a in \"$@\"; do printf '%s\\n' \"$a\" >> \"$OUTPUT\"; done",
    );
    ok(launch_cmd(&t)
        .args(["resume", "side-helper-0001"])
        .output()
        .unwrap());
    let text = fs::read_to_string(t.path().join("argv.txt")).unwrap();
    assert_eq!(
        text,
        format!(
            "{}\n-r\nroot-alpha-0001\n",
            t.path()
                .join("project alpha")
                .canonicalize()
                .unwrap()
                .display()
        )
    );
    ok(run(&t, &["resume", "--list"]));
    assert_eq!(
        run(&t, &["resume", "--list", "root-alpha"]).status.code(),
        Some(2)
    );
    assert_eq!(run(&t, &["resume", "root-alpha"]).status.code(), Some(1));
}
#[test]
fn summary_uses_retained_full_todo_state() {
    let t = fixture();
    let path = t
        .path()
        .join("home/.pi/agent/sessions/alpha/2026-01-03_root-alpha-0002.jsonl");
    let mut text = fs::read_to_string(&path).unwrap();
    text += &format!(
        "{}\n",
        json!({"type":"message","id":"plan-call","parentId":"a1","timestamp":"2026-01-03T00:06:00Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"plan","name":"update_plan","arguments":{"plan":[{"step":"first","status":"completed"},{"step":"verify deployment","status":"pending"}]}}]}})
    );
    text += &format!(
        "{}\n",
        json!({"type":"message","id":"plan-result","parentId":"plan-call","timestamp":"2026-01-03T00:06:01Z","message":{"role":"toolResult","toolCallId":"plan","toolName":"update_plan","content":[{"type":"text","text":"updated"}]}})
    );
    fs::write(path, text).unwrap();
    ok(run(&t, &["index"]));
    let rows = records(&ok(run(
        &t,
        &[
            "summary",
            "pending",
            "--no-auto",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-04",
            "--json",
        ],
    )));
    assert!(rows
        .iter()
        .any(|r| r["status"] == "todo_open" && r["items"] == json!(["verify deployment"])));
}

#[cfg(unix)]
#[test]
fn run_relays_termination_and_drains_descendants() {
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;
    let t = fixture();
    fake_agent(
        &t,
        "codex",
        "trap 'exit 0' TERM; (trap '' TERM; exec /bin/sleep 60) & kid=$!; printf '%s\\n' \"$kid\"; wait",
    );
    let mut process = launch_cmd(&t)
        .args(["run", "codex"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(process.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let descendant = line.trim().parse::<u32>().unwrap();
    assert!(Command::new("/bin/kill")
        .args(["-TERM", &process.id().to_string()])
        .status()
        .unwrap()
        .success());
    assert_eq!(process.wait().unwrap().code(), Some(143));
    assert!(
        exit::wait(descendant, std::time::Duration::from_secs(2)).unwrap(),
        "descendant {descendant} is still live"
    );
    assert!(!t.path().join("data/runs").exists());
    // Normal leader exit must also terminate an owned descendant that ignores TERM.
    fake_agent(
        &t,
        "codex",
        "(trap '' TERM; exec /bin/sleep 60) & printf '%s\\n' \"$!\"; exit 0",
    );
    let normal = launch_cmd(&t).args(["run", "codex"]).output().unwrap();
    assert!(normal.status.success());
    let pid: u32 = String::from_utf8_lossy(&normal.stdout)
        .trim()
        .parse()
        .unwrap();
    assert!(
        exit::wait(pid, std::time::Duration::from_secs(2)).unwrap(),
        "normal-exit descendant {pid} is still live"
    );
    // The wrapper propagates a stopped child to its caller, then resumes it.
    use std::os::unix::process::CommandExt;
    fake_agent(&t, "codex", "printf '%s\\n' \"$$\"; kill -STOP $$; exit 0");
    let mut cmd = launch_cmd(&t);
    cmd.args(["run", "codex"]).stdout(Stdio::piped());
    unsafe {
        cmd.pre_exec(|| {
            if libc::setpgid(0, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut wrapper = cmd.spawn().unwrap();
    let mut child_id = String::new();
    BufReader::new(wrapper.stdout.take().unwrap())
        .read_line(&mut child_id)
        .unwrap();
    let stopped = job::stops(wrapper.id(), std::time::Duration::from_secs(3));
    unsafe {
        libc::kill(wrapper.id() as i32, libc::SIGCONT);
        if !stopped {
            libc::kill(child_id.trim().parse().unwrap(), libc::SIGCONT);
            libc::kill(wrapper.id() as i32, libc::SIGTERM);
        }
    }
    let result = wrapper.wait().unwrap();
    assert!(stopped, "wrapper did not propagate child suspension");
    assert!(result.success());
}

#[cfg(unix)]
#[test]
fn run_exits_with_its_agent_when_nothing_is_left_to_drain() {
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;
    let t = fixture();
    // Each agent prints one line and exits; the second leaves a descendant that ends on TERM.
    for script in [
        "printf 'leaving\\n'; exit 0",
        "/bin/sleep 60 & printf '%s\\n' \"$!\"; exit 0",
    ] {
        fake_agent(&t, "codex", script);
        let mut process = launch_cmd(&t)
            .args(["run", "codex"])
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut line = String::new();
        BufReader::new(process.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        // Well under the 1.5 s grace that a member still running after TERM would get.
        let exited = exit::wait(process.id(), std::time::Duration::from_secs(1)).unwrap();
        if !exited {
            process.kill().unwrap();
        }
        let status = process.wait().unwrap();
        assert!(
            exited,
            "run outlived an agent that left nothing running: {script}"
        );
        assert!(status.success());
        if let Ok(descendant) = line.trim().parse::<u32>() {
            assert!(
                exit::wait(descendant, std::time::Duration::from_secs(2)).unwrap(),
                "descendant {descendant} is still live"
            );
        }
    }
}

#[test]
fn pending_does_not_reopen_a_plan_followed_by_work() {
    let t = fixture();
    let root = DataRoot(t.path().join("data"));
    let c = store::writer(&root).unwrap();
    let source: i64 = c
        .query_row(
            "SELECT source FROM prose WHERE session='root-alpha-0002' LIMIT 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let text = "Next steps:\n- deploy the build Done and verified.";
    c.execute(
        "UPDATE prose SET text=?1 WHERE session='root-alpha-0002' AND who='agent' AND turn=1",
        [text.as_bytes()],
    )
    .unwrap();
    let split = "Next steps:\n- deploy the build".chars().count();
    let shape = json!({"head_chars":text.chars().count(),"breaks":[split],"worked":[1],"joins":1,"lead":0,"end":"","end_cut":false});
    c.execute(
        "UPDATE reply_shapes SET body=?1 WHERE source=?2 AND session='root-alpha-0002' AND turn=1",
        params![shape.to_string(), source],
    )
    .unwrap();
    drop(c);
    let rows = records(&ok(run(
        &t,
        &[
            "summary",
            "pending",
            "--no-auto",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-04",
            "--json",
        ],
    )));
    assert!(!rows.iter().any(|r| r["session"] == "root-alpha-0002"));
}

#[test]
fn self_controls_and_invalid_filters_are_not_silent() {
    let t = fixture();
    let invoke = |args: &[&str]| {
        Command::new(support::binary())
            .env_clear()
            .env("HOME", t.path().join("home"))
            .env("AGREP_HOME", t.path().join("home"))
            .env("AGREP_DATA_DIR", t.path().join("data"))
            .env("CODEX_THREAD_ID", "root-alpha-0001")
            .args(args)
            .output()
            .unwrap()
    };
    let without = invoke(&["chats", "--json", "cache"]);
    assert_eq!(without.status.code(), Some(1));
    // 0.4.9 marked `self` only on chats it judged against the caller's window, so an
    // included window carries no mark (py/search.py:8838-8846).
    let with = records(&ok(invoke(&["chats", "--json", "--self", "cache"])));
    assert_eq!(with[1]["session"], "root-alpha-0001");
    assert!(with[1].get("self").is_none());
    let no = records(&ok(invoke(&["chats", "--json", "--no-self", "--side"])));
    assert_eq!(no.len(), 2);
    assert_eq!(no[1]["session"], "root-alpha-0002");
    assert_eq!(no[1]["self"], false);
    for args in [
        vec!["chats", "--model", "test"],
        vec!["chats", "--project", ""],
        vec!["chats", "--agent", "bogus"],
        vec!["summary", "pending", "--group", "week"],
    ] {
        assert_eq!(run(&t, &args).status.code(), Some(2));
    }
    let malformed = run(&t, &["why", "@~!bad", "--json"]);
    assert_eq!(malformed.status.code(), Some(2));
    assert_eq!(records(&malformed)[0]["version"], 1);
}
#[test]
fn integration_drift_and_corrupt_database_are_diagnosed() {
    let t = fixture();
    ok(run(
        &t,
        &[
            "setup",
            "--yes",
            "--no-hook",
            "--no-semantic",
            "--no-archive",
        ],
    ));
    fs::write(t.path().join("home/.pi/agent/AGENTS.md"), "user only").unwrap();
    // 0.4.9's doctor exits 0 for a completed report and 1 only when a repair --fix
    // attempted failed (py/doctor.py:15).
    let out = ok(run(&t, &["doctor", "--json"]));
    let status = records(&out);
    assert_eq!(status[0]["status"]["integrations"][0]["drifted"], true);
    fs::write(
        t.path().join("home/.pi/agent/AGENTS.md"),
        "user text\n<!-- agrep:recall v1 -->\nowned\n<!-- /agrep:recall -->\n",
    )
    .unwrap();
    ok(run(&t, &["doctor", "--json"]));
    fs::write(t.path().join("data/index.db"), b"not a sqlite database").unwrap();
    let out = run(&t, &["doctor", "--fix"]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(
        fs::read(t.path().join("data/index.db")).unwrap(),
        b"not a sqlite database"
    );
    assert!(String::from_utf8_lossy(&out.stdout)
        .lines()
        .any(|l| l == "repair: failed - rebuilding a corrupt index requires confirmation on a terminal; run agrep doctor --fix"));
}
#[test]
fn doctor_and_audit_help_keep_0_4_9_exit_lines() {
    // Lines from 0.4.9's `agrep doctor --help` and `agrep audit --help`.
    let t = TempDir::new().unwrap();
    let doctor = String::from_utf8(ok(run(&t, &["doctor", "--help"])).stdout).unwrap();
    assert!(doctor.lines().any(|l| l
        == "exit: 0 diagnostic/action complete, 1 requested repair failed, 2 invalid options."));
    let audit = String::from_utf8(ok(run(&t, &["audit", "--help"])).stdout).unwrap();
    assert!(audit
        .lines()
        .any(|l| l == "exit: 0 clean, 1 evidence warning, 2 accounting error or invalid input."));
    assert!(
        audit.contains("promote coverage gaps and live/uncertified evidence to errors (exit 2)")
    );
}

#[test]
fn full_audit_reparses_every_adapter_fixture() {
    fn copy(from: &Path, to: &Path) {
        fs::create_dir_all(to).unwrap();
        for e in fs::read_dir(from).unwrap() {
            let e = e.unwrap();
            if e.file_type().unwrap().is_dir() {
                copy(&e.path(), &to.join(e.file_name()))
            } else {
                fs::copy(e.path(), to.join(e.file_name())).unwrap();
            }
        }
    }
    for name in [
        "claude",
        "codex",
        "pi",
        "kimi",
        "antigravity",
        "cline",
        "gemini",
        "gemini_jsonl",
        "gemini_compress",
        "opencode",
        "opencode_v2",
        "crush",
        "cursor",
    ] {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(name);
        if fixture.join("home").exists() {
            copy(&fixture.join("home"), &home)
        } else {
            fs::create_dir_all(&home).unwrap();
        }
        let seed = |file: &str, path: std::path::PathBuf| {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            rusqlite::Connection::open(path)
                .unwrap()
                .execute_batch(&fs::read_to_string(fixture.join(file)).unwrap())
                .unwrap();
        };
        match name {
            "opencode" | "opencode_v2" => {
                seed("seed.sql", home.join(".local/share/opencode/opencode.db"))
            }
            "crush" => seed("seed.sql", home.join(".local/share/crush/crush.db")),
            "cursor" => {
                seed(
                    "global.sql",
                    home.join(".config/Cursor/User/globalStorage/state.vscdb"),
                );
                let ws = home
                    .join(".config/Cursor/User/workspaceStorage/aaaa0001bbbb0002cccc0003dddd0004");
                seed("workspace.sql", ws.join("state.vscdb"));
                fs::write(
                    ws.join("workspace.json"),
                    r#"{"folder":"file:///c%3A/Users/tester/Desktop/flaky-app"}"#,
                )
                .unwrap();
            }
            _ => {}
        }
        ok(run(&t, &["index"]));
        let out = run(&t, &["audit", "--full", "--strict", "--json"]);
        assert!(
            out.status.success(),
            "fixture {name}: {} {}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
    }
}

#[cfg(windows)]
#[test]
fn windows_run_and_resume_preserve_native_argv_without_records() {
    let t = fixture();
    let bin = t.path().join("bin");
    fs::create_dir(&bin).unwrap();
    let compiler = std::env::var_os("RUSTC").unwrap_or_else(|| "rustc".into());
    let compiled = Command::new(compiler)
        .arg("--edition=2021")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/support/native_child.rs"))
        .arg("-o")
        .arg(bin.join("codex.exe"))
        .status()
        .unwrap();
    assert!(compiled.success());
    fs::write(bin.join("codex"), "extensionless npm shell shim").unwrap();
    let cwd = t.path().join("project alpha");
    let output = t.path().join("native-argv.bin");
    let args = ["one two", "", "-x", "space %value% & quotation \"value\""];
    let out = Command::new(support::binary())
        .env_clear()
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("PATH", &bin)
        .env("OUTPUT", &output)
        .args(["run", "codex", "--cwd", cwd.to_str().unwrap(), "--"])
        .args(args)
        .output()
        .unwrap();
    ok(out);
    let bytes = fs::read(&output).unwrap();
    let fields: Vec<_> = bytes
        .split(|b| *b == 0)
        .map(|b| String::from_utf8(b.to_vec()).unwrap())
        .collect();
    assert_eq!(&fields[1..fields.len() - 1], args);
    assert_eq!(
        Path::new(&fields[0]).canonicalize().unwrap(),
        cwd.canonicalize().unwrap()
    );
    assert!(!t.path().join("data/runs").exists());
    fs::copy(bin.join("codex.exe"), bin.join("pi.exe")).unwrap();
    let resumed = Command::new(support::binary())
        .env_clear()
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("PATH", &bin)
        .env("OUTPUT", &output)
        .args(["resume", "root-alpha-0001"])
        .output()
        .unwrap();
    ok(resumed);
    let bytes = fs::read(&output).unwrap();
    let fields: Vec<_> = bytes
        .split(|b| *b == 0)
        .map(|b| String::from_utf8(b.to_vec()).unwrap())
        .collect();
    assert_eq!(&fields[1..fields.len() - 1], ["-r", "root-alpha-0001"]);
    fs::copy(bin.join("codex.exe"), bin.join("omp.exe")).unwrap();
    let c = store::writer(&DataRoot(t.path().join("data"))).unwrap();
    c.execute("UPDATE sources SET path=?1 WHERE rowid=(SELECT source FROM prose WHERE session='root-alpha-0001' LIMIT 1)", [r"C:\fixture\.omp\agent\sessions\root-alpha-0001.jsonl"]).unwrap();
    drop(c);
    let resume = || {
        Command::new(support::binary())
            .env_clear()
            .env("AGREP_HOME", t.path().join("home"))
            .env("AGREP_DATA_DIR", t.path().join("data"))
            .env("PATH", &bin)
            .env("OUTPUT", &output)
            .args(["resume", "--no-auto", "root-alpha-0001"])
            .output()
            .unwrap()
    };
    ok(resume());
    assert!(fs::read_to_string(output.with_extension("executable"))
        .unwrap()
        .eq_ignore_ascii_case("omp.exe"));
    fs::remove_file(bin.join("omp.exe")).unwrap();
    ok(resume());
    assert!(fs::read_to_string(output.with_extension("executable"))
        .unwrap()
        .eq_ignore_ascii_case("pi.exe"));
}

#[test]
fn summary_folds_compaction_replies_without_inventing_turns() {
    let t = fixture();
    let c = store::writer(&DataRoot(t.path().join("data"))).unwrap();
    c.execute(
        "UPDATE prose SET who='recap' WHERE session='root-alpha-0001' AND who='user' AND turn=1",
        [],
    )
    .unwrap();
    drop(c);
    let out = records(&ok(run(
        &t,
        &[
            "summary",
            "--no-auto",
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-04",
            "--json",
        ],
    )));
    let project = &out[1];
    let work = project["worked_on"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["session"] == "root-alpha-0001")
        .unwrap();
    assert_eq!(work["turns"], 1);
    let item = project["open"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["session"] == "root-alpha-0001")
        .unwrap();
    assert_eq!(item["status"], "waiting_on_user");
    assert_eq!(item["turn"], 0);
}

#[test]
fn audit_does_not_collapse_identical_turns() {
    let t = fixture();
    let path = t
        .path()
        .join("home/.pi/agent/sessions/repeated/repeated.jsonl");
    let cwd = t.path().join("project alpha");
    write_chat(
        &path,
        "repeated-prompts",
        &cwd,
        &[
            ("2026-01-04T00:00:00Z", "same prompt", "same answer"),
            ("2026-01-04T00:01:00Z", "same prompt", "same answer"),
        ],
    );
    ok(run(&t, &["index"]));
    ok(run(&t, &["audit", "--full", "--strict"]));
    let c = store::writer(&DataRoot(t.path().join("data"))).unwrap();
    c.execute(
        "DELETE FROM prose WHERE session='repeated-prompts' AND position=1",
        [],
    )
    .unwrap();
    drop(c);
    let out = run(&t, &["audit", "--full", "--strict", "--json"]);
    // --strict makes a gap an error (py/audit.py:2515-2517).
    assert_eq!(out.status.code(), Some(2));
    assert!(records(&out)[0]["gap_details"]
        .as_array()
        .unwrap()
        .iter()
        .any(|g| g["session"] == "repeated-prompts"));
}

#[test]
fn status_refuses_unmarked_nonempty_data() {
    let t = TempDir::new().unwrap();
    fs::create_dir(t.path().join("data")).unwrap();
    fs::write(t.path().join("data/unrecognized"), b"keep").unwrap();
    let out = run(&t, &["status", "--json"]);
    assert_eq!(out.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&out.stderr).contains("nonempty data root without FORMAT"));
    assert_eq!(
        fs::read(t.path().join("data/unrecognized")).unwrap(),
        b"keep"
    );
}

#[cfg(unix)]
#[test]
fn doctor_rebuilds_corrupt_index_only_after_terminal_consent() {
    use std::io::Write;
    use std::os::fd::FromRawFd;
    use std::process::Stdio;
    let t = fixture();
    fs::write(t.path().join("data/index.db"), b"corrupt database").unwrap();
    let (mut master, mut slave) = (-1, -1);
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        0
    );
    assert_eq!(
        unsafe { libc::fcntl(master, libc::F_SETFD, libc::FD_CLOEXEC) },
        0
    );
    assert_eq!(
        unsafe { libc::fcntl(slave, libc::F_SETFD, libc::FD_CLOEXEC) },
        0
    );
    let mut master = unsafe { fs::File::from_raw_fd(master) };
    let slave = unsafe { fs::File::from_raw_fd(slave) };
    let child = Command::new(support::binary())
        .env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .args(["doctor", "--fix"])
        .stdin(Stdio::from(slave))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    master.write_all(b"y\n").unwrap();
    let output = ok(child.wait_with_output().unwrap());
    assert!(String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|l| l == "repaired: rebuilt index; corrupt database retained"));
    let backup = fs::read_dir(t.path().join("data"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("index.corrupt-")
        })
        .unwrap();
    assert_eq!(fs::read(backup).unwrap(), b"corrupt database");
    ok(run(&t, &["chats"]));
}

#[test]
fn resume_noninteractive_picker_and_empty_list_exit_contracts() {
    let t = fixture();
    assert_eq!(run(&t, &["resume"]).status.code(), Some(0));
    let empty = TempDir::new().unwrap();
    fs::create_dir(empty.path().join("home")).unwrap();
    ok(run(&empty, &["index"]));
    assert_eq!(run(&empty, &["resume", "--list"]).status.code(), Some(2));
    assert_eq!(
        run(&t, &["resume", "no-such-session"]).status.code(),
        Some(1)
    );
}

#[cfg(unix)]
#[test]
fn run_lets_a_pipeline_agent_read_terminal_input() {
    use std::io::{BufRead, BufReader, Write};
    use std::os::fd::FromRawFd;
    use std::os::unix::process::CommandExt;
    use std::process::Stdio;
    let t = fixture();
    fake_agent(
        &t,
        "codex",
        "printf 'ready\\n'; read answer; printf '%s' \"$answer\" > \"$OUTPUT\"",
    );
    let (mut master, mut slave) = (-1, -1);
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        0
    );
    assert_eq!(
        unsafe { libc::fcntl(master, libc::F_SETFD, libc::FD_CLOEXEC) },
        0
    );
    assert_eq!(
        unsafe { libc::fcntl(slave, libc::F_SETFD, libc::FD_CLOEXEC) },
        0
    );
    let mut master = unsafe { fs::File::from_raw_fd(master) };
    let slave = unsafe { fs::File::from_raw_fd(slave) };
    let mut cmd = launch_cmd(&t);
    cmd.args(["run", "codex"])
        .stdin(Stdio::from(slave))
        .stdout(Stdio::piped());
    unsafe {
        cmd.pre_exec(|| {
            if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = cmd.spawn().unwrap();
    drop(cmd);
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(line, "ready\n");
    master.write_all(b"terminal input\n").unwrap();
    // A terminal emulator consumes echoed output. On macOS an exiting session
    // leader drains its controlling PTY, so leaving this queued would deadlock
    // the fixture's wait even though the agent finished successfully.
    let mut echoed = String::new();
    BufReader::new(&mut master).read_line(&mut echoed).unwrap();
    assert!(echoed.contains("terminal input"));
    assert!(child.wait().unwrap().success());
    assert_eq!(
        fs::read_to_string(t.path().join("argv.txt")).unwrap(),
        "terminal input"
    );
}

#[cfg(unix)]
#[test]
fn run_owns_a_pipeline_agent_and_ends_its_descendants() {
    use std::io::{BufRead, BufReader};
    use std::os::fd::FromRawFd;
    use std::os::unix::process::CommandExt;
    use std::process::Stdio;
    let t = fixture();
    // The descendant also ignores the HUP this test's session leader sends its foreground
    // group on exit, so only the agent's own group can end it.
    fake_agent(
        &t,
        "codex",
        "(trap '' TERM HUP; exec /bin/sleep 60) & printf '%s\\n' \"$!\"; \
         if (: </dev/tty) 2>/dev/null; then echo terminal; else echo detached; fi",
    );
    let (mut master, mut slave) = (-1, -1);
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        0
    );
    for fd in [master, slave] {
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) },
            0
        );
    }
    let master = unsafe { fs::File::from_raw_fd(master) };
    let slave = unsafe { fs::File::from_raw_fd(slave) };
    // `agrep run codex | tee log` typed at a terminal.
    let mut cmd = launch_cmd(&t);
    cmd.args(["run", "codex"])
        .stdin(Stdio::from(slave))
        .stdout(Stdio::piped());
    unsafe {
        cmd.pre_exec(|| {
            if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = cmd.spawn().unwrap();
    drop(cmd);
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let descendant: u32 = lines.next().unwrap().unwrap().parse().unwrap();
    let terminal = lines.next().unwrap().unwrap();
    let status = child.wait().unwrap();
    let ended = exit::wait(descendant, std::time::Duration::from_secs(5)).unwrap();
    if !ended {
        unsafe { libc::kill(descendant as i32, libc::SIGKILL) };
    }
    drop(master);
    assert!(status.success());
    assert!(ended, "descendant {descendant} outlived agrep run");
    // As in 0.4.9, a pipeline stage runs detached: the terminal stays with the whole job.
    assert_eq!(terminal, "detached");
}

#[cfg(unix)]
#[test]
fn run_follows_shell_job_control() {
    use std::io::{BufRead, Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::{net::UnixStream, process::CommandExt};
    use std::time::{Duration, Instant};
    struct Shell {
        process: std::process::Child,
        master: Option<fs::File>,
        seen: Vec<u8>,
        from: usize,
    }
    impl Shell {
        fn type_line(&mut self, line: &str) {
            self.press(format!("{line}\n").as_bytes());
        }
        fn press(&mut self, keys: &[u8]) {
            self.master.as_mut().unwrap().write_all(keys).unwrap();
        }
        /// Reads the terminal until `needle` appears after the previous match.
        fn expect(&mut self, needle: &str) {
            let deadline = Instant::now() + Duration::from_secs(60);
            let master = self.master.as_mut().unwrap();
            let at = loop {
                let unread = &self.seen[self.from..];
                if let Some(at) = unread
                    .windows(needle.len())
                    .position(|w| w == needle.as_bytes())
                {
                    break at;
                }
                let mut poll = libc::pollfd {
                    fd: master.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                };
                let left = deadline.saturating_duration_since(Instant::now());
                let ready = unsafe { libc::poll(&mut poll, 1, left.as_millis() as libc::c_int) };
                let mut chunk = [0; 4096];
                let read = if ready > 0 {
                    master.read(&mut chunk).unwrap_or(0)
                } else {
                    0
                };
                assert!(
                    read > 0,
                    "the terminal never showed {needle:?}: {:?}",
                    String::from_utf8_lossy(&self.seen)
                );
                self.seen.extend_from_slice(&chunk[..read]);
            };
            self.from += at + needle.len();
        }
    }
    impl Drop for Shell {
        fn drop(&mut self) {
            // Hanging up the terminal ends the shell, which hangs up its jobs.
            self.master.take();
            if !exit::wait(self.process.id(), Duration::from_secs(10)).unwrap_or(false) {
                let _ = self.process.kill();
            }
            let _ = self.process.wait();
        }
    }
    let t = fixture();
    let (mut master, mut slave) = (-1, -1);
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        0
    );
    for fd in [master, slave] {
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) },
            0
        );
    }
    let master = unsafe { fs::File::from_raw_fd(master) };
    let slave = unsafe { fs::File::from_raw_fd(slave) };
    // The agent's gate is descriptor 3 of the shell, and through agrep, of the agent.
    let (gate, theirs) = UnixStream::pair().unwrap();
    let held = theirs.as_raw_fd();
    // An interactive job-control shell on its own terminal; -b reports a stopped job at once.
    let mut command = Command::new("/bin/bash");
    command
        .args(["--norc", "--noprofile", "-ib"])
        .env_clear()
        .env("HOME", t.path().join("home"))
        .env("AGREP_HOME", t.path().join("home"))
        .env("AGREP_DATA_DIR", t.path().join("data"))
        .env("PATH", t.path().join("bin"))
        .env("OUTPUT", t.path().join("argv.txt"))
        .env("CLI", support::binary())
        .env("PS1", "shell> ")
        .stdin(slave.try_clone().unwrap())
        .stdout(slave.try_clone().unwrap())
        .stderr(slave);
    unsafe {
        command.pre_exec(move || {
            let gate = || {
                if held == 3 {
                    libc::fcntl(3, libc::F_SETFD, 0)
                } else {
                    libc::dup2(held, 3)
                }
            };
            if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY as _, 0) < 0 || gate() < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let mut shell = Shell {
        process: command.spawn().unwrap(),
        master: Some(master),
        seen: vec![],
        from: 0,
    };
    drop((command, theirs));
    // Every job starts in the background: bash 3.2 also hands a foreground job the terminal
    // from the parent after the fork, which can land after agrep lent it to the agent.
    // Input is typed once bash, running `fg`, has printed the job: bash reads no further,
    // so the line waits for the agent.
    let fg = |shell: &mut Shell| {
        shell.type_line("fg; printf 'fg-status:%s\\n' \"$?\"");
        shell.expect("fg-status:%s");
        shell.expect("\"$CLI\" run codex");
    };
    // The agent reads at once, so it stops in the background and agrep's job stops with
    // it; `fg` continues agrep as the terminal's foreground. Its own SIGTTIN later stands
    // for terminal access a beat before a handoff landed: holding the terminal, it goes on.
    fake_agent(
        &t,
        "codex",
        "read answer; kill -TTIN $$; printf '%s' \"$answer\" > \"$OUTPUT\"",
    );
    shell.expect("shell> ");
    shell.type_line("\"$CLI\" run codex &");
    shell.expect("Stopped");
    fg(&mut shell);
    shell.type_line("typed after fg");
    shell.expect("fg-status:0");
    assert_eq!(
        fs::read_to_string(t.path().join("argv.txt")).unwrap(),
        "typed after fg"
    );
    // agrep alone is stopped, by its agent so that bash is idle to report it, while the
    // agent runs on. Released before `fg`, the agent then reads the terminal, and gets it
    // once `fg` continues agrep, whenever that read lands.
    fake_agent(
        &t,
        "codex",
        "kill -STOP $PPID; read go <&3; read answer; printf '%s' \"$answer\" > \"$OUTPUT\"",
    );
    shell.expect("shell> ");
    shell.type_line("\"$CLI\" run codex &");
    shell.expect("Stopped");
    (&gate).write_all(b"go\n").unwrap();
    fg(&mut shell);
    shell.type_line("typed after a running agent was brought forward");
    shell.expect("fg-status:0");
    assert_eq!(
        fs::read_to_string(t.path().join("argv.txt")).unwrap(),
        "typed after a running agent was brought forward"
    );
    // A running background job gets no SIGCONT from `fg`. Until the agent touches the
    // terminal, Ctrl-Z therefore targets agrep, which must stop the agent's group first
    // (0.4.9's _supervise_owned forwards suspend_requested before mirroring its stop).
    fake_agent(
        &t,
        "codex",
        "printf '%s %s\\n' \"$$\" \"$PPID\" >&3; read go <&3; read answer; printf '%s' \"$answer\" > \"$OUTPUT\"",
    );
    shell.expect("shell> ");
    shell.type_line("\"$CLI\" run codex &");
    shell.expect("shell> ");
    gate.set_read_timeout(Some(Duration::from_secs(60)))
        .unwrap();
    let mut ids = String::new();
    std::io::BufReader::new(&gate).read_line(&mut ids).unwrap();
    let ids: Vec<i32> = ids.split_whitespace().map(|s| s.parse().unwrap()).collect();
    assert_eq!(ids.len(), 2);
    fg(&mut shell);
    // bash prints the job before it hands the job the terminal, and nothing signals the
    // handoff itself, so wait (bounded) while the terminal still belongs to the shell.
    let master = shell.master.as_ref().unwrap().as_raw_fd();
    let shell_group = shell.process.id() as i32;
    let deadline = Instant::now() + Duration::from_secs(60);
    while unsafe { libc::tcgetpgrp(master) } == shell_group {
        assert!(
            Instant::now() < deadline,
            "bash never handed the job the terminal"
        );
        std::thread::yield_now();
    }
    assert_eq!(
        unsafe { libc::tcgetpgrp(master) },
        ids[1],
        "agrep must still hold the terminal before the agent touches it"
    );
    shell.press(b"\x1a");
    shell.expect("Stopped");
    shell.expect("shell> ");
    let state = Command::new("/bin/ps")
        .args(["-o", "stat=", "-p", &ids[0].to_string()])
        .output()
        .unwrap();
    assert!(state.status.success());
    let state = String::from_utf8(state.stdout).unwrap();
    assert!(
        state.trim().starts_with('T'),
        "Ctrl-Z stopped agrep but left its agent running: {state:?}"
    );
    fg(&mut shell);
    (&gate).write_all(b"go\n").unwrap();
    shell.type_line("typed after wrapper Ctrl-Z");
    shell.expect("fg-status:0");
    assert_eq!(
        fs::read_to_string(t.path().join("argv.txt")).unwrap(),
        "typed after wrapper Ctrl-Z"
    );
    // Once the agent has read from the terminal `fg` lent it, Ctrl-Z stops the agent and
    // the job with it; `bg` continues agrep in the background, where it must neither hand
    // the agent the terminal nor take it or reset its modes when the agent exits. A mode
    // set after `bg` stands for the shell's own.
    fake_agent(
        &t,
        "codex",
        "read line; printf 'ready\\n'; read go <&3; exit 0",
    );
    shell.expect("shell> ");
    shell.type_line("\"$CLI\" run codex &");
    shell.expect("Stopped");
    fg(&mut shell);
    shell.type_line("line");
    shell.expect("ready");
    shell.press(b"\x1a");
    shell.expect("Stopped");
    shell.type_line("bg");
    shell.expect("run codex &");
    shell.expect("shell> ");
    let terminal = shell.master.as_ref().unwrap().as_raw_fd();
    let mut modes: libc::termios = unsafe { std::mem::zeroed() };
    assert_eq!(unsafe { libc::tcgetattr(terminal, &mut modes) }, 0);
    modes.c_lflag |= libc::ECHOPRT;
    assert_eq!(
        unsafe { libc::tcsetattr(terminal, libc::TCSANOW, &modes) },
        0
    );
    (&gate).write_all(b"go\n").unwrap();
    shell.expect("Done");
    assert_eq!(unsafe { libc::tcgetattr(terminal, &mut modes) }, 0);
    assert_ne!(
        modes.c_lflag & libc::ECHOPRT,
        0,
        "agrep reset the shell's modes"
    );
    shell.type_line("printf 'shell-%s\\n' ok");
    shell.expect("shell-ok");
    // An agent configuring the terminal from the background stops there instead of changing
    // it under the shell. Whether the change then succeeds or reports the interruption is
    // up to stty on each platform.
    fake_agent(&t, "codex", "exec /bin/stty -echo");
    shell.expect("shell> ");
    shell.type_line("\"$CLI\" run codex &");
    shell.expect("Stopped");
    fg(&mut shell);
    shell.expect("fg-status:");
}

#[test]
fn why_keeps_its_versioned_envelope_for_corrupt_rows() {
    let t = fixture();
    let c = store::writer(&DataRoot(t.path().join("data"))).unwrap();
    c.execute(
        "UPDATE sessions SET last_ts='invalid timestamp' WHERE id='root-alpha-0001'",
        [],
    )
    .unwrap();
    drop(c);
    let out = run(&t, &["why", "root-alpha-0001", "--json"]);
    assert_eq!(out.status.code(), Some(2));
    let rows = records(&out);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["version"], 1);
    assert_eq!(rows[0]["verdict"], "unprovable");
    assert_eq!(rows[0]["next_action"], "agrep doctor");
}

#[cfg(unix)]
#[test]
fn resume_chooses_omp_for_windows_origin_paths() {
    let t = fixture();
    fake_agent(&t, "pi", "printf pi > \"$OUTPUT\"");
    fake_agent(&t, "omp", "printf omp > \"$OUTPUT\"");
    let c = store::writer(&DataRoot(t.path().join("data"))).unwrap();
    c.execute("UPDATE sources SET path=?1 WHERE rowid=(SELECT source FROM prose WHERE session='root-alpha-0001' LIMIT 1)",[r"C:\fixture\.omp\agent\sessions\root-alpha-0001.jsonl"]).unwrap();
    drop(c);
    ok(launch_cmd(&t)
        .args(["resume", "--no-auto", "root-alpha-0001"])
        .output()
        .unwrap());
    assert_eq!(
        fs::read_to_string(t.path().join("argv.txt")).unwrap(),
        "omp"
    );
}

#[test]
fn browse_color_switches_apply_only_to_human_output() {
    let t = fixture();
    for args in [
        vec!["chats"],
        vec!["resume", "--list"],
        vec!["summary", "--since", "2026-01-01", "--until", "2026-01-04"],
    ] {
        let mut colored = args.clone();
        colored.extend(["--color", "always"]);
        assert!(ok(run(&t, &colored)).stdout.contains(&27));
        let mut plain = args;
        plain.extend(["--color", "never"]);
        assert!(!ok(run(&t, &plain)).stdout.contains(&27));
    }
    let out = ok(run(&t, &["chats", "--json", "--color", "always"]));
    assert!(!out.stdout.contains(&27));
    records(&out);
}
