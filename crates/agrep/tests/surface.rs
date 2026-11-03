mod support;
use agrep_core::{evidence, handles, pages};
use rusqlite::Connection;
use serde_json::Value;
use std::{
    fs,
    path::Path,
    process::{Command, Output},
};
use tempfile::TempDir;
fn cli(home: &Path, data: &Path, args: &[&str], env: &[(&str, &str)]) -> Output {
    let mut cmd = Command::new(support::binary());
    cmd.env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_CALLER_PUBLICATION_DIR", home.join("empty-callers"))
        .env("AGREP_PROFILE", "classic")
        .args(args);
    for (k, v) in env {
        cmd.env(k, v);
    }
    cmd.output().unwrap()
}
fn ok(o: Output) -> Output {
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    o
}
fn copy(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for e in fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        if e.file_type().unwrap().is_dir() {
            copy(&e.path(), &dst.join(e.file_name()));
        } else {
            fs::copy(e.path(), dst.join(e.file_name())).unwrap();
        }
    }
}
fn pi() -> (TempDir, std::path::PathBuf, std::path::PathBuf) {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    copy(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/pi/home"),
        &h,
    );
    ok(cli(&h, &d, &["index"], &[]));
    (t, h, d)
}
fn values(o: &Output) -> Vec<Value> {
    String::from_utf8_lossy(&o.stdout)
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}
#[test]
fn fixture_search_golden_modes() {
    let (_t, h, d) = pi();
    for (name, args) in [
        ("flat", vec!["retry", "--flat"]),
        ("count", vec!["retry", "-c"]),
        ("tiers", vec!["retry", "--count-by-tier"]),
        ("around", vec!["around", "01930000", "0"]),
        ("recall", vec!["recall", "retry", "--lexical"]),
        ("probe", vec!["recall", "retry", "--lexical", "--probe"]),
    ] {
        let o = ok(cli(&h, &d, &args, &[]));
        let expected = fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("tests/golden/pi-{name}.txt")),
        )
        .unwrap();
        // Chrono uses the Windows system timezone, regardless of TZ. Keep the
        // golden's UTC instant but render it in the same local zone as the CLI.
        let local = chrono::DateTime::parse_from_rfc3339("2026-01-02T03:04:00Z")
            .unwrap()
            .with_timezone(&chrono::Local)
            .format("%Y-%m-%d %H:%M")
            .to_string();
        assert_eq!(
            String::from_utf8(o.stdout).unwrap(),
            expected.replace("2026-01-02 03:04", &local),
            "{name}"
        );
    }
    let rows = values(&ok(cli(&h, &d, &["retry", "--json"], &[])));
    assert_eq!(rows[0]["completeness"]["total"], 3);
    assert_eq!(rows[0]["completeness"]["unit"], "matching row");
    assert_eq!(rows[1]["handle"], "@01930000:2.18ba");
    assert_eq!(rows[1]["project"], "/work/alpha");
    assert!(rows[1].get("text").is_none());
    let j = ok(cli(
        &h,
        &d,
        &["recall", "retry", "--lexical", "--json"],
        &[],
    ));
    let v: Value = serde_json::from_slice(&j.stdout).unwrap();
    assert_eq!(v["hits"].as_array().unwrap().len(), 1);
    assert_eq!(v["hits"][0]["window"].as_array().unwrap().len(), 5);
}
#[test]
fn handle_rescue_clamp_and_tool_identity() {
    let (_t, h, d) = pi();
    let handle = "@01930000:0.cd22";
    let o = ok(cli(&h, &d, &["around", handle], &[]));
    let text = String::from_utf8(o.stdout).unwrap();
    assert!(text.contains("user: why did"));
    assert!(!text.contains("now add jitter"));
    let o = ok(cli(&h, &d, &["around", "01930000", "999"], &[]));
    assert!(String::from_utf8(o.stderr).unwrap().contains("clamped"));
    let clamped_json = ok(cli(&h, &d, &["around", "01930000", "999", "--json"], &[]));
    let records = values(&clamped_json);
    assert_eq!(records[0]["served"]["served_as_requested"], false);
    assert_eq!(
        records[0]["served"]["divergences"][0]["kind"],
        "turn_clamped"
    );
    assert_eq!(
        cli(&h, &d, &["around", "@01930000:999.cd22"], &[])
            .status
            .code(),
        Some(2)
    );
    assert_eq!(
        cli(&h, &d, &["around", "@01930000:0.0000", "--json"], &[])
            .status
            .code(),
        Some(2)
    );
    let db = Connection::open(d.join("index.db")).unwrap();
    db.execute(
        "UPDATE prose SET position=3 WHERE session LIKE '01930000%' AND position=0 AND who='user'",
        [],
    )
    .unwrap();
    let o = ok(cli(&h, &d, &["around", handle, "--no-auto"], &[]));
    assert!(String::from_utf8(o.stderr)
        .unwrap()
        .contains("content moved: turn 0 → 3"));
    let rows = values(&ok(cli(
        &h,
        &d,
        &["backoff", "--who", "tool", "--json"],
        &[],
    )));
    let handle = rows[1]["handle"].as_str().unwrap();
    let o = ok(cli(
        &h,
        &d,
        &["around", handle, "--tool-output", "300"],
        &[],
    ));
    assert!(String::from_utf8(o.stdout)
        .unwrap()
        .contains("Duration::from_secs(30)"));
    assert_eq!(
        cli(&h, &d, &["around", handle, "--no-tools"], &[])
            .status
            .code(),
        Some(2)
    );
    let claim = handles::parse(handle).unwrap();
    let bad = handle.replace(claim.event.as_ref().unwrap(), "000000000000000000000000");
    assert_eq!(cli(&h, &d, &["around", &bad], &[]).status.code(), Some(2));
}
#[test]
fn caller_window_applies_to_every_machine_lane() {
    let (_t, h, d) = pi();
    let caller = [("CODEX_THREAD_ID", "01930000-0000-7000-8000-000000000001")];
    let n = ok(cli(&h, &d, &["retry", "-c"], &caller));
    assert_eq!(n.stdout, b"2\n");
    let n = ok(cli(&h, &d, &["retry", "-c", "--self"], &caller));
    assert_eq!(n.stdout, b"3\n");
    assert_eq!(
        cli(&h, &d, &["retry", "-c", "--no-self"], &caller)
            .status
            .code(),
        Some(1)
    );
    let around = values(&ok(cli(
        &h,
        &d,
        &["around", "01930000", "0", "--json"],
        &caller,
    )));
    assert_eq!(around[0]["self_exclusion"]["active"], false);
    assert_eq!(around[1]["self"], true);
    let r = values(&ok(cli(&h, &d, &["retry", "--json"], &caller)));
    assert_eq!(r[1]["self"], true);
    assert_eq!(r[1]["turn"], 0);
    let o = cli(
        &h,
        &d,
        &["retry", "--json"],
        &[("CODEX_SANDBOX", "seatbelt")],
    );
    assert_eq!(
        String::from_utf8(o.stderr)
            .unwrap()
            .lines()
            .filter(|l| l.contains("caller unknown"))
            .count(),
        0
    );
}
#[test]
fn postcompact_stays_before_boundary_and_in_caller() {
    let (_t, h, d) = pi();
    let o = ok(cli(
        &h,
        &d,
        &["postcompact", "--session", "01930000", "--json"],
        &[],
    ));
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(v["status"], "recovered");
    assert_eq!(v["rows"].as_array().unwrap().len(), 2);
    assert!(v["rows"].as_array().unwrap().iter().all(|r| r["turn"] == 0));
    assert!(o.stdout.len() <= 16000);
    let o = ok(cli(&h, &d, &["postcompact", "--session", "01930000"], &[]));
    assert!(o.stdout.len() <= 8000);
    assert!(!String::from_utf8(o.stdout)
        .unwrap()
        .contains("now add jitter"));
    assert_eq!(
        cli(&h, &d, &["postcompact", "--session", "01950000"], &[])
            .status
            .code(),
        Some(2)
    );
    assert_eq!(
        cli(
            &h,
            &d,
            &["postcompact", "--session", "01930000", "--boundary-ms", "1"],
            &[]
        )
        .status
        .code(),
        Some(2)
    );
}
#[test]
fn budgets_pack_and_frozen_pages() {
    let (_t, h, d) = pi();
    for mode in [
        vec![
            "recall",
            "retry",
            "--lexical",
            "--budget",
            "160",
            "--color",
            "always",
        ],
        vec![
            "pack",
            "retry",
            "question",
            "--lexical",
            "--budget",
            "2048",
            "--json",
        ],
    ] {
        let o = ok(cli(&h, &d, &mode, &[]));
        let budget: usize = mode[mode.iter().position(|s| *s == "--budget").unwrap() + 1]
            .parse()
            .unwrap();
        assert!(o.stdout.len() <= budget);
        assert!(std::str::from_utf8(&o.stdout).is_ok());
    }
    let o = cli(&h, &d, &["recall", "retry", "--budget", "12"], &[]);
    assert_eq!(o.status.code(), Some(2));
    assert!(o.stdout.is_empty());
    let root = agrep_core::data::DataRoot(d.clone());
    let f = pages::Frozen {
        records: (0..30)
            .map(|i| pages::Record {
                line: format!("@saved:{i}.1234 {}", "€".repeat(300)),
                family: i.to_string(),
                score: 1.,
                lane: "phrase".into(),
                tool: false,
                identity: i.to_string(),
            })
            .collect(),
        query: "saved".into(),
        total: 30,
        exact: true,
        shown: 0,
        requested: None,
        chat_scoped: false,
        full_argv: vec![],
    };
    let handle = pages::save(&root, &f).unwrap().unwrap();
    let old = pages::load(&root, &handle).unwrap();
    assert_eq!(old.records[0].line, f.records[0].line);
    let page = ok(cli(&h, &d, &["--more", &handle], &[]));
    assert!(page.stdout.len() <= 3584);
    assert!(String::from_utf8(page.stdout)
        .unwrap()
        .starts_with("@saved:"));
    Connection::open(d.join("pages.db"))
        .unwrap()
        .execute("UPDATE pages SET expires_ms=0", [])
        .unwrap();
    assert_eq!(
        cli(&h, &d, &["--more", &handle], &[]).status.code(),
        Some(2)
    );
    let c = agrep_core::store::reader(&root).unwrap();
    let w = evidence::load(&c, &evidence::Request::new("01930000", Some(0), Some(0))).unwrap();
    assert_eq!(
        w.events[0].output,
        "src/retry.rs:88: cap = Duration::from_secs(30)"
    );
}
#[test]
fn snapshot_windows_keep_committed_snapshot() {
    let (_t, _h, d) = pi();
    let root = agrep_core::data::DataRoot(d.clone());
    let c = agrep_core::store::reader(&root).unwrap();
    let req = evidence::Request::new("01930000", Some(0), Some(0));
    let w = evidence::load(&c, &req).unwrap();
    let writer = Connection::open(d.join("index.db")).unwrap();
    writer
        .execute_batch("BEGIN; DELETE FROM prose; DELETE FROM tools; COMMIT;")
        .unwrap();
    let second = evidence::load(&c, &req).unwrap();
    assert_eq!(w.rows.len(), second.rows.len());
    assert_eq!(w.events.len(), second.events.len());
}

#[test]
fn text_caps_filters_and_color_are_explicit() {
    let (_t, h, d) = pi();
    let db = Connection::open(d.join("index.db")).unwrap();
    let text = "é🙂 evidence ".repeat(1000);
    db.execute(
        "UPDATE prose SET text=? WHERE session LIKE '01930000%' AND position=0 AND who='user'",
        [text.as_bytes()],
    )
    .unwrap();
    let small = ok(cli(
        &h,
        &d,
        &[
            "around",
            "01930000",
            "0",
            "-C",
            "0",
            "--max-chars",
            "64",
            "--no-auto",
        ],
        &[],
    ));
    assert!(small.stdout.len() < 1200);
    assert!(String::from_utf8(small.stdout)
        .unwrap()
        .contains("chars - agrep around"));
    let full = ok(cli(
        &h,
        &d,
        &[
            "around",
            "01930000",
            "0",
            "-C",
            "0",
            "--max-chars",
            "0",
            "--no-auto",
        ],
        &[],
    ));
    assert!(String::from_utf8(full.stdout).unwrap().contains(&text));
    let colored = ok(cli(
        &h,
        &d,
        &["retry", "--color", "always", "--classic"],
        &[],
    ));
    assert!(colored.stdout.windows(2).any(|w| w == b"\x1b["));
    assert!(String::from_utf8(colored.stdout)
        .unwrap()
        .contains("\x1b[2m›"));
    let user = ok(cli(
        &h,
        &d,
        &[
            "around",
            "01930000",
            "--whole",
            "--who",
            "you",
            "--json",
            "--no-auto",
        ],
        &[],
    ));
    assert!(values(&user).iter().skip(1).all(|v| v["who"] == "user"));
    let missing = cli(
        &h,
        &d,
        &["retry", "--model", "never-indexed", "--json"],
        &[],
    );
    assert_eq!(missing.status.code(), Some(1));
    assert_eq!(
        values(&missing)[0]["filter_coverage"]["empty_dimensions"][0]["dimension"],
        "model"
    );
    for args in [
        vec!["retry", "--who", "nobody"],
        vec!["retry", "--project", ""],
        vec!["retry", "--soft"],
    ] {
        let o = cli(&h, &d, &args, &[]);
        assert_eq!(o.status.code(), Some(2));
        assert!(o.stdout.is_empty());
    }
}

#[test]
fn recall_direct_handle_preserves_its_role_and_overrides_filters() {
    let (_t, h, d) = pi();
    let o = ok(cli(
        &h,
        &d,
        &[
            "recall",
            "@01930000:0.e5c8",
            "--chat",
            "missing-chat",
            "--json",
        ],
        &[],
    ));
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(v["hits"][0]["handle"], "@01930000:0.e5c8");
    assert_eq!(v["filter_override"]["reason"], "direct-handle");
    let rows = values(&ok(cli(
        &h,
        &d,
        &["backoff", "--who", "tool", "--json"],
        &[],
    )));
    let handle = rows[1]["handle"].as_str().unwrap();
    let o = ok(cli(&h, &d, &["recall", handle, "--lexical"], &[]));
    let body = String::from_utf8(o.stdout).unwrap();
    assert!(body.contains(handle));
    assert!(body.contains("Duration::from_secs(30)"));
}

#[test]
fn compact_defers_two_prose_roles_at_one_turn() {
    let (_t, h, d) = pi();
    let out = ok(cli(
        &h,
        &d,
        &["legacy", "--lexical"],
        &[("AGREP_PROFILE", "compact")],
    ));
    let body = String::from_utf8(out.stdout).unwrap();
    assert_eq!(body.lines().count(), 1);
    assert!(body.contains("legacy linear question"));
    assert!(String::from_utf8(out.stderr)
        .unwrap()
        .contains("agrep --more"));
}

#[test]
fn aliases_and_page_arguments_are_not_silently_dropped() {
    let (_t, h, d) = pi();
    let out = ok(cli(&h, &d, &["retry", "--agent", "omp", "-c"], &[]));
    assert_eq!(out.stdout, b"3\n");
    let out = cli(&h, &d, &["--more", "m.deadbeef", "retry"], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
}

#[test]
fn a_child_caller_does_not_hide_a_later_sibling() {
    let (_t, _h, d) = pi();
    let root = "01940000-0000-7000-8000-000000000003";
    let child = "01940000-0000-7000-8000-000000000004";
    let c = Connection::open(d.join("index.db")).unwrap();
    c.execute("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES('pi','later-sibling','beta',?1,?1,1,'later',1900000000000,1900000000000)",[root]).unwrap();
    let caller = agrep_core::caller::Caller::resolve(
        &c,
        agrep_core::caller::Identity {
            session: Some(child.into()),
            ..Default::default()
        },
    )
    .unwrap();
    assert!(caller.members.iter().any(|s| s == "later-sibling"));
    assert!(!caller.live_members.iter().any(|s| s == "later-sibling"));
}
