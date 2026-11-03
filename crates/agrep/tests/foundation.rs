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
fn pending_sources(h: &Path, d: &Path) -> usize {
    serde_json::from_slice::<Value>(&ok(cli(h, d, &["__census"])).stdout).unwrap()
        ["changed_sources"]
        .as_u64()
        .unwrap() as usize
}
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
    ok(cli(&h, &d, &["index"]));
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
    let work = root.work_lock(true).unwrap().unwrap();
    transcript(&h, &["new marker"]);
    let out = cli(&h, &d, &["new", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(out.stderr.is_empty());
    assert_eq!(pending_sources(&h, &d), 1);
    drop(work);
    drop(lock);
    ok(cli(&h, &d, &["index"]));
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
    let mut work = [vec![], vec![]];
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
        for (index, pattern) in ["deepold", "deepold item"].into_iter().enumerate() {
            ops.store(0, Ordering::Relaxed);
            let result = agrep_core::query::search(
                &c,
                &agrep_core::query::Options {
                    pattern: pattern.into(),
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
            work[index].push(ops.load(Ordering::Relaxed));
        }
    }
    for (pattern, counts) in ["deepold", "deepold item"].into_iter().zip(work) {
        assert!(
            counts[1] < counts[0] * 2,
            "SQLite work grew with the full {pattern:?} posting list: {counts:?}"
        );
    }
}

#[test]
fn unselective_agent_keeps_the_bounded_prose_access_path() {
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    let mut work = Vec::new();
    for count in [8192, 32768] {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        let data = t.path().join("data");
        transcript(&home, &vec!["deepold"; count]);
        ok(cli(&home, &data, &["index"]));
        let c = store::reader(&DataRoot::open(&data, false).unwrap()).unwrap();
        let steps = Arc::new(AtomicUsize::new(0));
        let capture = steps.clone();
        c.progress_handler(
            100,
            Some(move || {
                capture.fetch_add(100, Ordering::Relaxed);
                false
            }),
        );
        let mut options = retrieval_options("deepold");
        options.limit = 40;
        options.filters.agent = Some("pi".into());
        let result = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(result.hits.len(), 40);
        assert_eq!(
            result.hits.iter().map(|h| h.turn).collect::<Vec<_>>(),
            (0..40).collect::<Vec<_>>()
        );
        work.push(steps.load(Ordering::Relaxed));
    }
    assert!(work[1] < work[0] * 2, "{work:?}");
}

#[test]
fn sparse_old_matches_ignore_the_recency_of_unrelated_rows() {
    let mut work = Vec::new();
    for n in [2048, 8192] {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        let data = t.path().join("data");
        let texts: Vec<_> = (0..n)
            .map(|i| match i % 9 {
                0 => format!("record {i} deepold evidence"),
                3 => format!("record {i} deepold's evidence"),
                6 => format!("record {i} prefixdeepoldsuffix evidence"),
                _ => format!("record {i} unrelated recent activity"),
            })
            .collect();
        let path = transcript(&home, &texts.iter().map(String::as_str).collect::<Vec<_>>());
        let now = chrono::Utc::now();
        let rewritten = fs::read_to_string(&path)
            .unwrap()
            .lines()
            .enumerate()
            .map(|(line, text)| {
                let mut value: Value = serde_json::from_str(text).unwrap();
                if line > 0 {
                    let i = line - 1;
                    let age = if i % 3 == 0 {
                        chrono::Duration::days(365)
                    } else {
                        chrono::Duration::days(1)
                    };
                    value["timestamp"] = serde_json::json!((now - age
                        + chrono::Duration::seconds(i as i64))
                    .to_rfc3339());
                }
                value.to_string()
            })
            .collect::<Vec<_>>()
            .join("\n");
        fs::write(&path, rewritten).unwrap();
        ok(cli(&home, &data, &["index"]));
        let root = DataRoot::open(&data, false).unwrap();
        let c = store::reader(&root).unwrap();
        let mut options = retrieval_options("deepold");
        options.limit = 40;
        let result = agrep_core::query::search(&c, &options).unwrap();
        let expected: Vec<_> = (0..n)
            .rev()
            .filter(|i| i % 9 == 0)
            .take(40)
            .map(|i| i as i64)
            .collect();
        assert_eq!(
            result.hits.iter().map(|hit| hit.turn).collect::<Vec<_>>(),
            expected
        );
        assert_eq!(result.total_basis, "floor");
        work.push(result.examined);
    }
    assert!(
        work[1] < work[0] * 2,
        "old matches inherited unrelated recency: {work:?}"
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

#[test]
fn reader_does_not_parse_changed_sources_and_work_lock_arbitrates_refresh() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["committed old text"]);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let guard = root.work_lock(true).unwrap().unwrap();
    transcript(&h, &["pending new text"]);
    ok(cli(&h, &d, &["committed", "--json"]));
    assert_eq!(pending_sources(&h, &d), 1);
    ok(cli(&h, &d, &["__work", "refresh"]));
    assert_eq!(
        cli(&h, &d, &["pending", "-c", "--no-auto"]).status.code(),
        Some(2)
    );
    drop(guard);
    ok(cli(&h, &d, &["__work", "refresh"]));
    ok(cli(&h, &d, &["pending", "-c", "--no-auto"]));
    assert_eq!(pending_sources(&h, &d), 0);
}
#[test]
fn positional_tool_count_does_not_hydrate_text() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    fixture("pi", &h);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    let o = agrep_core::query::Options {
        pattern: "backoff".into(),
        regex: false,
        word: false,
        sort: "score".into(),
        limit: 0,
        exact: true,
        count_only: true,
        count_by_tier: false,
        content: false,
        chats: false,
        tools: true,
        filters: agrep_core::query::Filters {
            who: Some("tool".into()),
            ..Default::default()
        },
    };
    let r = agrep_core::query::search(&c, &o).unwrap();
    assert!(r.total > 0);
    assert_eq!(r.examined, 0);
}

fn retrieval_options(pattern: &str) -> agrep_core::query::Options {
    agrep_core::query::Options {
        pattern: pattern.into(),
        regex: false,
        word: false,
        sort: "score".into(),
        limit: 10,
        exact: false,
        count_only: false,
        count_by_tier: false,
        content: false,
        chats: false,
        tools: false,
        filters: Default::default(),
    }
}
#[test]
fn caller_predicates_filter_the_window_and_mark_older_rows() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["needle old", "needle current"]);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut o = retrieval_options("needle");
    o.filters.caller = Some(agrep_core::query::CallerFilter {
        agent: "pi".into(),
        session: "test-session".into(),
        family: "test-session".into(),
        exclude_from_turn: Some(1),
        exclude_family: false,
        excluded_sessions: vec![],
    });
    let r = agrep_core::query::search(&c, &o).unwrap();
    assert_eq!(r.hits.len(), 1);
    assert_eq!(r.hits[0].turn, 0);
    assert!(r.hits[0].self_marker);
    o.filters.caller.as_mut().unwrap().exclude_family = true;
    assert_eq!(agrep_core::query::search(&c, &o).unwrap().total, 0);
}
#[test]
fn coverage_weights_rare_terms_and_stops_on_block_bounds() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let mut texts = vec!["common ordinary"; 1000];
    texts.extend(["selectiveword evidence"; 2]);
    transcript(&h, &texts);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut o = retrieval_options("how did common selectiveword absentword");
    o.limit = 1;
    o.content = true;
    let r = agrep_core::query::search(&c, &o).unwrap();
    assert!(r.hits[0].text.contains("selectiveword"));
    assert_eq!(r.hits[0].matched, "content-terms");
    assert!(r.examined < 1000);
    assert_eq!(r.total_basis, "floor");
}
#[test]
fn regex_literal_queries_have_admissible_early_termination() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = transcript(&h, &vec!["deepold historical request"; 2048]);
    fs::write(
        &path,
        fs::read_to_string(&path)
            .unwrap()
            .replace("2026-10-29", "2023-10-29"),
    )
    .unwrap();
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut o = retrieval_options("deepold");
    o.regex = true;
    let r = agrep_core::query::search(&c, &o).unwrap();
    assert_eq!(r.hits.len(), 10);
    assert!(r.examined < 2048);
    assert_eq!(r.hits[0].turn, 0);
}

fn canonical_rows(data: &Path) -> Vec<Value> {
    let root = DataRoot::open(data, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut rows = vec![];
    for table in ["prose", "tools"] {
        let mut stmt=c.prepare(&format!("SELECT agent,session,position,who,model,model_source,ts,text,extra FROM {table} WHERE visible=1 ORDER BY agent,session,position,who,extra")).unwrap();
        for row in stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, i64>(6)?,
                    r.get::<_, Vec<u8>>(7)?,
                    r.get::<_, String>(8)?,
                ))
            })
            .unwrap()
        {
            let (agent, session, turn, who, model, model_source, ts, bytes, extra) = row.unwrap();
            rows.push(serde_json::json!([
                table,
                agent,
                session,
                turn,
                who,
                model,
                model_source,
                ts,
                store::decode(table, bytes).unwrap(),
                extra
            ]));
        }
    }
    for (table, key) in [("events", "call_id"), ("reply_shapes", "turn")] {
        let mut q = c
            .prepare(&format!(
                "SELECT session,CAST({key} AS TEXT),body FROM {table} ORDER BY session,{key}"
            ))
            .unwrap();
        for row in q
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })
            .unwrap()
        {
            let (session, key, body) = row.unwrap();
            rows.push(serde_json::json!([
                table,
                session,
                key,
                serde_json::from_str::<Value>(&body).unwrap()
            ]));
        }
    }
    let mut q = c
        .prepare("SELECT token,n,s FROM term_stats ORDER BY token")
        .unwrap();
    for row in q
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, i64>(2)?,
            ))
        })
        .unwrap()
    {
        let (t, n, aligned) = row.unwrap();
        rows.push(serde_json::json!(["term_stats", t, n, aligned]));
    }
    rows
}
fn write_records(path: &Path, records: &[Value], append: bool) {
    use std::io::Write;
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .append(append)
        .truncate(!append)
        .open(path)
        .unwrap();
    for r in records {
        writeln!(f, "{r}").unwrap();
    }
}
#[test]
fn appended_jsonl_matches_full_parsing_for_each_high_churn_adapter() {
    use serde_json::json;
    for agent in ["pi", "claude", "codex"] {
        let t = TempDir::new().unwrap();
        let h = t.path().join("h");
        let d = t.path().join("d");
        let full = t.path().join("full");
        let ts = "2026-10-29T12:00:00Z";
        let (path, initial, tail) = match agent {
            "pi" => (
                h.join(".pi/agent/sessions/project/chat.jsonl"),
                vec![
                    json!({"type":"session","version":3,"id":"resume-pi","cwd":"/work/project"}),
                    json!({"type":"message","id":"u1","parentId":null,"timestamp":ts,"message":{"role":"user","content":"first prompt"}}),
                    json!({"type":"message","id":"a1","parentId":"u1","timestamp":ts,"message":{"role":"assistant","model":"model-one","content":[{"type":"text","text":"working"},{"type":"toolCall","id":"call1","name":"shell","arguments":{"command":"echo sample"}}]}}),
                ],
                vec![
                    json!({"type":"message","id":"r1","parentId":"a1","timestamp":ts,"message":{"role":"toolResult","toolCallId":"call1","content":"returned"}}),
                    json!({"type":"message","id":"a2","parentId":"r1","timestamp":ts,"message":{"role":"assistant","content":"done"}}),
                    json!({"type":"model_change","id":"mc","parentId":"a2","modelId":"model-two","timestamp":ts}),
                    json!({"type":"message","id":"u2","parentId":"mc","timestamp":ts,"message":{"role":"user","content":"second prompt"}}),
                ],
            ),
            "claude" => (
                h.join(".claude/projects/project/chat.jsonl"),
                vec![
                    json!({"type":"user","sessionId":"resume-claude","cwd":"/work/project","timestamp":ts,"message":{"role":"user","content":"first prompt"}}),
                    json!({"type":"assistant","sessionId":"resume-claude","timestamp":ts,"message":{"role":"assistant","model":"model-one","content":[{"type":"text","text":"working"},{"type":"tool_use","id":"call1","name":"Bash","input":{"command":"echo sample"}}]}}),
                ],
                vec![
                    json!({"type":"user","sessionId":"resume-claude","timestamp":ts,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"call1","content":"returned"}]}}),
                    json!({"type":"assistant","sessionId":"resume-claude","timestamp":ts,"message":{"role":"assistant","model":"model-two","content":"done"}}),
                    json!({"type":"user","sessionId":"resume-claude","cwd":"/work/project","timestamp":ts,"message":{"role":"user","content":"second prompt"}}),
                ],
            ),
            _ => (
                h.join(".codex/sessions/rollout-resume.jsonl"),
                vec![
                    json!({"type":"session_meta","timestamp":ts,"payload":{"id":"resume-codex","cwd":"/work/project"}}),
                    json!({"type":"event_msg","timestamp":ts,"payload":{"type":"user_message","message":"first prompt"}}),
                    json!({"type":"response_item","timestamp":ts,"payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"first prompt"}]}}),
                    json!({"type":"turn_context","timestamp":ts,"payload":{"model":"model-one"}}),
                    json!({"type":"response_item","timestamp":ts,"payload":{"type":"function_call","call_id":"call1","name":"shell","arguments":"{\"command\":\"echo sample\"}"}}),
                ],
                vec![
                    json!({"type":"response_item","timestamp":ts,"payload":{"type":"function_call_output","call_id":"call1","output":"returned"}}),
                    json!({"type":"response_item","timestamp":ts,"payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}}),
                    json!({"type":"turn_context","timestamp":ts,"payload":{"model":"model-two"}}),
                    json!({"type":"event_msg","timestamp":ts,"payload":{"type":"user_message","message":"second prompt"}}),
                    json!({"type":"response_item","timestamp":ts,"payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"second prompt"}]}}),
                ],
            ),
        };
        write_records(&path, &initial, false);
        ok(cli(&h, &d, &["index"]));
        for record in &tail {
            write_records(&path, std::slice::from_ref(record), true);
            let out = ok(cli(&h, &d, &["index"]));
            let info: Value = serde_json::from_slice(&out.stdout).unwrap();
            assert_eq!(info["resumed_sources"], 1, "{agent}: {info}");
            if full.exists() {
                fs::remove_dir_all(&full).unwrap();
            }
            ok(cli(&h, &full, &["index"]));
            assert_eq!(
                canonical_rows(&d),
                canonical_rows(&full),
                "{agent} after {record}"
            );
        }
    }
}
#[test]
fn pi_branch_switches_replay_persisted_nodes_and_match_full_parse() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let full = t.path().join("full");
    let path = transcript(
        &h,
        &["root prompt", "abandoned prompt", "restorable prompt"],
    );
    ok(cli(&h, &d, &["index"]));
    for (id, parent, text) in [
        ("b", "m0", "fork prompt"),
        ("c", "m2", "restored branch prompt"),
    ] {
        write_records(
            &path,
            &[
                json!({"type":"message","id":id,"parentId":parent,"timestamp":"2026-10-29T12:00:03Z","message":{"role":"user","content":text}}),
            ],
            true,
        );
        let out = ok(cli(&h, &d, &["index"]));
        let info: Value = serde_json::from_slice(&out.stdout).unwrap();
        assert!(info["bytes_read"].as_u64().unwrap() < 20_000);
        if full.exists() {
            fs::remove_dir_all(&full).unwrap();
        }
        ok(cli(&h, &full, &["index"]));
        assert_eq!(canonical_rows(&d), canonical_rows(&full));
    }
}

#[test]
fn abandoned_tool_text_is_shared_and_restored_without_source_reparse() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("home");
    let d = t.path().join("data");
    let full = t.path().join("full");
    let path = h.join(".pi/agent/sessions/test/branch.jsonl");
    let input = format!("inspect {}", "input ".repeat(60));
    let output = format!("\n{}\n", "retained tool evidence ".repeat(120));
    write_records(
        &path,
        &[
            json!({"type":"session","id":"branch-fixture","version":3,"cwd":"/work/fixture"}),
            json!({"type":"message","id":"root","parentId":null,"message":{"role":"user","content":"root prompt"}}),
            json!({"type":"message","id":"call","parentId":"root","message":{"role":"assistant","content":[{"type":"toolCall","id":"tool","name":"shell","arguments":{"command":input}}]}}),
            json!({"type":"message","id":"result","parentId":"call","message":{"role":"toolResult","toolCallId":"tool","content":output}}),
            json!({"type":"message","id":"tail","parentId":"result","message":{"role":"user","content":"original tail"}}),
        ],
        false,
    );
    ok(cli(&h, &d, &["index"]));
    let original = canonical_rows(&d);
    assert!(original
        .iter()
        .any(|r| r.to_string().contains("retained tool evidence")));
    for (id, parent, text) in [
        ("fork", "root", "new branch"),
        ("return", "result", "restored branch"),
    ] {
        write_records(
            &path,
            &[
                json!({"type":"message","id":id,"parentId":parent,"message":{"role":"user","content":text}}),
            ],
            true,
        );
        ok(cli(&h, &d, &["index"]));
        if full.exists() {
            fs::remove_dir_all(&full).unwrap();
        }
        ok(cli(&h, &full, &["index"]));
        assert_eq!(canonical_rows(&d), canonical_rows(&full));
        let expected = if id == "fork" { "0\n" } else { "1\n" };
        let counted = cli(&h, &d, &["retained tool evidence", "--who", "tool", "-c"]);
        assert_eq!(
            counted.status.code(),
            Some(if id == "fork" { 1 } else { 0 }),
            "{}",
            String::from_utf8_lossy(&counted.stderr)
        );
        assert_eq!(String::from_utf8(counted.stdout).unwrap(), expected);
    }
    for (id, parent, role, text) in [
        (
            "alternate",
            "call",
            "toolResult",
            "changed result".repeat(150),
        ),
        (
            "back",
            "result",
            "user",
            "back to the original result".into(),
        ),
    ] {
        let mut message = json!({"role":role,"content":text});
        if role == "toolResult" {
            message["toolCallId"] = json!("tool");
        }
        write_records(
            &path,
            &[json!({"type":"message","id":id,"parentId":parent,"message":message})],
            true,
        );
        ok(cli(&h, &d, &["index"]));
        fs::remove_dir_all(&full).unwrap();
        ok(cli(&h, &full, &["index"]));
        assert_eq!(canonical_rows(&d), canonical_rows(&full));
    }
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    assert_eq!(
        c.query_row("SELECT count(*) FROM tool_blobs", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        2
    );
    drop(c);
    write_records(
        &path,
        &[
            json!({"type":"session","id":"branch-fixture","version":3,"cwd":"/work/fixture"}),
            json!({"type":"message","id":"root","parentId":null,"message":{"role":"user","content":"rewritten prompt"}}),
        ],
        false,
    );
    ok(cli(&h, &d, &["index"]));
    let c = store::reader(&root).unwrap();
    assert_eq!(
        c.query_row("SELECT count(*) FROM tool_blobs", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn append_parsing_is_bounded_and_rewrites_are_detected() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = transcript(&h, &["initial prompt"]);
    let padding = "x".repeat(2048);
    let mut records = vec![];
    let mut parent = "m0".to_string();
    for i in 0..2048 {
        let id = format!("meta-{i}");
        records.push(json!({"type":"model_change","id":id,"parentId":parent,"modelId":"model-one","padding":padding}));
        parent = id;
    }
    write_records(&path, &records, true);
    assert!(fs::metadata(&path).unwrap().len() > 4_000_000);
    ok(cli(&h, &d, &["index"]));
    write_records(
        &path,
        &[
            json!({"type":"message","id":"tail","parentId":parent,"message":{"role":"user","content":"appended prompt"}}),
        ],
        true,
    );
    let info: Value = serde_json::from_slice(&ok(cli(&h, &d, &["index"])).stdout).unwrap();
    assert_eq!(info["resumed_sources"], 1);
    assert!(info["bytes_parsed"].as_u64().unwrap() < 20_000, "{info}");
    let mut bytes = fs::read(&path).unwrap();
    let middle = bytes.len() / 2;
    let at = bytes[middle..].iter().position(|b| *b == b'x').unwrap() + middle;
    bytes[at] = b'y';
    fs::write(&path, &bytes).unwrap();
    let info: Value = serde_json::from_slice(&ok(cli(&h, &d, &["index"])).stdout).unwrap();
    assert_eq!(info["resumed_sources"], 0);
    assert!(info["bytes_read"].as_u64().unwrap() >= bytes.len() as u64);
    let mut bytes = fs::read(&path).unwrap();
    let middle = bytes.len() / 2;
    let at = bytes[middle..].iter().position(|b| *b == b'x').unwrap() + middle;
    bytes[at] = b'z';
    fs::write(&path, &bytes).unwrap();
    write_records(
        &path,
        &[
            json!({"type":"model_change","id":"after-rewrite","parentId":"tail","modelId":"model-two"}),
        ],
        true,
    );
    let info: Value = serde_json::from_slice(&ok(cli(&h, &d, &["index"])).stdout).unwrap();
    assert_eq!(
        info["resumed_sources"], 0,
        "interior rewrite plus append must not pass edge guards"
    );
    transcript(&h, &["rewritten prompt"]);
    let info: Value = serde_json::from_slice(&ok(cli(&h, &d, &["index"])).stdout).unwrap();
    assert_eq!(info["resumed_sources"], 0);
    assert_eq!(
        canonical_rows(&d)
            .iter()
            .filter(|r| r[0] == "prose" && r[4] == "user")
            .count(),
        1
    );
}

#[test]
fn accelerated_sha256_preserves_content_identity() {
    assert_eq!(
        store::digest("abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}

#[test]
fn unicode_only_blocks_keep_a_conservative_retrieval_bound() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("home");
    let d = t.path().join("data");
    let texts = vec!["界界界"; 700];
    transcript(&h, &texts);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut options = retrieval_options("界界界");
    options.limit = 40;
    let r = agrep_core::query::search(&c, &options).unwrap();
    assert_eq!(r.hits.len(), 40);
    assert!(r.blocks > 0);
    options.limit = 0;
    options.exact = true;
    options.count_only = true;
    assert_eq!(agrep_core::query::search(&c, &options).unwrap().total, 700);
}

#[test]
fn word_edges_and_metadata_filters_preserve_literal_semantics() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["c++ needle", "xc++ needle", "c++x needle"]);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let writer = store::writer(&root).unwrap();
    writer
        .execute("UPDATE prose SET model='GPT_5%' WHERE turn=0", [])
        .unwrap();
    writer
        .execute("UPDATE prose SET model='GPTx5Z' WHERE turn>0", [])
        .unwrap();
    drop(writer);
    let c = store::reader(&root).unwrap();
    let mut o = retrieval_options("c++");
    o.word = true;
    let r = agrep_core::query::search(&c, &o).unwrap();
    assert_eq!(r.hits.len(), 1);
    assert_eq!(r.hits[0].turn, 0);
    o = retrieval_options("needle");
    o.filters.model = Some("gpt_5%".into());
    assert_eq!(agrep_core::query::search(&c, &o).unwrap().hits.len(), 1);
    o.filters.model_soft = true;
    o.filters.model = Some("_5%".into());
    assert_eq!(agrep_core::query::search(&c, &o).unwrap().hits.len(), 1);
    o.filters.model = None;
    o.filters.chat = Some("test_".into());
    assert_eq!(agrep_core::query::search(&c, &o).unwrap().total, 0);
    o.filters.chat = Some("TEST-".into());
    assert_eq!(agrep_core::query::search(&c, &o).unwrap().hits.len(), 3);
}

#[test]
fn pi_checkpoints_keep_parser_inputs_without_native_payload_copies() {
    use agrep_core::ingest::pi::compact_record;
    use serde_json::json;
    let raw = json!({
        "type":"message","id":"r1","parentId":"c1","timestamp":"2026-10-29T12:00:00Z",
        "opaque_trace":"x".repeat(100_000),
        "message":{"role":"toolResult","toolCallId":"call1","isError":false,
            "content":"output".repeat(1000),"details":{"unused":"x".repeat(100_000)}}
    });
    let compact = compact_record(raw);
    assert!(compact.to_string().len() < 2000);
    assert_eq!(compact["message"]["__agrep_chars"], 6000);
    assert_eq!(compact["message"]["__agrep_bytes"], 6000);
    assert_eq!(compact_record(compact.clone()), compact);
    let call = compact_record(json!({"type":"message","id":"c1","parentId":null,
        "message":{"role":"assistant","content":[{"type":"toolCall","id":"call1","name":"shell",
            "arguments":{"command":"echo kept","opaque_trace":"x".repeat(100_000)}}]}}));
    assert!(call.to_string().len() < 1000);
    assert_eq!(call["message"]["content"][0]["__agrep_input"], "echo kept");
    assert_eq!(compact_record(call.clone()), call);
}

#[test]
fn tool_dictionary_preserves_substrings_and_verifies_cross_token_literals() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = h.join(".pi/agent/sessions/test/sample.jsonl");
    let mut rows = vec![
        json!({"type":"session","id":"dictionary-test","version":3,"cwd":"/work/project"}),
        json!({"type":"message","id":"user","parentId":null,"message":{"role":"user","content":"prompt"}}),
    ];
    let mut parent = "user".to_string();
    for (i, output) in [
        "terror syntaxerror",
        "errorCode",
        "err rro ror",
        "foo.bar",
        "foo x bar",
        "déjà café",
        "abc000ddef",
    ]
    .iter()
    .enumerate()
    {
        let call = format!("call{i}");
        let result = format!("result{i}");
        rows.push(json!({"type":"message","id":&call,"parentId":parent,"message":{"role":"assistant","content":[{"type":"toolCall","name":"shell","id":&call,"arguments":{}}]}}));
        rows.push(json!({"type":"message","id":&result,"parentId":&call,"message":{"role":"toolResult","toolCallId":call,"content":output}}));
        parent = result;
    }
    write_records(&path, &rows, false);
    ok(cli(&h, &d, &["index"]));
    for (pattern, expected, indexed) in [
        ("error", 2, true),
        ("foo.bar", 1, false),
        ("café", 1, false),
        ("000d", 1, true),
        ("foo bar", 2, true),
    ] {
        let root = DataRoot::open(&d, false).unwrap();
        let c = store::reader(&root).unwrap();
        let mut o = retrieval_options(pattern);
        o.tools = true;
        o.filters.who = Some("tool".into());
        o.exact = true;
        o.limit = 0;
        o.count_only = true;
        let r = agrep_core::query::search(&c, &o).unwrap();
        assert_eq!(r.total, expected, "{pattern}");
        if indexed {
            assert_eq!(r.examined, 0, "{pattern}");
        }
        o.count_only = false;
        assert_eq!(
            agrep_core::query::search(&c, &o).unwrap().total,
            expected,
            "{pattern}"
        );
    }
    write_records(
        &path,
        &[
            json!({"type":"message","id":"lastcall","parentId":parent,"message":{"role":"assistant","content":[{"type":"toolCall","name":"shell","id":"lastcall","arguments":{}}]}}),
            json!({"type":"message","id":"lastresult","parentId":"lastcall","message":{"role":"toolResult","toolCallId":"lastcall","content":"newlexicalentry"}}),
        ],
        true,
    );
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(
        &h,
        &d,
        &["lexical", "--who", "tool", "-c", "--no-auto"],
    ));
    assert_eq!(String::from_utf8(out.stdout).unwrap().trim(), "1");
}

#[test]
fn metadata_only_jsonl_change_reuses_verified_cursor() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let p = transcript(&h, &["unchanged content"]);
    ok(cli(&h, &d, &["index"]));
    let bytes = fs::read(&p).unwrap();
    fs::write(&p, bytes).unwrap();
    let info: Value = serde_json::from_slice(&ok(cli(&h, &d, &["index"])).stdout).unwrap();
    assert_eq!(info["resumed_sources"], 1);
    assert_eq!(info["bytes_parsed"], 0);
    assert_eq!(info["changed_rows"], 0);
}

#[test]
fn selective_tool_stream_keeps_exact_winners_across_tiers_and_folded_filters() {
    use serde_json::json;
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let path = home.join(".pi/agent/sessions/test/selective.jsonl");
    let mut rows =
        vec![json!({"type":"session","id":"selective-tool-stream","version":3,"cwd":"/work/test"})];
    let mut parent = Value::Null;
    for i in 0..8192 {
        let ts = if i < 512 {
            "2026-10-29T12:00:00Z"
        } else {
            "2023-01-01T12:00:00Z"
        };
        let call = format!("call-{i}");
        let result = format!("result-{i}");
        rows.push(json!({"type":"message","id":call,"parentId":parent,"timestamp":ts,"message":{"role":"assistant","content":[{"type":"toolCall","id":call,"name":"shell","arguments":{"command":"inspect"}}]}}));
        rows.push(json!({"type":"message","id":result,"parentId":call,"timestamp":ts,"message":{"role":"toolResult","toolCallId":call,"content":if i%16==0 {format!("needle uncommon result {i}")}else{format!("ordinary result {i}")}}}));
        parent = json!(result);
    }
    write_records(&path, &rows, false);
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let c = store::reader(&root).unwrap();
    for pattern in [
        "needle uncommon",
        "uncommon needle",
        "ordinary result",
        "result ordinary",
    ] {
        let mut options = agrep_core::query::Options {
            pattern: pattern.into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 20,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: true,
            filters: agrep_core::query::Filters {
                agent: Some(" PI ".into()),
                who: Some(" Tool ".into()),
                ..Default::default()
            },
        };
        let fast = agrep_core::query::search(&c, &options).unwrap();
        options.exact = true;
        let all = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(fast.hits.len(), 20);
        assert_eq!(
            fast.hits.iter().map(|h| &h.handle).collect::<Vec<_>>(),
            all.hits.iter().map(|h| &h.handle).collect::<Vec<_>>()
        );
        assert!(fast.examined < all.examined);
    }
}

#[test]
fn initial_single_source_metadata_uses_visible_rows_and_first_prompt() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let data = t.path().join("data");
    let path = home.join(".pi/agent/sessions/test/metadata.jsonl");
    write_records(
        &path,
        &[
            json!({"type":"session","id":"metadata-session","version":3,"cwd":"/work/test"}),
            json!({"type":"message","id":"a","parentId":null,"timestamp":"2026-10-28T00:00:00Z","message":{"role":"assistant","content":"orphan reply"}}),
            json!({"type":"message","id":"b","parentId":"a","timestamp":"2026-10-29T00:00:00Z","message":{"role":"user","content":"first prompt\nbody"}}),
            json!({"type":"message","id":"c","parentId":"b","timestamp":"2026-10-30T00:00:00Z","message":{"role":"user","content":"later prompt"}}),
        ],
        false,
    );
    ok(cli(&home, &data, &["index"]));
    let c = Connection::open(data.join("index.db")).unwrap();
    let got: (i64, i64, String) = c
        .query_row(
            "SELECT first_ts,last_ts,title FROM sessions WHERE id='metadata-session'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(
        got.0,
        chrono::DateTime::parse_from_rfc3339("2026-10-29T00:00:00Z")
            .unwrap()
            .timestamp_millis()
    );
    assert_eq!(
        got.1,
        chrono::DateTime::parse_from_rfc3339("2026-10-30T00:00:00Z")
            .unwrap()
            .timestamp_millis()
    );
    assert_eq!(got.2, "first prompt");
}

#[test]
fn no_meta_uses_proven_event_lineage_and_retains_only_evidence() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let data = t.path().join("data");
    let path = home.join(".pi/agent/sessions/test/meta.jsonl");
    write_records(
        &path,
        &[
            json!({"type":"session","id":"meta-session","version":3,"cwd":"/work/test"}),
            json!({"type":"message","id":"u","parentId":null,"message":{"role":"user","content":"needle lived question"}}),
            json!({"type":"message","id":"c","parentId":"u","message":{"role":"assistant","content":[{"type":"toolCall","id":"call","name":"shell","arguments":{"command":"agrep search needle"}}]}}),
            json!({"type":"message","id":"r","parentId":"c","message":{"role":"toolResult","toolCallId":"call","content":"needle from history"}}),
            json!({"type":"message","id":"a","parentId":"r","message":{"role":"assistant","content":"needle history echo"}}),
            json!({"type":"message","id":"u2","parentId":"a","message":{"role":"user","content":"Request interrupted by user"}}),
        ],
        false,
    );
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut o = retrieval_options("needle");
    o.tools = true;
    o.filters.no_meta = true;
    let result = agrep_core::query::search(&c, &o).unwrap();
    assert_eq!(result.hits.len(), 1);
    assert_eq!(result.hits[0].who, "user");
    assert!(!result.hits[0].meta_marker);
    o.filters.who = Some("agent".into());
    let result = agrep_core::query::search(&c, &o).unwrap();
    assert_eq!(result.hits.len(), 1);
    assert!(result.hits[0].meta_marker);
    o.pattern = "interrupted".into();
    o.filters.who = None;
    let result = agrep_core::query::search(&c, &o).unwrap();
    assert_eq!(result.hits[0].who, "control");
    assert!(!result.hits[0].meta_marker);
    o.count_only = true;
    assert!(agrep_core::query::search(&c, &o).is_err());
}

#[test]
fn no_meta_filters_before_the_page_limit() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let data = t.path().join("data");
    let path = home.join(".pi/agent/sessions/test/meta-page.jsonl");
    let mut records =
        vec![json!({"type":"session","id":"meta-page","version":3,"cwd":"/work/test"})];
    let mut parent = String::new();
    for i in 0..81 {
        let call = format!("call-{i}");
        let result = format!("result-{i}");
        let (time, command, output) = if i < 80 {
            (
                "2099-01-01T00:00:00Z",
                "agrep search 'needle marker'",
                "needle marker from history",
            )
        } else {
            (
                "2024-01-01T00:00:00Z",
                "printf 'needle marker'",
                "needle marker ordinary evidence",
            )
        };
        records.push(json!({"type":"message","id":call,"parentId":parent,"timestamp":time,"message":{"role":"assistant","content":[{"type":"toolCall","id":call,"name":"shell","arguments":{"command":command}}]}}));
        records.push(json!({"type":"message","id":result,"parentId":call,"timestamp":time,"message":{"role":"toolResult","toolCallId":call,"content":output}}));
        parent = result;
    }
    write_records(&path, &records, false);
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut o = retrieval_options("needle marker");
    o.tools = true;
    o.limit = 1;
    o.filters.who = Some("tool".into());
    let unfiltered = agrep_core::query::search(&c, &o).unwrap();
    assert!(unfiltered.hits[0].text.contains("agrep search"));
    o.filters.no_meta = true;
    let result = agrep_core::query::search(&c, &o).unwrap();
    assert_eq!(result.hits.len(), 1);
    assert!(result.hits[0].text.contains("ordinary evidence"));
    assert!(!result.hits[0].meta_marker);
}

#[test]
fn explicit_coverage_is_separate_and_identifiers_keep_grep_semantics() {
    let t = TempDir::new().unwrap();
    let home = t.path().join("h");
    let data = t.path().join("d");
    transcript(&home, &["specific useful context", "other history"]);
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let c = store::reader(&root).unwrap();
    let o = retrieval_options("specific useful missing");
    assert!(agrep_core::query::search(&c, &o).unwrap().hits.is_empty());
    let recovered = agrep_core::query::coverage(&c, &o).unwrap();
    assert!(!recovered.hits.is_empty());
    assert!(recovered.hits.iter().all(|h| h.matched == "content-terms"));
    let mut o = retrieval_options("specific_useful_missing");
    o.content = true;
    assert!(agrep_core::query::search(&c, &o).unwrap().hits.is_empty());
    let output = cli(&home, &data, &["--coverage", "specific useful missing"]);
    assert_ne!(output.status.code(), Some(2));
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .contains("coverage lane (forced)"));
}

#[test]
fn a_full_prose_page_cannot_stop_tools_for_time_or_position_sort() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let data = t.path().join("data");
    let base = home.join(".pi/agent/sessions/test");
    write_records(
        &base.join("prose.jsonl"),
        &[
            json!({"type":"session","id":"zzz","version":3,"cwd":"/work/test"}),
            json!({"type":"message","id":"p","timestamp":"2024-01-01T00:00:00Z","message":{"role":"user","content":"needle needle needle"}}),
        ],
        false,
    );
    write_records(
        &base.join("tool.jsonl"),
        &[
            json!({"type":"session","id":"aaa","version":3,"cwd":"/work/test"}),
            json!({"type":"message","id":"p","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"unrelated"}}),
            json!({"type":"message","id":"c","parentId":"p","timestamp":"2025-01-01T00:00:01Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"c","name":"shell","arguments":{"command":"inspect"}}]}}),
            json!({"type":"message","id":"r","parentId":"c","timestamp":"2025-01-01T00:00:02Z","message":{"role":"toolResult","toolCallId":"c","content":"needle"}}),
        ],
        false,
    );
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut options = retrieval_options("needle");
    options.tools = true;
    options.limit = 1;
    let ranked = agrep_core::query::search(&c, &options).unwrap();
    assert_eq!(ranked.hits[0].who, "user");
    assert!(ranked.hits[0].score > agrep_core::query::weight("tool"));
    for sort in ["time", "position"] {
        options.sort = sort.into();
        let result = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(result.hits[0].who, "tool", "{sort}");
    }
}

#[test]
fn all_terms_bounds_stop_at_attainable_ascii_scores_and_keep_unicode_winners() {
    for count in [2048, 8192] {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        let data = t.path().join("data");
        transcript(&home, &vec!["beta office"; count]);
        ok(cli(&home, &data, &["index"]));
        let root = DataRoot::open(&data, false).unwrap();
        let c = store::reader(&root).unwrap();
        let mut options = retrieval_options("office beta");
        options.limit = 40;
        let fast = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(fast.hits.len(), 40);
        assert!(fast.examined <= 512, "{}", fast.examined);
        options.exact = true;
        let full = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(
            fast.hits.iter().map(|h| &h.handle).collect::<Vec<_>>(),
            full.hits.iter().map(|h| &h.handle).collect::<Vec<_>>()
        );
        drop(c);
        let mut texts = vec!["beta office"; count];
        texts[count - 1] = "beta oﬃce";
        transcript(&home, &texts);
        ok(cli(&home, &data, &["index"]));
        let c = store::reader(&root).unwrap();
        options.exact = false;
        let fast = agrep_core::query::search(&c, &options).unwrap();
        options.exact = true;
        let full = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(
            fast.hits.iter().map(|h| &h.handle).collect::<Vec<_>>(),
            full.hits.iter().map(|h| &h.handle).collect::<Vec<_>>()
        );
    }
}

#[test]
fn coverage_bounds_use_terms_in_the_same_row_and_preserve_unicode() {
    let t = TempDir::new().unwrap();
    let home = t.path().join("home");
    let data = t.path().join("data");
    let mut texts: Vec<_> = (0..8192)
        .map(|i| {
            if i % 2 == 0 {
                "amber common"
            } else {
                "gamma common"
            }
        })
        .collect();
    texts[0] = "ａｍｂｅｒ common";
    transcript(&home, &texts);
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let c = store::reader(&root).unwrap();
    for pattern in ["how amber gamma", "how amber gamma common"] {
        let mut options = retrieval_options(pattern);
        options.content = true;
        options.limit = 40;
        let fast = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(fast.hits.len(), 40);
        assert!(fast.examined <= 1024, "{pattern}: {}", fast.examined);
        options.exact = true;
        let full = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(
            fast.hits.iter().map(|h| &h.handle).collect::<Vec<_>>(),
            full.hits.iter().map(|h| &h.handle).collect::<Vec<_>>()
        );
    }
}

#[test]
fn appended_block_bounds_equal_a_complete_rebuild() {
    fn bounds(root: &DataRoot) -> Vec<Vec<Vec<rusqlite::types::Value>>> {
        let c = store::reader(root).unwrap();
        [
            "SELECT * FROM blocks ORDER BY id",
            "SELECT * FROM block_grams ORDER BY lane,block",
            "SELECT * FROM gram_postings ORDER BY lane,gram",
        ]
        .into_iter()
        .map(|sql| {
            let mut q = c.prepare(sql).unwrap();
            let columns = q.column_count();
            q.query_map([], |r| (0..columns).map(|i| r.get(i)).collect())
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap()
        })
        .collect()
    }
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let path = transcript(&home, &vec!["old marker"; 600]);
    ok(cli(&home, &data, &["index"]));
    write_records(
        &path,
        &[
            serde_json::json!({"type":"message","id":"m600","parentId":"m599","timestamp":"2099-01-01T00:00:00Z","message":{"role":"user","content":"new needle needle βeta"}}),
        ],
        true,
    );
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let incremental = bounds(&root);
    store::rebuild_bounds(&root).unwrap();
    assert_eq!(incremental, bounds(&root));
    // A metadata-only edit of an old row must invalidate the append merge too.
    let text = fs::read_to_string(&path)
        .unwrap()
        .replace("2026-10-29T12:00:01Z", "2099-02-01T00:00:00Z");
    fs::write(&path, text).unwrap();
    ok(cli(&home, &data, &["index"]));
    let incremental = bounds(&root);
    store::rebuild_bounds(&root).unwrap();
    assert_eq!(incremental, bounds(&root));
}

#[test]
fn old_single_occurrences_use_the_block_ceiling_before_hydration() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let path = transcript(&home, &vec!["deepold explanation"; 8192]);
    let text = fs::read_to_string(&path)
        .unwrap()
        .replace("2026-10-29", "2024-01-01");
    fs::write(path, text).unwrap();
    ok(cli(&home, &data, &["index"]));
    let root = DataRoot::open(&data, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut options = retrieval_options("deepold");
    options.limit = 40;
    let fast = agrep_core::query::search(&c, &options).unwrap();
    assert!(fast.examined <= 50, "{}", fast.examined);
    options.exact = true;
    let full = agrep_core::query::search(&c, &options).unwrap();
    assert_eq!(
        fast.hits.iter().map(|h| &h.handle).collect::<Vec<_>>(),
        full.hits.iter().map(|h| &h.handle).collect::<Vec<_>>()
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
