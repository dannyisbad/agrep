mod support;
use agrep_core::{data::DataRoot, store};
use rusqlite::{Connection, OpenFlags};
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};
use tempfile::TempDir;
fn cli(home: &Path, data: &Path, args: &[&str]) -> Output {
    Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_PROFILE", "classic")
        .args(args)
        .output()
        .expect("built agrep executable is required")
}
fn ok(out: Output) -> Output {
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    out
}
// Rebuild invariants concern row counts. Status also reports physical disk use and
// refresh timestamps, which are expected to change when the same rows are rebuilt.
fn status_counts(out: Output) -> Value {
    let status: Value = serde_json::from_slice(&ok(out).stdout).unwrap();
    let counts = status["index"]["counts"].clone();
    assert!(counts.is_object(), "status must report committed counts");
    counts
}
fn copy(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for e in fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        let p = dst.join(e.file_name());
        if e.path().is_dir() {
            copy(&e.path(), &p)
        } else {
            fs::copy(e.path(), p).unwrap();
        }
    }
}
fn fixture(name: &str, home: &Path) {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    if dir.join("home").exists() {
        copy(&dir.join("home"), home)
    }
    let seed = |sql: &str, p: PathBuf| {
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        Connection::open(p)
            .unwrap()
            .execute_batch(&fs::read_to_string(dir.join(sql)).unwrap())
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
            let ws =
                home.join(".config/Cursor/User/workspaceStorage/aaaa0001bbbb0002cccc0003dddd0004");
            seed("workspace.sql", ws.join("state.vscdb"));
            fs::write(
                ws.join("workspace.json"),
                r#"{"folder":"file:///c%3A/Users/tester/Desktop/flaky-app"}"#,
            )
            .unwrap();
        }
        _ => {}
    }
}
fn snapshot(root: &Path) -> std::collections::BTreeMap<PathBuf, Vec<u8>> {
    let mut files = std::collections::BTreeMap::new();
    fn visit(root: &Path, p: &Path, files: &mut std::collections::BTreeMap<PathBuf, Vec<u8>>) {
        for e in fs::read_dir(p).unwrap() {
            let e = e.unwrap();
            if e.file_type().unwrap().is_dir() {
                visit(root, &e.path(), files)
            } else {
                files.insert(
                    e.path().strip_prefix(root).unwrap().into(),
                    fs::read(e.path()).unwrap(),
                );
            }
        }
    }
    visit(root, root, &mut files);
    files
}
fn expected(name: &str) -> Vec<Value> {
    fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(name)
            .join("golden.txt"),
    )
    .unwrap()
    .lines()
    .filter_map(|l| serde_json::from_str::<Value>(l).ok())
    .filter(|v| v.get("text").is_some())
    .collect()
}
#[test]
fn every_adapter_preserves_fixture_prose() {
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
        fixture(name, &home);
        let data = t.path().join("data");
        let before = snapshot(&home);
        ok(cli(&home, &data, &["index"]));
        assert_eq!(snapshot(&home), before, "{name}: source files changed");
        let db =
            Connection::open_with_flags(data.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)
                .unwrap();
        let got: Vec<(String, String, i64, String)> = db
            .prepare("SELECT agent,session,turn,CAST(text AS TEXT) FROM prose WHERE who!='agent'")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let want = expected(name);
        assert_eq!(got.len(), want.len(), "fixture {name}: {got:?}");
        for v in want {
            assert!(
                got.contains(&(
                    v["agent"].as_str().unwrap().into(),
                    v["session"].as_str().unwrap().into(),
                    v["turn"].as_i64().unwrap(),
                    v["text"].as_str().unwrap().into()
                )),
                "{name}: missing {v}"
            );
        }
    }
}
fn transcript(home: &Path, texts: &[&str]) -> PathBuf {
    let p = home.join(".pi/agent/sessions/project/chat.jsonl");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    let mut lines=vec![serde_json::json!({"type":"session","id":"test-session","version":3,"cwd":"/work/project","timestamp":"2026-10-29T12:00:00Z"}).to_string()];
    for (i, text) in texts.iter().enumerate() {
        lines.push(serde_json::json!({"type":"message","id":format!("m{i}"),"parentId":if i==0{Value::Null}else{Value::String(format!("m{}",i-1))},"timestamp":"2026-10-29T12:00:01Z","message":{"role":"user","content":text}}).to_string());
    }
    fs::write(&p, lines.join("\n") + "\n").unwrap();
    p
}
#[test]
fn incremental_changes_and_last_good() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = transcript(&h, &["alpha marker"]);
    ok(cli(&h, &d, &["index"]));
    let db = Connection::open(d.join("index.db")).unwrap();
    let id: i64 = db
        .query_row("SELECT id FROM prose", [], |r| r.get(0))
        .unwrap();
    transcript(&h, &["alpha marker", "beta marker"]);
    ok(cli(&h, &d, &["index"]));
    assert_eq!(
        db.query_row("SELECT id FROM prose WHERE turn=0", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        id
    );
    transcript(&h, &["gamma marker"]);
    ok(cli(&h, &d, &["index"]));
    assert_eq!(cli(&h, &d, &["alpha", "-c"]).status.code(), Some(1));
    fs::write(&path, "{not JSON\n").unwrap();
    ok(cli(&h, &d, &["index"]));
    ok(cli(&h, &d, &["gamma", "-c"]));
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM sources WHERE last_error IS NOT NULL",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    let unchanged_error = ok(cli(&h, &d, &["index"]));
    let unchanged_error: Value = serde_json::from_slice(&unchanged_error.stdout).unwrap();
    assert_eq!(unchanged_error["parsed"], 0);
    fs::remove_file(path).unwrap();
    ok(cli(&h, &d, &["index"]));
    assert_eq!(cli(&h, &d, &["gamma", "-c"]).status.code(), Some(1));
}
#[test]
fn locks_and_wal_snapshots() {
    let t = TempDir::new().unwrap();
    let root = DataRoot::open(t.path().join("d"), true).unwrap();
    let w = store::writer(&root).unwrap();
    let lock = root.lock(true).unwrap().unwrap();
    assert!(root.lock(false).unwrap().is_none());
    let r = store::reader(&root).unwrap();
    assert_eq!(
        r.query_row("SELECT count(*) FROM state", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    w.execute_batch("BEGIN;INSERT INTO state VALUES('a','b');")
        .unwrap();
    assert_eq!(
        r.query_row("SELECT count(*) FROM state", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    w.execute_batch("COMMIT").unwrap();
    assert_eq!(
        r.query_row("SELECT count(*) FROM state", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    r.execute_batch("ROLLBACK;BEGIN").unwrap();
    assert_eq!(
        r.query_row("SELECT count(*) FROM state", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
    drop(lock);
    assert!(root.lock(false).unwrap().is_some());
}
#[test]
fn search_contracts() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(
        &h,
        &[
            "release_workflow café peakDetect",
            "workflow release don call",
            "this hi",
            "release release workflow",
        ],
    );
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(&h, &d, &["release workflow", "--json"]));
    let lines: Vec<Value> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(lines[0]["kind"], "agrep-meta");
    assert!(lines[1].get("matched").is_none());
    assert!(lines[1]["handle"]
        .as_str()
        .unwrap()
        .starts_with("@test-ses:"));
    assert_eq!(lines[1]["self"], false);
    assert!(!lines[1].as_object().unwrap().contains_key("completeness"));
    for (q, n) in [("akD", 1), ("CAFÉ", 1), ("hi", 1), ("don calls", 1)] {
        let o = ok(cli(&h, &d, &[q, "-c"]));
        assert_eq!(String::from_utf8(o.stdout).unwrap().trim(), n.to_string());
    }
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["hi", "-w", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["-E", "release.*workflow", "-c"])).stdout)
            .unwrap()
            .trim(),
        "2"
    );
    let punctuation_regex = ok(cli(&h, &d, &["-E", ".*", "-c"]));
    assert_eq!(
        String::from_utf8(punctuation_regex.stdout).unwrap().trim(),
        "4"
    );
    let top = ok(cli(&h, &d, &["release", "--json", "-n", "1"]));
    let top_text = String::from_utf8(top.stdout).unwrap();
    let meta: Value = serde_json::from_str(top_text.lines().next().unwrap()).unwrap();
    assert_eq!(meta["completeness"]["total_basis"], "floor");
    let o = ok(cli(&h, &d, &["release", "--flat", "-n", "1"]));
    assert_eq!(
        String::from_utf8(o.stdout)
            .unwrap()
            .trim()
            .split('\t')
            .count(),
        6
    );
    let o = cli(&h, &d, &["zz_missing", "--json"]);
    assert_eq!(o.status.code(), Some(1));
    assert_eq!(
        serde_json::from_slice::<Value>(&o.stdout).unwrap()["hits"],
        serde_json::json!([])
    );
    for args in [
        vec!["--no-auto", "search", "release", "-c"],
        vec!["--no-auto", "release", "-c"],
    ] {
        assert_eq!(
            String::from_utf8(ok(cli(&h, &d, &args)).stdout)
                .unwrap()
                .trim(),
            "3"
        );
    }
    let tiers = ok(cli(&h, &d, &["release", "--count-by-tier"]));
    let tiers = String::from_utf8(tiers.stdout).unwrap();
    assert_eq!(tiers.split_whitespace().count(), 5);
    assert!(tiers.starts_with("phrase_aligned="));
    assert!(tiers.ends_with("total=3\n"));
    let invalid = cli(&h, &d, &["release", "--unrecognized-flag"]);
    assert_eq!(invalid.status.code(), Some(2));
    assert!(invalid.stdout.is_empty());
    assert_eq!(
        String::from_utf8(invalid.stderr).unwrap().lines().count(),
        1
    );
    assert_eq!(cli(&h, &d, &["-E", "("]).status.code(), Some(2));
    assert_eq!(cli(&h, &d, &["recall", "q"]).status.code(), Some(1));
}
#[test]
fn format_refusal_and_no_auto() {
    let t = TempDir::new().unwrap();
    fs::write(t.path().join("unrelated"), "keep").unwrap();
    assert!(DataRoot::open(t.path(), true).is_err());
    assert_eq!(
        fs::read_to_string(t.path().join("unrelated")).unwrap(),
        "keep"
    );
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["old marker"]);
    ok(cli(&h, &d, &["index"]));
    transcript(&h, &["new marker"]);
    assert_eq!(
        cli(&h, &d, &["new", "-c", "--no-auto"]).status.code(),
        Some(2)
    );
    ok(cli(&h, &d, &["new", "-c"]));
}

#[test]
fn bounded_dense_page_has_exact_winners_without_exhausting_matches() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let text: Vec<_> = (0..2048)
        .map(|i| format!("deepold historical request {i:08}"))
        .collect();
    let refs: Vec<_> = text.iter().map(String::as_str).collect();
    transcript(&h, &refs);
    ok(cli(&h, &d, &["index"]));
    let page = ok(cli(&h, &d, &["deepold", "--json", "-n", "40", "--no-auto"]));
    let rows: Vec<Value> = String::from_utf8(page.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(rows.len(), 41);
    for (i, row) in rows[1..].iter().enumerate() {
        assert_eq!(row["turn"], i as i64);
    }
    assert!(measured_query(&d, "deepold", 40, None).examined < 2048);
    assert_eq!(rows[0]["completeness"]["total_basis"], "floor");
    let count = ok(cli(&h, &d, &["deepold", "-c", "--no-auto"]));
    assert_eq!(String::from_utf8(count.stdout).unwrap().trim(), "2048");
}
#[test]
fn reindex_and_full_preserve_rows_and_token_stores() {
    for name in ["pi", "cursor", "crush"] {
        let t = TempDir::new().unwrap();
        let h = t.path().join("h");
        let d = t.path().join("d");
        fixture(name, &h);
        ok(cli(&h, &d, &["index"]));
        let before = status_counts(cli(&h, &d, &["status", "--json", "--no-auto"]));
        ok(cli(&h, &d, &["index", "--full"]));
        assert_eq!(
            status_counts(cli(&h, &d, &["status", "--json", "--no-auto"])),
            before
        );
        ok(cli(&h, &d, &["reindex"]));
        assert_eq!(
            status_counts(cli(&h, &d, &["status", "--json", "--no-auto"])),
            before
        );
    }
}

#[test]
fn gemini_recorded_flows_keep_roles_replies_and_parents() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    fixture("gemini_flows", &h);
    ok(cli(&h, &d, &["index"]));
    let expected: Value = serde_json::from_slice(
        &fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/gemini_flows/expected.json"),
        )
        .unwrap(),
    )
    .unwrap();
    let db = Connection::open(d.join("index.db")).unwrap();
    for (session, case) in expected.as_object().unwrap() {
        let rows:Vec<Value>=db.prepare("SELECT p.who,CAST(p.text AS TEXT),COALESCE((SELECT CAST(a.text AS TEXT) FROM prose a WHERE a.session=p.session AND a.turn=p.turn AND a.who='agent'),'') FROM prose p WHERE p.session=? AND p.who!='agent' ORDER BY p.turn").unwrap().query_map([session],|r|Ok(serde_json::json!([r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?]))).unwrap().collect::<rusqlite::Result<_>>().unwrap();
        assert_eq!(serde_json::json!(rows), case["rows"], "{}", case["flow"]);
        if !rows.is_empty() {
            let parent: String = db
                .query_row("SELECT parent FROM sessions WHERE id=?", [session], |r| {
                    r.get(0)
                })
                .unwrap();
            assert_eq!(
                parent,
                case["parent"].as_str().unwrap_or(""),
                "{}",
                case["flow"]
            );
        }
    }
}
#[test]
fn opportunistic_second_writer_reads_old_complete_state() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["old marker"]);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let lock = root.lock(true).unwrap().unwrap();
    transcript(&h, &["new marker"]);
    let out = cli(&h, &d, &["new", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(out.stderr.is_empty());
    drop(lock);
    ok(cli(&h, &d, &["new", "-c"]));
}
#[test]
fn harness_policy_reclassifies_unchanged_sources() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["benchmark scenario"]);
    ok(cli(&h, &d, &["index"]));
    fs::write(d.join("harness_prefixes.txt"), "benchmark ").unwrap();
    ok(cli(&h, &d, &["index"]));
    let db = Connection::open(d.join("index.db")).unwrap();
    assert_eq!(
        db.query_row("SELECT who FROM prose", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "harness"
    );
}
#[test]
fn compact_content_fallback_is_labeled_and_plumbing_stays_strict() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["drafting moved into review"]);
    ok(cli(&h, &d, &["index"]));
    let out = Command::new(support::binary())
        .env_clear()
        .env("HOME", &h)
        .env("AGREP_HOME", &h)
        .env("AGREP_DATA_DIR", &d)
        .env("AGREP_PROFILE", "compact")
        .args(["how did drafting go last run", "--no-auto"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8(out.stdout).unwrap().contains("~all"));
    assert_eq!(
        cli(
            &h,
            &d,
            &["how did drafting go last run", "--json", "--no-auto"]
        )
        .status
        .code(),
        Some(2)
    );
}
#[test]
fn two_token_adapters_do_not_delete_each_others_unchanged_rows() {
    use std::io::{Seek, SeekFrom, Write};
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    fixture("cursor", &h);
    fixture("crush", &h);
    ok(cli(&h, &d, &["index"]));
    let before = status_counts(cli(&h, &d, &["status", "--json", "--no-auto"]));
    for path in [
        h.join(".config/Cursor/User/globalStorage/state.vscdb"),
        h.join(".local/share/crush/crush.db"),
    ] {
        let bytes = fs::read(&path).unwrap();
        let mut file = fs::OpenOptions::new().write(true).open(path).unwrap();
        file.seek(SeekFrom::Start(0)).unwrap();
        file.write_all(&bytes[..16]).unwrap();
        file.sync_all().unwrap();
    }
    ok(cli(&h, &d, &["index"]));
    assert_eq!(
        status_counts(cli(&h, &d, &["status", "--json", "--no-auto"])),
        before
    );
}
#[test]
fn tool_rows_round_trip_compression_and_have_event_spans() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    fixture("pi", &h);
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(
        &h,
        &d,
        &["backoff", "--who", "tool", "--json", "--no-auto"],
    ));
    let rows: Vec<Value> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert!(rows.len() > 1);
    let row = &rows[1];
    assert_eq!(row["who"], "tool");
    let handle = row["handle"].as_str().unwrap();
    let event = handle.split('~').nth(1).unwrap();
    assert_eq!(event.split(':').next().unwrap().len(), 24);
    assert!(row["snippet"].as_str().unwrap().contains("backoff"));
}
#[test]
fn phase_search_agrees_with_exhaustive_order_for_rows_and_chats() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let texts = [
        "alpha beta",
        "beta alpha",
        "alphabetab",
        "alpha___beta",
        "alphas betas",
        "alpha / beta",
        "alpha much farther away than expected beta",
    ];
    for i in 0..24 {
        let p = transcript(&h, &texts);
        let body = fs::read_to_string(&p)
            .unwrap()
            .replace("test-session", &format!("s{i:03}"));
        let dst = p.with_file_name(format!("s{i:03}.jsonl"));
        fs::write(&dst, body).unwrap();
        fs::remove_file(p).unwrap();
    }
    ok(cli(&h, &d, &["index"]));
    for extra in [false, true] {
        let mut a = vec!["alpha beta", "--json", "--no-auto", "-n", "0"];
        if extra {
            a.push("-l")
        }
        let all = ok(cli(&h, &d, &a));
        let all: Vec<Value> = String::from_utf8(all.stdout)
            .unwrap()
            .lines()
            .skip(1)
            .map(|s| serde_json::from_str(s).unwrap())
            .collect();
        let mut a = vec!["alpha beta", "--json", "--no-auto", "-n", "12"];
        if extra {
            a.push("-l")
        }
        let page = ok(cli(&h, &d, &a));
        let page: Vec<Value> = String::from_utf8(page.stdout)
            .unwrap()
            .lines()
            .skip(1)
            .map(|s| serde_json::from_str(s).unwrap())
            .collect();
        assert_eq!(
            page.iter().map(|v| &v["handle"]).collect::<Vec<_>>(),
            all.iter()
                .take(12)
                .map(|v| &v["handle"])
                .collect::<Vec<_>>()
        );
    }
}
#[test]
fn tool_bounds_do_not_hydrate_rows_below_the_prose_page() {
    use std::io::Write;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let texts = vec!["needle needle"; 40];
    let path = transcript(&h, &texts);
    let mut f = fs::OpenOptions::new().append(true).open(path).unwrap();
    let mut parent = "m39".to_string();
    for i in 0..600 {
        let call = format!("c{i}");
        let result = format!("r{i}");
        writeln!(f,"{}",serde_json::json!({"type":"message","id":call,"parentId":parent,"timestamp":"2026-10-30T12:00:00Z","message":{"role":"assistant","content":[{"type":"toolCall","id":result,"name":"shell","arguments":{"command":"needle"}}]}})).unwrap();
        writeln!(f,"{}",serde_json::json!({"type":"message","id":result,"parentId":call,"timestamp":"2026-10-30T12:00:01Z","message":{"role":"toolResult","toolCallId":result,"toolName":"shell","content":[{"type":"text","text":"needle ".repeat(50)}]}})).unwrap();
        parent = result;
    }
    drop(f);
    ok(cli(&h, &d, &["index"]));
    let output = ok(cli(&h, &d, &["needle", "--json", "--no-auto"]));
    let rows: Vec<Value> = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(rows.len(), 41);
    assert!(rows[1..].iter().all(|r| r["who"] == "user"));
    assert_eq!(measured_query(&d, "needle", 40, None).examined, 40);
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["needle", "-c", "--no-auto"])).stdout)
            .unwrap()
            .trim(),
        "640"
    );
}
#[test]
fn fts_block_limits_bound_sqlite_work_not_only_rendered_rows() {
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    let mut work = vec![];
    for n in [2048, 8192] {
        let t = TempDir::new().unwrap();
        let h = t.path().join("h");
        let d = t.path().join("d");
        let texts: Vec<_> = (0..n).map(|i| format!("deepold item {i:08}")).collect();
        transcript(&h, &texts.iter().map(String::as_str).collect::<Vec<_>>());
        ok(cli(&h, &d, &["index"]));
        let root = DataRoot::open(&d, false).unwrap();
        let c = store::reader(&root).unwrap();
        let ops = Arc::new(AtomicUsize::new(0));
        let tally = ops.clone();
        c.progress_handler(
            100,
            Some(move || {
                tally.fetch_add(100, Ordering::Relaxed);
                false
            }),
        );
        let result = agrep_core::query::search(
            &c,
            &agrep_core::query::Options {
                pattern: "deepold".into(),
                regex: false,
                word: false,
                sort: "score".into(),
                limit: 40,
                exact: false,
                count_only: false,
                count_by_tier: false,
                content: false,
                chats: false,
                tools: false,
                filters: Default::default(),
            },
        )
        .unwrap();
        assert_eq!(result.hits.len(), 40);
        work.push(ops.load(Ordering::Relaxed));
    }
    assert!(
        work[1] < work[0] * 2,
        "SQLite work grew with the full posting list: {work:?}"
    );
}

#[test]
fn failed_source_does_not_mark_or_reparse_its_valid_sibling() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let bad = transcript(&h, &["first marker"]);
    let good = bad.with_file_name("valid.jsonl");
    let body = fs::read_to_string(&bad)
        .unwrap()
        .replace("test-session", "valid-session");
    fs::write(&good, &body).unwrap();
    ok(cli(&h, &d, &["index"]));
    fs::write(&bad, "{broken JSON\n").unwrap();
    ok(cli(&h, &d, &["index"]));
    let c = Connection::open(d.join("index.db")).unwrap();
    let errors = || {
        c.query_row(
            "SELECT count(*) FROM sources WHERE last_error IS NOT NULL",
            [],
            |r| r.get::<_, i64>(0),
        )
        .unwrap()
    };
    assert_eq!(errors(), 1);
    let unchanged: Value = serde_json::from_slice(&ok(cli(&h, &d, &["index"])).stdout).unwrap();
    assert_eq!(unchanged["parsed"], 0);
    fs::write(&good, body.replace("first marker", "changed marker")).unwrap();
    let changed: Value = serde_json::from_slice(&ok(cli(&h, &d, &["index"])).stdout).unwrap();
    assert_eq!(changed["parsed"], 1);
    assert_eq!(errors(), 1);
    assert_eq!(cli(&h, &d, &["changed", "-c"]).status.code(), Some(0));
}

#[test]
fn native_replay_copies_merge_and_recover_when_a_copy_is_deleted() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let original = transcript(&h, &["alpha replay"]);
    let replay = original.with_file_name("replay.jsonl");
    fs::copy(&original, &replay).unwrap();
    ok(cli(&h, &d, &["index"]));
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["alpha", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
    let body = fs::read_to_string(&original).unwrap();
    let beta = serde_json::json!({"type":"message","id":"m1","parentId":"m0","timestamp":"2026-10-29T12:00:02Z","message":{"role":"user","content":"beta replay"}});
    fs::write(&replay, format!("{body}{beta}\n")).unwrap();
    ok(cli(&h, &d, &["index"]));
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["alpha", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["beta", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
    fs::remove_file(replay).unwrap();
    ok(cli(&h, &d, &["index"]));
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["alpha", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
    assert_eq!(cli(&h, &d, &["beta", "-c"]).status.code(), Some(1));
}

#[test]
fn replay_selects_complete_reply_and_deduplicates_tool_calls() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let original = transcript(&h, &["shared question"]);
    let base = fs::read_to_string(&original).unwrap();
    let call = serde_json::json!({"type":"message","id":"call","parentId":"m0","timestamp":"2026-10-29T12:00:02Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"done","name":"shell","arguments":{"command":"inspect"}}]}});
    let done = serde_json::json!({"type":"message","id":"done","parentId":"call","timestamp":"2026-10-29T12:00:03Z","message":{"role":"toolResult","toolCallId":"done","toolName":"shell","content":[{"type":"text","text":"replaypayload"}]}});
    let reply = |text: &str| serde_json::json!({"type":"message","id":"reply","parentId":"done","timestamp":"2026-10-29T12:00:04Z","message":{"role":"assistant","content":text}});
    fs::write(
        &original,
        format!("{base}{call}\n{done}\n{}\n", reply("shortreply")),
    )
    .unwrap();
    let replay = original.with_file_name("replay.jsonl");
    fs::write(
        &replay,
        format!(
            "{base}{call}\n{done}\n{}\n",
            reply("longreply with more complete evidence")
        ),
    )
    .unwrap();
    ok(cli(&h, &d, &["index"]));
    let status: Value =
        serde_json::from_slice(&ok(cli(&h, &d, &["status", "--json", "--no-auto"])).stdout)
            .unwrap();
    assert_eq!(status["prose"], 2);
    assert_eq!(status["tools"], 1);
    ok(cli(&h, &d, &["reindex"]));
    for term in ["shared", "longreply", "replaypayload"] {
        assert_eq!(
            String::from_utf8(ok(cli(&h, &d, &[term, "-c"])).stdout)
                .unwrap()
                .trim(),
            "1"
        );
    }
    assert_eq!(cli(&h, &d, &["shortreply", "-c"]).status.code(), Some(1));
    fs::write(
        &original,
        fs::read_to_string(&original)
            .unwrap()
            .replace("replaypayload", "updatedpayload"),
    )
    .unwrap();
    ok(cli(&h, &d, &["index"]));
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["updatedpayload", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
    assert_eq!(cli(&h, &d, &["replaypayload", "-c"]).status.code(), Some(1));
    fs::remove_file(replay).unwrap();
    ok(cli(&h, &d, &["index"]));
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["shortreply", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
    assert_eq!(
        String::from_utf8(ok(cli(&h, &d, &["updatedpayload", "-c"])).stdout)
            .unwrap()
            .trim(),
        "1"
    );
}
#[test]
fn replay_turn_collisions_receive_distinct_deterministic_positions() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let original = transcript(&h, &["alpha collision"]);
    let replay = original.with_file_name("replay.jsonl");
    fs::write(
        &replay,
        fs::read_to_string(&original)
            .unwrap()
            .replace("alpha collision", "beta collision"),
    )
    .unwrap();
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(
        &h,
        &d,
        &["collision", "--json", "--sort", "position", "-n", "0"],
    ));
    let rows: Vec<Value> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[1]["turn"], 0);
    assert_eq!(rows[2]["turn"], 1);
    assert_ne!(rows[1]["handle"], rows[2]["handle"]);
    fs::remove_file(original).unwrap();
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(&h, &d, &["collision", "--json"]));
    let rows: Vec<Value> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[1]["turn"], 0);
}

#[test]
fn compact_budget_includes_long_opaque_handles() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let original = transcript(&h, &["needle"]);
    let base = fs::read_to_string(&original).unwrap();
    for i in 0..4 {
        let id = format!("family-{i}-{}", "x".repeat(700));
        fs::write(
            original.with_file_name(format!("copy-{i}.jsonl")),
            base.replace("test-session", &id),
        )
        .unwrap();
    }
    fs::remove_file(original).unwrap();
    ok(cli(&h, &d, &["index"]));
    let out = Command::new(support::binary())
        .env_clear()
        .env("HOME", &h)
        .env("AGREP_HOME", &h)
        .env("AGREP_DATA_DIR", &d)
        .env("AGREP_PROFILE", "compact")
        .args(["needle"])
        .output()
        .unwrap();
    assert!(out.status.success());
    assert!(out.stdout.len() <= 3584, "{} bytes", out.stdout.len());
    assert!(String::from_utf8(out.stdout).unwrap().starts_with("@~"));
}

#[test]
fn match_only_counts_agree_with_exhaustive_ranked_results() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(
        &h,
        &[
            "alpha beta Straße cats",
            "beta distant alpha concatenate",
            "alphabeta hi λ",
            "nothing here",
        ],
    );
    ok(cli(&h, &d, &["index"]));
    for query in ["alpha beta", "STRASSE", "cats", "hi", "λ", "unmatchedz"] {
        let ranked = cli(&h, &d, &[query, "--json", "-n", "0"]);
        let text = String::from_utf8(ranked.stdout).unwrap();
        let meta: Value = serde_json::from_str(text.lines().next().unwrap()).unwrap();
        let counted = cli(&h, &d, &[query, "-c"]);
        let n: usize = String::from_utf8(counted.stdout)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert_eq!(
            n as u64,
            meta["completeness"]["total"].as_u64().unwrap(),
            "{query}"
        );
        assert_eq!(ranked.status.code(), counted.status.code(), "{query}");
    }
}

#[test]
fn a_short_term_keeps_the_other_terms_indexed() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let mut texts = vec!["ordinary history without the selective word"; 1000];
    texts.push("hi selectiveneedle");
    transcript(&h, &texts);
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(&h, &d, &["hi selectiveneedle", "--json", "-n", "0"]));
    let text = String::from_utf8(out.stdout).unwrap();
    let meta: Value = serde_json::from_str(text.lines().next().unwrap()).unwrap();
    assert_eq!(meta["completeness"]["total"], 1);
    assert_eq!(
        measured_query(&d, "hi selectiveneedle", 0, None).examined,
        1
    );
    assert_eq!(
        cli(&h, &d, &["go selectiveneedle", "-c"]).status.code(),
        Some(1)
    );
    use std::io::Write;
    let path = h.join(".pi/agent/sessions/project/chat.jsonl");
    let mut file = fs::OpenOptions::new().append(true).open(path).unwrap();
    let mut parent = "m1000".to_string();
    for i in 0..200 {
        let call = format!("call-{i}");
        let result = format!("result-{i}");
        writeln!(file,"{}",serde_json::json!({"type":"message","id":&call,"parentId":&parent,"timestamp":"2026-10-29T12:00:03Z","message":{"role":"assistant","content":[{"type":"toolCall","id":&result,"name":"shell","arguments":{"command":"inspect"}}]}})).unwrap();
        writeln!(file,"{}",serde_json::json!({"type":"message","id":&result,"parentId":&call,"timestamp":"2026-10-29T12:00:04Z","message":{"role":"toolResult","toolCallId":&result,"toolName":"shell","content":[{"type":"text","text":if i==199{"hi selectiveneedle"}else{"hi ordinary output"}}]}})).unwrap();
        parent = result;
    }
    drop(file);
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(
        &h,
        &d,
        &["hi selectiveneedle", "--who", "tool", "--json", "-n", "0"],
    ));
    let text = String::from_utf8(out.stdout).unwrap();
    let meta: Value = serde_json::from_str(text.lines().next().unwrap()).unwrap();
    assert_eq!(meta["completeness"]["total"], 1);
    assert_eq!(
        measured_query(&d, "hi selectiveneedle", 0, Some("tool")).examined,
        1
    );
}

#[test]
fn all_terms_page_rules_out_lower_weight_tool_blocks() {
    use std::io::Write;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let words = vec!["alpha separate beta"; 64];
    let path = transcript(&h, &words);
    let mut file = fs::OpenOptions::new().append(true).open(path).unwrap();
    let mut parent = "m63".to_string();
    for i in 0..768 {
        let call = format!("call-{i}");
        let result = format!("result-{i}");
        writeln!(file,"{}",serde_json::json!({"type":"message","id":&call,"parentId":&parent,"timestamp":"2026-10-29T12:00:03Z","message":{"role":"assistant","content":[{"type":"toolCall","id":&result,"name":"shell","arguments":{"command":"inspect"}}]}})).unwrap();
        writeln!(file,"{}",serde_json::json!({"type":"message","id":&result,"parentId":&call,"timestamp":"2026-10-29T12:00:04Z","message":{"role":"toolResult","toolCallId":&result,"toolName":"shell","content":[{"type":"text","text":"alpha separate beta"}]}})).unwrap();
        parent = result;
    }
    drop(file);
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(&h, &d, &["alpha beta", "--json", "-n", "40"]));
    let text = String::from_utf8(out.stdout).unwrap();
    let rows: Vec<Value> = text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 41);
    assert!(rows[1..].iter().all(|r| r["who"] == "user"));
    assert!(
        measured_query(&d, "alpha beta", 40, None).examined < 1000,
        "{}",
        rows[0]
    );
}

fn measured_query(
    data: &std::path::Path,
    pattern: &str,
    limit: usize,
    who: Option<&str>,
) -> agrep_core::query::Results {
    let root = agrep_core::data::DataRoot(data.into());
    let c = agrep_core::store::reader(&root).unwrap();
    agrep_core::query::search(
        &c,
        &agrep_core::query::Options {
            pattern: pattern.into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit,
            exact: limit == 0,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: true,
            filters: agrep_core::query::Filters {
                who: who.map(str::to_owned),
                ..Default::default()
            },
        },
    )
    .unwrap()
}
