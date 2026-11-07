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
#[track_caller]
fn ok(out: Output) -> Output {
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    out
}
/// Runs `index` and returns the statistics it reports as an `AGREP_QUERY_TIMING` event.
fn indexed(home: &Path, data: &Path) -> Value {
    let out = ok(Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_PROFILE", "classic")
        .env("AGREP_QUERY_TIMING", "1")
        .arg("index")
        .output()
        .expect("built agrep executable is required"));
    String::from_utf8_lossy(&out.stderr)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find_map(|event| event.get("index").cloned())
        .expect("index statistics event")
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
    let unchanged_error = indexed(&h, &d);
    assert_eq!(unchanged_error["parsed"], 0);
    let other = path.parent().unwrap().join("other.jsonl");
    fs::write(&other, format!("{}\n{}\n", serde_json::json!({"type":"session","id":"other","version":3,"cwd":"/work/project"}), serde_json::json!({"type":"message","id":"m0","parentId":null,"message":{"role":"user","content":"unrelated prompt"}}))).unwrap();
    let other_refresh = indexed(&h, &d);
    assert_eq!(other_refresh["parsed"], 1);
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM sources WHERE last_error IS NOT NULL",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    ok(cli(&h, &d, &["gamma", "-c"]));
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
        vec!["search", "--no-auto", "release", "-c"],
        vec!["--no-auto", "release", "-c"],
    ] {
        assert_eq!(
            String::from_utf8(ok(cli(&h, &d, &args)).stdout)
                .unwrap()
                .trim(),
            "3"
        );
    }
    // As in 0.4.9, a leading option starts a bare search, so a command word after it is
    // searched for: "search release" occurs nowhere.
    let leading = cli(&h, &d, &["--no-auto", "search", "release", "-c"]);
    assert_eq!(leading.status.code(), Some(2));
    assert_eq!(String::from_utf8(leading.stdout).unwrap(), "0\n");
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
fn no_auto_reads_the_committed_state_while_another_writer_holds_the_lock() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["old marker"]);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let lock = root.lock(true).unwrap().unwrap();
    let work = root.work_lock(true).unwrap().unwrap();
    transcript(&h, &["new marker"]);
    let out = cli(&h, &d, &["new", "--json", "--no-auto"]);
    assert_eq!(out.status.code(), Some(2));
    let meta: Value =
        serde_json::from_str(String::from_utf8_lossy(&out.stdout).lines().next().unwrap()).unwrap();
    assert_eq!(meta["freshness"]["checked"], false);
    assert_eq!(pending_sources(&h, &d), 1);
    drop(work);
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
// A source whose directory became a file is gone for the census, so the refresh that the
// census asks for removes it, and the census then has nothing left to ask for.
#[test]
fn a_source_under_a_directory_turned_file_is_removed_by_the_refresh() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    fixture("pi", &h);
    ok(cli(&h, &d, &["index"]));
    assert_eq!(pending_sources(&h, &d), 0);
    let dir = h.join(".pi/agent/sessions/-work-legacy");
    let source = dir.join("2025-11-01T00-00-00-000Z_01920000-0000-7000-8000-000000000002.jsonl");
    fs::remove_dir_all(&dir).unwrap();
    fs::write(&dir, b"not a directory").unwrap();
    assert_eq!(pending_sources(&h, &d), 1);
    ok(cli(&h, &d, &["index"]));
    assert_eq!(pending_sources(&h, &d), 0);
    let status: Value =
        serde_json::from_slice(&ok(cli(&h, &d, &["status", "--json", "--no-auto"])).stdout)
            .unwrap();
    assert!(!status["sources"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s["path"].as_str().map(Path::new) == Some(source.as_path())));
}
// A directory the census cannot list is recorded with its error, and once it lists again
// that record names no source: the census asks for a refresh, and the refresh drops it.
#[test]
fn a_directory_error_clears_once_the_directory_reads_again() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    fixture("pi", &h);
    ok(cli(&h, &d, &["index"]));
    let dir = h.join(".pi/agent/sessions/-work-alpha");
    // The record a refresh writes for a directory its census could not list (the shape a
    // `chmod 000` on the sessions directory leaves for each session directory).
    store::writer(&DataRoot(d.clone()))
        .unwrap()
        .execute(
            "INSERT INTO sources VALUES(?1,'pi',0,0,x'02',?2)",
            rusqlite::params![
                dir.to_string_lossy(),
                format!("source parse incomplete: {}", dir.display())
            ],
        )
        .unwrap();
    let errors = || -> Vec<Value> {
        let status: Value =
            serde_json::from_slice(&ok(cli(&h, &d, &["status", "--json", "--no-auto"])).stdout)
                .unwrap();
        status["sources"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|s| !s["last_error"].is_null())
            .cloned()
            .collect()
    };
    assert_eq!(errors().len(), 1);
    assert_eq!(pending_sources(&h, &d), 1);
    ok(cli(&h, &d, &["index"]));
    assert_eq!(pending_sources(&h, &d), 0);
    assert!(errors().is_empty());
    assert!(
        String::from_utf8_lossy(&ok(cli(&h, &d, &["doctor"])).stdout)
            .starts_with("doctor: checks passed\n")
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

/// A pi chat in its own directory: `users` matching user turns, then `tools` matching
/// shell results.
fn needle_chat(home: &Path, id: &str, users: usize, tools: usize) {
    let p = home.join(".pi/agent/sessions").join(id).join("chat.jsonl");
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    let mut lines = vec![
        serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/project","timestamp":"2026-10-29T12:00:00Z"}),
    ];
    let mut parent = Value::Null;
    let mut push = |message: Value, id: String| {
        lines.push(serde_json::json!({"type":"message","id":id,"parentId":parent,"timestamp":"2026-10-29T12:00:01Z","message":message}));
        parent = Value::String(id);
    };
    for i in 0..users {
        push(
            serde_json::json!({"role":"user","content":format!("needle row {i}")}),
            format!("u{i}"),
        );
    }
    for i in 0..tools {
        push(
            serde_json::json!({"role":"assistant","content":[{"type":"toolCall","id":format!("t{i}"),"name":"shell","arguments":{"command":"ls"}}]}),
            format!("c{i}"),
        );
        push(
            serde_json::json!({"role":"toolResult","toolCallId":format!("t{i}"),"toolName":"shell","content":[{"type":"text","text":format!("needle output {i}")}]}),
            format!("r{i}"),
        );
    }
    let body: Vec<String> = lines.iter().map(Value::to_string).collect();
    fs::write(&p, body.join("\n") + "\n").unwrap();
}
// A chat listing counts every listed chat's matching rows. Counting each chat by
// scanning the pattern's matches across the corpus made the work grow with the listed
// chats times the corpus; the counts must cost the same however much else matches.
#[test]
fn chat_listing_counts_follow_the_listed_chats_not_the_corpus() {
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    let listed: Vec<String> = (0..6).map(|i| format!("listed-chat-{i}")).collect();
    let mut work = Vec::new();
    for filler in [2_000, 8_000] {
        let t = TempDir::new().unwrap();
        let (home, data) = (t.path().join("home"), t.path().join("data"));
        for id in &listed {
            needle_chat(&home, id, 5, 2);
        }
        needle_chat(&home, "filler-chat", filler, filler / 4);
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
        let mut options = retrieval_options("needle");
        options.tools = true;
        let counts = agrep_core::query::session_counts(
            &c,
            &options,
            &listed,
            &mut agrep_core::query::Plans::default(),
        )
        .unwrap();
        assert_eq!(counts, vec![7; listed.len()]);
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
    let unchanged = indexed(&h, &d);
    assert_eq!(unchanged["parsed"], 0);
    fs::write(&good, body.replace("first marker", "changed marker")).unwrap();
    let changed = indexed(&h, &d);
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

fn append_tool_results(path: &Path, parent: &str, from: usize, texts: &[String]) -> String {
    use std::io::Write;
    let mut file = fs::OpenOptions::new().append(true).open(path).unwrap();
    let mut parent = parent.to_string();
    for (i, text) in texts.iter().enumerate() {
        let n = from + i;
        let call = format!("call-{n}");
        let result = format!("result-{n}");
        // Spread over two weeks so block recency ceilings order the plan.
        let ts = chrono::DateTime::from_timestamp(1_793_000_000 + (n as i64 * 7919) % 1_209_600, 0)
            .unwrap()
            .to_rfc3339();
        writeln!(file,"{}",serde_json::json!({"type":"message","id":&call,"parentId":&parent,"timestamp":&ts,"message":{"role":"assistant","content":[{"type":"toolCall","id":&result,"name":"shell","arguments":{"command":"inspect"}}]}})).unwrap();
        writeln!(file,"{}",serde_json::json!({"type":"message","id":&result,"parentId":&call,"timestamp":&ts,"message":{"role":"toolResult","toolCallId":&result,"toolName":"shell","content":[{"type":"text","text":text}]}})).unwrap();
        parent = result;
    }
    parent
}
fn handles(h: &Path, d: &Path, args: &[&str]) -> (Value, Vec<Value>) {
    let out = ok(cli(h, d, args));
    let mut rows: Vec<Value> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let meta = rows.remove(0);
    (meta, rows.iter().map(|r| r["handle"].clone()).collect())
}
#[test]
fn tool_block_word_plans_keep_exhaustive_pages() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = transcript(&h, &["release workflow notes", "how did the release go"]);
    // Words containing the terms differ by block: some blocks hold only one
    // term, only concatenations, or Unicode separators the regex accepts.
    let shapes = [
        "prerelease workflows staged",
        "unreleased_workflowid pending",
        "releaseworkflow joined",
        "release\u{2192}workflow arrow",
        "release\u{e000}workflow private",
        "workflow then release reversed",
        "how did the release go today",
        "plain filler without either",
    ];
    let texts: Vec<String> = (0..1700)
        .map(|i| {
            let block = i / 400;
            let shape = match block {
                0 => i % 3,
                1 => 3 + i % 3,
                2 => 6 + i % 2,
                _ => i % 8,
            };
            if i % 5 == 4 {
                format!("{} {i}", shapes[7])
            } else {
                format!("{} {i}", shapes[shape])
            }
        })
        .collect();
    let parent = append_tool_results(&path, "m1", 0, &texts);
    ok(cli(&h, &d, &["index"]));
    let check = |label: &str| {
        for pattern in [
            "release workflow",
            "workflow release",
            "elease orkflo",
            "how did the release go",
            "unreleased workflowid",
        ] {
            for extra in [&[][..], &["--who", "tool"][..], &["-l"][..]] {
                let mut all = vec![pattern, "--json", "--no-auto", "-n", "0"];
                all.extend(extra);
                let (meta, every) = handles(&h, &d, &all);
                for n in ["1", "7", "40"] {
                    let mut page = vec![pattern, "--json", "--no-auto", "-n", n];
                    page.extend(extra);
                    let (_, shown) = handles(&h, &d, &page);
                    let want: Vec<_> = every.iter().take(n.parse().unwrap()).cloned().collect();
                    assert_eq!(shown, want, "{label} {pattern} {extra:?} -n {n} {meta}");
                }
            }
        }
    };
    check("first build");
    let more: Vec<String> = (0..300)
        .map(|i| format!("{} late{i}", shapes[(i % 7) as usize]))
        .collect();
    append_tool_results(&path, &parent, 1700, &more);
    ok(cli(&h, &d, &["index"]));
    check("append");
}

#[test]
fn reader_refreshes_sources_without_waiting_for_the_maintenance_worker() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    transcript(&h, &["committed old text"]);
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let guard = root.work_lock(true).unwrap().unwrap();
    transcript(&h, &["replacement new text"]);
    ok(cli(&h, &d, &["committed", "--json", "--no-auto"]));
    assert_eq!(pending_sources(&h, &d), 1);
    ok(cli(&h, &d, &["replacement", "--json"]));
    assert_eq!(pending_sources(&h, &d), 0);
    drop(guard);
    transcript(&h, &["subsequent new text"]);
    ok(cli(&h, &d, &["__work"]));
    assert_eq!(
        pending_sources(&h, &d),
        1,
        "maintenance never ingests sources"
    );
    ok(cli(&h, &d, &["subsequent", "-c"]));
    assert_eq!(pending_sources(&h, &d), 0);
}
/// The index's fingerprint of every file source, by path.
fn source_fingerprints(data: &Path) -> std::collections::BTreeMap<String, (i64, i64, Vec<u8>)> {
    let c = Connection::open_with_flags(data.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)
        .unwrap();
    let mut q = c
        .prepare("SELECT path,size,mtime_ns,change_token FROM sources WHERE path NOT LIKE '@%' AND path NOT LIKE 'token:%'")
        .unwrap();
    q.query_map([], |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?))))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}
#[test]
fn the_census_counts_exactly_the_sources_a_full_build_would_change() {
    use std::io::Write;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let session = |path: &Path, id: &str, text: &str| {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, format!("{}\n{}\n", serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/p"}), serde_json::json!({"type":"message","id":"m0","parentId":null,"timestamp":"2026-10-29T12:00:01Z","message":{"role":"user","content":text}}))).unwrap();
    };
    let pi = h.join(".pi/agent/sessions");
    for (path, id) in [
        ("p1/a.jsonl", "a"),
        ("p1/b.jsonl", "b"),
        ("p1/a/side.jsonl", "a-side"),
        ("p2/x.jsonl", "x"),
        ("p2/y.jsonl", "y"),
    ] {
        session(&pi.join(path), id, &format!("{id} first prompt"));
    }
    // More sources than one stat task takes, so their stats split across the pool.
    for i in 0..300 {
        session(
            &pi.join(format!("flat/s{i:03}.jsonl")),
            &format!("s{i}"),
            &format!("flat prompt {i}"),
        );
    }
    ok(cli(&h, &d, &["index"]));
    type Change<'a> = Box<dyn Fn() + 'a>;
    let steps: Vec<(&str, Change)> = vec![
        ("unchanged", Box::new(|| {})),
        (
            "append",
            Box::new(|| {
                let mut f = fs::OpenOptions::new()
                    .append(true)
                    .open(pi.join("p1/a.jsonl"))
                    .unwrap();
                writeln!(f, "{}", serde_json::json!({"type":"message","id":"m1","parentId":"m0","timestamp":"2026-10-29T12:00:02Z","message":{"role":"user","content":"appended"}})).unwrap();
            }),
        ),
        (
            "same bytes rewritten",
            Box::new(|| {
                let p = pi.join("p1/b.jsonl");
                fs::write(&p, fs::read(&p).unwrap()).unwrap();
            }),
        ),
        (
            "new file",
            Box::new(|| session(&pi.join("p1/c.jsonl"), "c", "c prompt")),
        ),
        (
            "new directory",
            Box::new(|| session(&pi.join("p2/x/side.jsonl"), "x-side", "side prompt")),
        ),
        (
            "removed file",
            Box::new(|| fs::remove_file(pi.join("p2/y.jsonl")).unwrap()),
        ),
        (
            "removed directory",
            Box::new(|| fs::remove_dir_all(pi.join("p1/a")).unwrap()),
        ),
        (
            "not a source",
            Box::new(|| fs::write(pi.join("p2/notes.md"), "notes").unwrap()),
        ),
        (
            "append in a large directory",
            Box::new(|| {
                let mut f = fs::OpenOptions::new()
                    .append(true)
                    .open(pi.join("flat/s150.jsonl"))
                    .unwrap();
                writeln!(f, "{}", serde_json::json!({"type":"message","id":"m1","parentId":"m0","timestamp":"2026-10-29T12:00:02Z","message":{"role":"user","content":"appended"}})).unwrap();
            }),
        ),
        (
            "removed from a large directory",
            Box::new(|| fs::remove_file(pi.join("flat/s007.jsonl")).unwrap()),
        ),
        (
            "new in a large directory",
            Box::new(|| session(&pi.join("flat/s300.jsonl"), "s300", "flat prompt 300")),
        ),
    ];
    for (n, (name, change)) in steps.iter().enumerate() {
        change();
        let full = t.path().join(format!("full-{n}"));
        ok(cli(&h, &full, &["index"]));
        let (indexed, current) = (source_fingerprints(&d), source_fingerprints(&full));
        let differing = indexed
            .keys()
            .chain(current.keys())
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .filter(|p| indexed.get(*p) != current.get(*p))
            .count();
        assert!(n == 0 || differing > 0, "{name} changed no fingerprint");
        assert_eq!(pending_sources(&h, &d), differing, "{name}");
    }
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
// Exact counts under the caller's exclusion and session scopes start from the excluded
// or scoped sessions' rows; a tiered count verifies every candidate row instead, so the
// two must agree in each lane.
#[test]
fn session_scoped_counts_match_verified_counts() {
    use agrep_core::query::{CallerFilter, Filters, SessionRows};
    let t = TempDir::new().unwrap();
    let (home, data) = (t.path().join("home"), t.path().join("data"));
    for (id, users, tools) in [
        ("caller-chat", 6, 3),
        ("caller-chat-side", 4, 2),
        ("other-chat", 5, 4),
    ] {
        needle_chat(&home, id, users, tools);
    }
    ok(cli(&home, &data, &["index"]));
    let c = store::reader(&DataRoot::open(&data, false).unwrap()).unwrap();
    let caller = |from: Option<i64>, sessions: &[&str], family: bool| Filters {
        caller: Some(CallerFilter {
            agent: "pi".into(),
            session: "caller-chat".into(),
            family: "caller-chat".into(),
            exclude_from_turn: from,
            exclude_family: family,
            excluded_sessions: sessions.iter().map(|s| s.to_string()).collect(),
        }),
        ..Filters::default()
    };
    let scopes = [
        caller(Some(3), &[], false),
        caller(None, &["caller-chat", "caller-chat-side"], false),
        caller(Some(0), &["caller-chat-side"], false),
        caller(None, &[], true),
        caller(None, &[], false),
        Filters {
            chat: Some("caller-chat".into()),
            ..Filters::default()
        },
        Filters {
            session_rows: Some(SessionRows {
                session: "caller-chat".into(),
                from: Some(2),
            }),
            ..Filters::default()
        },
    ];
    let mut totals = Vec::new();
    for filters in scopes {
        let mut o = retrieval_options("needle");
        (o.tools, o.limit, o.exact, o.count_only) = (true, 0, true, true);
        o.filters = filters;
        let quick = agrep_core::query::search(&c, &o).unwrap();
        o.count_by_tier = true;
        let verified = agrep_core::query::search(&c, &o).unwrap();
        assert_eq!(
            (quick.total, quick.tool_rows),
            (verified.total, verified.tool_rows),
            "{:?}",
            o.filters
        );
        totals.push(quick.total);
    }
    assert_eq!(totals, [18, 9, 9, 15, 24, 15, 7]);
}
/// One pi transcript at `path`: user turns, `recap:` compactions and `tool:` results,
/// each at the given second.
fn pi_turns(path: &Path, id: &str, turns: &[(&str, i64)]) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let at = |s: i64| {
        chrono::DateTime::from_timestamp(1_793_000_000 + s, 0)
            .unwrap()
            .to_rfc3339()
    };
    let mut lines = vec![serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/project","timestamp":at(0)}).to_string()];
    let mut parent = Value::Null;
    for (n, (text, s)) in turns.iter().enumerate() {
        let entry = format!("e{n}");
        if let Some(summary) = text.strip_prefix("recap:") {
            lines.push(serde_json::json!({"type":"compaction","id":entry,"parentId":parent,"timestamp":at(*s),"summary":summary,"tokensBefore":1000,"firstKeptEntryId":"e0"}).to_string());
        } else if let Some(output) = text.strip_prefix("tool:") {
            let call = format!("c{n}");
            lines.push(serde_json::json!({"type":"message","id":call,"parentId":parent,"timestamp":at(*s),"message":{"role":"assistant","content":[{"type":"toolCall","id":entry,"name":"bash","arguments":{"command":"run"}}]}}).to_string());
            lines.push(serde_json::json!({"type":"message","id":entry,"parentId":call,"timestamp":at(*s),"message":{"role":"toolResult","toolCallId":entry,"toolName":"bash","content":[{"type":"text","text":output}]}}).to_string());
        } else {
            lines.push(serde_json::json!({"type":"message","id":entry,"parentId":parent,"timestamp":at(*s),"message":{"role":"user","content":text}}).to_string());
        }
        parent = Value::String(entry);
    }
    fs::write(path, lines.join("\n") + "\n").unwrap();
}
fn resolved(c: &Connection, session: &str) -> agrep_core::caller::Caller {
    let caller = agrep_core::caller::Caller::resolve(
        c,
        agrep_core::caller::Identity {
            session: Some(session.into()),
            reason: "pi".into(),
            agent_context: true,
            published_sessions: vec![],
        },
    )
    .unwrap();
    assert!(caller.resolved);
    caller
}
/// The rows 0.4.9 counted as withheld: each probed session's `--chat <session>` query,
/// then that session's rows the policy withholds, without tooling roles under --no-meta.
fn chat_probe_counts(
    c: &Connection,
    o: &agrep_core::query::Options,
    caller: &agrep_core::caller::Caller,
    no_self: bool,
) -> agrep_core::caller::Withheld {
    let window = (!no_self).then(|| caller.boundary.unwrap_or(0));
    let members = if no_self {
        &caller.members
    } else {
        &caller.live_members
    };
    let mut sessions: std::collections::BTreeSet<_> = members.iter().cloned().collect();
    sessions.insert(caller.session.clone());
    let mut counted = agrep_core::caller::Withheld::default();
    for probed in sessions {
        let from = window.filter(|_| probed == caller.session);
        let mut q = o.clone();
        (q.limit, q.exact, q.chats, q.sort) = (0, true, false, "time".into());
        q.filters.caller = None;
        q.filters.no_meta = false;
        q.filters.chat = Some(probed.clone());
        q.filters.agent = Some(caller.agent.clone());
        for hit in agrep_core::query::search(c, &q).unwrap().hits {
            let tooling = matches!(hit.who.as_str(), "harness" | "synthetic")
                || (hit.who == "subagent" && hit.turn == 0);
            if hit.session != probed
                || from.is_some_and(|from| hit.turn < from)
                || (o.filters.no_meta && tooling)
            {
                continue;
            }
            if hit.who == "tool" {
                counted.tools += 1;
            } else {
                counted.prose += 1;
            }
        }
    }
    counted
}
// 0.4.9 ran each probe as a `--chat <session>` query and kept the session's rows the
// policy withholds.
#[test]
fn self_exclusion_counts_match_0_4_9_chat_probes() {
    use agrep_core::caller::Request;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let work = h.join(".pi/agent/sessions/work");
    // Turns before the recap are newer than the window's first turns, and a tool
    // result before it matches too.
    pi_turns(
        &work.join("2026-01-01T00-00-00-000Z_caller-one.jsonl"),
        "caller-one",
        &[
            ("flaky windows cache first", 100),
            ("tool:windows cache tool output before", 400),
            ("drafting notes before", 150),
            ("recap:summary of earlier work", 200),
            ("flaky windows cache in window", 210),
            ("tool:backoff windows cache window tool", 220),
            ("drafting continued in the window", 230),
            ("cache now in the window", 240),
        ],
    );
    pi_turns(
        &work.join("2026-01-01T00-00-00-000Z_caller-one/side.jsonl"),
        "caller-one-side",
        &[("flaky windows cache side", 300), ("drafting side", 310)],
    );
    // An id that another one extends, whose extension alone holds the exact phrase.
    pi_turns(
        &work.join("2026-01-02T00-00-00-000Z_plain.jsonl"),
        "plain",
        &[("drafting plain row", 500), ("windows cache plain", 510)],
    );
    pi_turns(
        &work.join("2026-01-03T00-00-00-000Z_plain-ext.jsonl"),
        "plain-ext",
        &[("how did the drafting runs", 50)],
    );
    pi_turns(
        &work.join("2026-01-04T00-00-00-000Z_other.jsonl"),
        "other",
        &[("flaky windows cache other", 250), ("drafting other", 260)],
    );
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut checked = 0;
    for session in ["caller-one", "plain"] {
        let caller = resolved(&c, session);
        for no_self in [false, true] {
            let request = Request {
                no_self,
                ..Request::default()
            };
            let policy = caller.policy(request).unwrap();
            for pattern in [
                "windows cache",
                "flaky windows cache",
                "how did the drafting runs",
                "backoff",
            ] {
                for (content, tools, no_meta) in [
                    (false, false, false),
                    (true, false, false),
                    (true, true, false),
                    (false, true, true),
                    (true, true, true),
                ] {
                    let mut o = retrieval_options(pattern);
                    (o.content, o.tools) = (content, tools);
                    o.filters.no_meta = no_meta;
                    let o = agrep_core::caller::options(&o, &caller, false, no_self);
                    let want = chat_probe_counts(&c, &o, &caller, no_self);
                    assert_eq!(
                        policy.withheld(&c, &o, None).unwrap(),
                        Some(want),
                        "{session} no_self={no_self} {pattern} {content} {tools} {no_meta}"
                    );
                    checked += usize::from(want.total() > 0);
                }
            }
        }
    }
    assert!(checked >= 20, "{checked}");
}
// Each probe reads the withheld session's rows through its index, not every row of the
// caller's agent newer than the window.
#[test]
fn self_exclusion_probes_do_not_read_other_sessions() {
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    let mut work = Vec::new();
    for n in [1024, 4096] {
        let t = TempDir::new().unwrap();
        let h = t.path().join("h");
        let d = t.path().join("d");
        let sessions = h.join(".pi/agent/sessions/work");
        pi_turns(
            &sessions.join("2026-01-01T00-00-00-000Z_caller-one.jsonl"),
            "caller-one",
            &[
                ("needle before", 100),
                ("recap:summary", 200),
                ("needle in window", 210),
                ("tool:needle tool in window", 220),
            ],
        );
        let others: Vec<(String, i64)> = (0..n)
            .map(|i| (format!("needle other {i}"), 300 + i as i64))
            .collect();
        let others: Vec<(&str, i64)> = others.iter().map(|(t, s)| (t.as_str(), *s)).collect();
        pi_turns(
            &sessions.join("2026-01-02T00-00-00-000Z_other.jsonl"),
            "other",
            &others,
        );
        ok(cli(&h, &d, &["index"]));
        let c = store::reader(&DataRoot::open(&d, false).unwrap()).unwrap();
        let caller = resolved(&c, "caller-one");
        let policy = caller
            .policy(agrep_core::caller::Request::default())
            .unwrap();
        let mut o = retrieval_options("needle");
        o.tools = true;
        let probes: Vec<_> = [false, true]
            .into_iter()
            .map(|content| {
                o.content = content;
                let options = agrep_core::caller::options(&o, &caller, false, false);
                let want = chat_probe_counts(&c, &options, &caller, false);
                assert!(want.prose > 0 && want.tools > 0, "{want:?}");
                (options, want)
            })
            .collect();
        let steps = Arc::new(AtomicUsize::new(0));
        let capture = steps.clone();
        c.progress_handler(
            100,
            Some(move || {
                capture.fetch_add(100, Ordering::Relaxed);
                false
            }),
        );
        for (options, want) in probes {
            assert_eq!(policy.withheld(&c, &options, None).unwrap(), Some(want));
        }
        work.push(steps.load(Ordering::Relaxed));
    }
    assert!(work[1] < work[0] * 2, "{work:?}");
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
            let info = indexed(&h, &d);
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
        use std::io::Write;
        fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"{malformed before branch\n")
            .unwrap();
        write_records(
            &path,
            &[
                json!({"type":"message","id":id,"parentId":parent,"timestamp":"2026-10-29T12:00:03Z","message":{"role":"user","content":text}}),
            ],
            true,
        );
        fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"{malformed after branch\n")
            .unwrap();
        let info = indexed(&h, &d);
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
fn append_parsing_is_bounded_and_boundary_rewrites_are_detected() {
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
    let info = indexed(&h, &d);
    assert_eq!(info["resumed_sources"], 1);
    assert!(info["bytes_parsed"].as_u64().unwrap() < 20_000, "{info}");
    assert!(
        info["bytes_read"].as_u64().unwrap()
            <= info["bytes_parsed"].as_u64().unwrap() + 2 * 64 * 1024,
        "{info}"
    );
    let mut bytes = fs::read(&path).unwrap();
    let at = bytes[..64 * 1024].iter().position(|b| *b == b'x').unwrap();
    bytes[at] = b'y';
    fs::write(&path, &bytes).unwrap();
    let info = indexed(&h, &d);
    assert_eq!(info["resumed_sources"], 0);
    assert!(info["bytes_read"].as_u64().unwrap() >= bytes.len() as u64);
    let mut bytes = fs::read(&path).unwrap();
    let last = (bytes.len() - 1) / (64 * 1024) * (64 * 1024);
    let at = bytes[last..].iter().position(|b| *b == b'x').unwrap() + last;
    bytes[at] = b'z';
    fs::write(&path, &bytes).unwrap();
    write_records(
        &path,
        &[
            json!({"type":"model_change","id":"after-rewrite","parentId":"tail","modelId":"model-two"}),
        ],
        true,
    );
    let info = indexed(&h, &d);
    assert_eq!(
        info["resumed_sources"], 0,
        "last-chunk rewrite plus append must not pass edge guards"
    );
    transcript(&h, &["rewritten prompt"]);
    let info = indexed(&h, &d);
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
fn chat_prefixes_match_every_row_of_their_sessions_in_both_lanes() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    // Ids that extend each other, differ in case and hold LIKE wildcards.
    let ids = [
        "alpha-one",
        "alpha-one-extended",
        "ALPHA-two",
        "al_pha%x",
        "beta",
    ];
    for (i, id) in ids.iter().enumerate() {
        let path = h.join(format!(".pi/agent/sessions/project/chat-{i}.jsonl"));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let mut lines = vec![serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/project","timestamp":"2026-10-29T12:00:00Z"}).to_string()];
        for n in 0..3 {
            lines.push(serde_json::json!({"type":"message","id":format!("m{n}"),"parentId":if n==0{Value::Null}else{Value::String(format!("m{}",n-1))},"timestamp":"2026-10-29T12:00:01Z","message":{"role":"user","content":format!("needle shared {id} {n}")}}).to_string());
        }
        fs::write(&path, lines.join("\n") + "\n").unwrap();
        let texts: Vec<String> = (0..4).map(|n| format!("needle tool {id} {n}")).collect();
        append_tool_results(&path, "m2", 0, &texts);
    }
    ok(cli(&h, &d, &["index"]));
    let root = DataRoot::open(&d, false).unwrap();
    let c = store::reader(&root).unwrap();
    let key = |hit: &agrep_core::query::Hit| (hit.session.clone(), hit.turn, hit.who.clone());
    for who in [None, Some("tool")] {
        let mut o = retrieval_options("needle");
        (o.limit, o.exact, o.tools, o.sort) = (0, true, true, "time".into());
        o.filters.who = who.map(Into::into);
        let every = agrep_core::query::search(&c, &o).unwrap().hits;
        assert_eq!(every.len(), if who.is_some() { 20 } else { 35 });
        for prefix in [
            "alpha-one",
            "ALPHA",
            "alpha-one-",
            "al_",
            "al_pha%",
            "a",
            "BETA",
            "beta-",
            "%",
        ] {
            o.filters.chat = Some(prefix.into());
            let found = agrep_core::query::search(&c, &o).unwrap();
            let want: Vec<_> = every
                .iter()
                .filter(|hit| {
                    hit.session
                        .to_ascii_lowercase()
                        .starts_with(&prefix.to_ascii_lowercase())
                })
                .map(key)
                .collect();
            assert_eq!(
                found.hits.iter().map(key).collect::<Vec<_>>(),
                want,
                "{prefix} {who:?}"
            );
            assert_eq!(found.total, want.len(), "{prefix} {who:?}");
            o.count_only = true;
            assert_eq!(agrep_core::query::search(&c, &o).unwrap().total, want.len());
            o.count_only = false;
        }
    }
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
    let info = indexed(&h, &d);
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
    // A chat is indexed through its turns, so the calls follow one.
    rows.push(json!({"type":"message","id":"u0","parentId":null,"timestamp":"2023-01-01T12:00:00Z","message":{"role":"user","content":[{"type":"text","text":"inspect the stream"}]}}));
    let mut parent = json!("u0");
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
fn selective_scope_decodes_only_rows_the_text_index_admits() {
    use serde_json::json;
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let path = home.join(".pi/agent/sessions/test/scope.jsonl");
    let mut rows =
        vec![json!({"type":"session","id":"selective-scope","version":3,"cwd":"/work/test"})];
    let mut parent = Value::Null;
    for i in 0..3072 {
        if i % 48 == 0 {
            let prompt = format!("prompt-{i}");
            let text = if i % 96 == 0 {
                format!("needle uncommon prompt {i}")
            } else {
                format!("plain prompt {i}")
            };
            rows.push(json!({"type":"message","id":prompt,"parentId":parent,"message":{"role":"user","content":text}}));
            parent = json!(prompt);
        }
        let call = format!("call-{i}");
        let result = format!("result-{i}");
        let output = match i % 101 {
            0 => format!("needle uncommon result {i}"),
            50 => format!("uncommon then needle {i}"),
            _ => format!("ordinary result {i}"),
        };
        rows.push(json!({"type":"message","id":call,"parentId":parent,"message":{"role":"assistant","content":[{"type":"toolCall","id":call,"name":"shell","arguments":{"command":"inspect"}}]}}));
        rows.push(json!({"type":"message","id":result,"parentId":call,"message":{"role":"toolResult","toolCallId":call,"content":output}}));
        parent = json!(result);
    }
    write_records(&path, &rows, false);
    ok(cli(&home, &data, &["index"]));
    let c = store::reader(&DataRoot::open(&data, false).unwrap()).unwrap();
    let handles = |r: &agrep_core::query::Results| {
        r.hits.iter().map(|h| h.handle.clone()).collect::<Vec<_>>()
    };
    for (pattern, chats) in [
        ("needle uncommon", false),
        ("uncommon needle", false),
        ("needle", false),
        ("needle uncommon", true),
    ] {
        for limit in [20, 400] {
            let mut options = retrieval_options(pattern);
            options.limit = limit;
            options.tools = true;
            options.chats = chats;
            let everywhere = agrep_core::query::search(&c, &options).unwrap();
            options.filters.agent = Some("pi".into());
            let scoped = agrep_core::query::search(&c, &options).unwrap();
            options.exact = true;
            let exact = agrep_core::query::search(&c, &options).unwrap();
            assert!(!scoped.hits.is_empty(), "{pattern}");
            assert_eq!(handles(&scoped), handles(&everywhere), "{pattern}");
            assert_eq!(handles(&scoped), handles(&exact), "{pattern}");
            if scoped.hits.len() < limit {
                assert_eq!(
                    (scoped.total, scoped.total_basis.as_str()),
                    (exact.total, "exact"),
                    "{pattern}"
                );
            }
            // The scope holds 3,136 rows; only rows holding the terms are decoded.
            assert!(scoped.examined < 512, "{pattern}: {}", scoped.examined);
        }
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
    // 0.4.9's aggregate_sessions joins whitespace before taking the first 120 characters.
    assert_eq!(got.2, "first prompt body");
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
fn no_meta_filters_the_page_0_4_9_fetched() {
    use serde_json::json;
    // 0.4.9 fetched max(-n, 40) rows, dropped the proven ~meta rows among them and kept
    // the best one only when nothing else was left; its totals then count the kept rows.
    for meta_rows in [30, 80] {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        let data = t.path().join("data");
        let path = home.join(".pi/agent/sessions/test/meta-page.jsonl");
        let mut records =
            vec![json!({"type":"session","id":"meta-page","version":3,"cwd":"/work/test"})];
        // A chat is indexed through its turns, so the calls follow one.
        records.push(json!({"type":"message","id":"u0","parentId":null,"timestamp":"2024-01-01T00:00:00Z","message":{"role":"user","content":[{"type":"text","text":"start the session"}]}}));
        let mut parent = String::from("u0");
        for i in 0..=meta_rows {
            let call = format!("call-{i}");
            let result = format!("result-{i}");
            let (time, command, output) = if i < meta_rows {
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
        let r = agrep_core::query::search(&c, &o).unwrap();
        assert_eq!(r.hits.len(), 1);
        let shape = (
            r.meta_dropped,
            r.meta_retained,
            r.total,
            r.total_basis.as_str(),
        );
        if meta_rows < 40 {
            assert!(r.hits[0].text.contains("ordinary evidence"));
            assert!(!r.hits[0].meta_marker);
            assert_eq!(shape, (meta_rows, 0, 1, "exact"));
        } else {
            assert!(r.hits[0].text.contains("agrep search"));
            assert!(r.hits[0].meta_marker);
            assert_eq!(shape, (39, 1, 1, "floor"));
        }
    }
}

#[test]
fn no_meta_work_follows_the_page_not_the_corpus() {
    use serde_json::json;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    let mut extra = vec![];
    for n in [64, 256] {
        let t = TempDir::new().unwrap();
        let home = t.path().join("home");
        let data = t.path().join("data");
        for i in 0..n {
            write_records(
                &home.join(format!(".pi/agent/sessions/test/lineage-{i}.jsonl")),
                &[
                    json!({"type":"session","id":format!("lineage-{i}"),"version":3,"cwd":"/work/test"}),
                    json!({"type":"message","id":"u","parentId":null,"message":{"role":"user","content":format!("question {i}")}}),
                    json!({"type":"message","id":"c","parentId":"u","message":{"role":"assistant","content":[{"type":"toolCall","id":"call","name":"shell","arguments":{"command":"agrep search needle"}}]}}),
                    json!({"type":"message","id":"r","parentId":"c","message":{"role":"toolResult","toolCallId":"call","content":format!("needle from history {i}")}}),
                    json!({"type":"message","id":"a","parentId":"r","message":{"role":"assistant","content":format!("needle history answer {i}")}}),
                ],
                false,
            );
        }
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
        let mut o = retrieval_options("needle");
        o.tools = true;
        o.chats = true;
        let mut work = vec![];
        for no_meta in [false, true] {
            o.filters.no_meta = no_meta;
            let before = steps.load(Ordering::Relaxed);
            let r = agrep_core::query::search(&c, &o).unwrap();
            work.push(steps.load(Ordering::Relaxed) - before);
            if no_meta {
                // Every chat's best row is an answer read from history: the 40 fetched
                // heads are checked, and only the best one stays.
                assert_eq!((r.meta_dropped, r.meta_retained, r.hits.len()), (39, 1, 1));
            } else {
                assert_eq!(r.hits.len(), 10);
            }
        }
        extra.push(work[1].saturating_sub(work[0]));
    }
    assert!(extra[1] < extra[0] * 2, "{extra:?}");
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
    // 0.4.9 printed the lane on stderr below the miss's explanation and kept the miss's
    // exit status (py/search.py:6484-6531, 8139-8157, 8553-8560).
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8_lossy(&output.stderr);
    let lines: Vec<&str> = stderr.lines().collect();
    assert_eq!(lines.len(), 3, "{stderr}");
    assert!(lines[0].starts_with("keyword: 0 matching rows"), "{stderr}");
    assert_eq!(
        lines[1],
        "coverage lane (forced): top row matched 2/3 terms - dropped: missing"
    );
    assert!(lines[2].starts_with("@test-ses"), "{stderr}");
    assert!(
        lines[2].ends_with(" ~all specific useful context"),
        "{stderr}"
    );
    let empty = cli(&home, &data, &["--coverage", "nothing matches here"]);
    assert_eq!(empty.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&empty.stderr)
        .lines()
        .any(|l| l == "coverage lane: no candidate rows"));
    // 0.4.9 ranked the coverage lane by coverage whatever --sort said.
    for sort in ["time", "position"] {
        let sorted = cli(
            &home,
            &data,
            &["--coverage", "specific useful missing", "--sort", sort],
        );
        assert_eq!(
            (sorted.status.code(), sorted.stdout, sorted.stderr),
            (
                output.status.code(),
                output.stdout.clone(),
                output.stderr.clone()
            )
        );
    }
}

/// 0.4.9's forced lane (py/search.py:6412-6445, py/corpusdb.py:5165-5204): rows quoting
/// the whole query are left out, each chat gives its best other row even when the page
/// already shows that chat, and at most 20 chats are listed.
#[test]
fn coverage_lane_skips_echoes_and_lists_one_row_per_chat() {
    let t = TempDir::new().unwrap();
    let home = t.path().join("h");
    let data = t.path().join("d");
    let chat = |name: &str, texts: &[&str]| {
        let p = home.join(format!(".pi/agent/sessions/{name}/chat.jsonl"));
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        let mut lines = vec![serde_json::json!({"type":"session","id":format!("{name}-session"),"version":3,"cwd":format!("/work/{name}"),"timestamp":"2026-10-29T12:00:00Z"}).to_string()];
        for (i, text) in texts.iter().enumerate() {
            lines.push(serde_json::json!({"type":"message","id":format!("m{i}"),"parentId":if i==0{Value::Null}else{Value::String(format!("m{}",i-1))},"timestamp":"2026-10-29T12:00:01Z","message":{"role":"user","content":text}}).to_string());
        }
        fs::write(&p, lines.join("\n") + "\n").unwrap();
    };
    chat(
        "echo",
        &["rotate secret credentials", "rotate the vault credentials"],
    );
    for i in 0..24 {
        chat(
            &format!("many{i:02}"),
            &[
                &format!("rotate credentials number {i}"),
                &format!("rotate keys number {i}"),
            ],
        );
    }
    ok(cli(&home, &data, &["index"]));
    let output = cli(&home, &data, &["--coverage", "rotate secret credentials"]);
    assert_eq!(output.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&output.stdout).contains("rotate secret credentials"));
    let stderr = String::from_utf8_lossy(&output.stderr);
    let block: Vec<&str> = stderr
        .lines()
        .skip_while(|l| !l.starts_with("coverage lane (forced): "))
        .collect();
    assert_eq!(
        block[0], "coverage lane (forced): top row matched 2/3 terms - dropped: secret",
        "{stderr}"
    );
    let rows = &block[1..];
    assert_eq!(rows.len(), 20, "{stderr}");
    assert!(rows
        .iter()
        .all(|r| !r.contains("rotate secret credentials")));
    assert!(rows
        .iter()
        .any(|r| r.ends_with("rotate the vault credentials")));
    let chats: std::collections::HashSet<_> =
        rows.iter().map(|r| r.split(':').next().unwrap()).collect();
    assert_eq!(chats.len(), 20, "{stderr}");
    assert!(rows.iter().all(|r| !r.contains("rotate keys")));
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
        // The first stage reads the rarest term's rows in the four blocks before coverage
        // bounds apply. Rows of the other term differ only past the fourth place, so they
        // tie and those earlier in the chat join the page; the second stage reads only them.
        assert!(
            fast.examined <= 1024 + options.limit,
            "{pattern}: {}",
            fast.examined
        );
        options.exact = true;
        let full = agrep_core::query::search(&c, &options).unwrap();
        assert_eq!(
            fast.hits.iter().map(|h| &h.handle).collect::<Vec<_>>(),
            full.hits.iter().map(|h| &h.handle).collect::<Vec<_>>()
        );
    }
}

#[test]
fn appended_bounds_tighten_to_a_complete_rebuild() {
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
    fn check_search(root: &DataRoot) {
        let c = store::reader(root).unwrap();
        for pattern in ["needle", "old marker", "βeta"] {
            let mut options = retrieval_options(pattern);
            options.limit = 20;
            let fast = agrep_core::query::search(&c, &options).unwrap();
            options.exact = true;
            let exact = agrep_core::query::search(&c, &options).unwrap();
            assert_eq!(
                fast.hits.iter().map(|h| &h.handle).collect::<Vec<_>>(),
                exact.hits.iter().map(|h| &h.handle).collect::<Vec<_>>()
            );
        }
    }
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let path = transcript(&home, &vec!["old marker"; 600]);
    let root = DataRoot::open(&data, true).unwrap();
    let merge = root.merge_lock().unwrap().unwrap();
    ok(cli(&home, &data, &["index"]));
    write_records(
        &path,
        &[
            serde_json::json!({"type":"message","id":"m600","parentId":"m599","timestamp":"2099-01-01T00:00:00Z","message":{"role":"user","content":"new needle needle βeta"}}),
        ],
        true,
    );
    ok(cli(&home, &data, &["index"]));
    check_search(&root);
    drop(merge);
    agrep_core::fts_segments::upkeep(&root).unwrap();
    let incremental = bounds(&root);
    store::rebuild_bounds(&root).unwrap();
    assert_eq!(incremental, bounds(&root));
    // Metadata-only edits must raise ceilings before the worker tightens them too.
    let text = fs::read_to_string(&path)
        .unwrap()
        .replace("2026-10-29T12:00:01Z", "2099-02-01T00:00:00Z");
    fs::write(&path, text).unwrap();
    let merge = root.merge_lock().unwrap().unwrap();
    ok(cli(&home, &data, &["index"]));
    check_search(&root);
    drop(merge);
    agrep_core::fts_segments::upkeep(&root).unwrap();
    assert_eq!(
        store::reader(&root)
            .unwrap()
            .query_row("SELECT count(*) FROM gram_raises", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    let incremental = bounds(&root);
    store::rebuild_bounds(&root).unwrap();
    assert_eq!(incremental, bounds(&root));
}

fn fts_segments(data: &Path) -> Vec<usize> {
    let c = Connection::open_with_flags(data.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)
        .unwrap();
    ["prose_fts", "tools_fts", "tool_lexicon"]
        .iter()
        .map(|t| {
            agrep_core::fts_segments::segments(&agrep_core::fts_segments::levels(&c, t).unwrap())
        })
        .collect()
}
/// What lookups read from the text indexes, whatever their segment layout.
fn fts_contents(data: &Path) -> Vec<Vec<(String, i64, i64)>> {
    let c = Connection::open_with_flags(data.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)
        .unwrap();
    [
        "SELECT term,doc,cnt FROM prose_vocab ORDER BY term",
        "SELECT term,doc,cnt FROM tools_vocab ORDER BY term",
        "SELECT term,doc,0 FROM tool_words ORDER BY term",
        "SELECT w.term,count(*),0 FROM tool_words w CROSS JOIN tool_lexicon l ON l.rowid=w.id WHERE tool_lexicon MATCH '\"' || substr(w.term,1,3) || '\"' GROUP BY w.term ORDER BY w.term",
    ]
    .iter()
    .map(|sql| {
        c.prepare(sql)
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap()
    })
    .collect()
}
#[test]
fn a_refresh_adds_one_segment_per_text_index_and_the_worker_merges_them() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let turns: Vec<String> = (0..40).map(|i| format!("prose turn {i} marker")).collect();
    let path = transcript(&h, &turns.iter().map(String::as_str).collect::<Vec<_>>());
    let outputs: Vec<String> = (0..40)
        .map(|i| format!("tool output {i} alpha{i}"))
        .collect();
    let mut parent = append_tool_results(&path, "m39", 0, &outputs);
    ok(cli(&h, &d, &["index"]));
    let mut before = fts_segments(&d);
    assert_eq!(before, [1, 1, 1]);
    for round in 0..6 {
        let mut records = Vec::new();
        for i in 0..12 {
            let id = format!("r{round}u{i}");
            records.push(json!({"type":"message","id":&id,"parentId":&parent,"timestamp":"2026-10-30T12:00:00Z","message":{"role":"user","content":format!("round {round} prompt {i} about cobalt{round}x{i}")}}));
            parent = id;
        }
        write_records(&path, &records, true);
        let novel: Vec<String> = (0..12)
            .map(|i| format!("new tool text novel{round}x{i} with alpha{i}"))
            .collect();
        parent = append_tool_results(&path, &parent, 100 + round * 12, &novel);
        if round == 3 {
            // An interior edit re-parses the source and replaces changed rows' entries.
            let text = fs::read_to_string(&path)
                .unwrap()
                .replace("prose turn 5 marker", "prose turn 5 edited")
                .replace("tool output 7 alpha7", "tool output 7 rewritten");
            fs::write(&path, text).unwrap();
        }
        ok(cli(&h, &d, &["__work", "refresh"]));
        let after = fts_segments(&d);
        for (b, a) in before.iter().zip(&after) {
            assert!(a <= &(b + 1), "round {round}: {before:?} -> {after:?}");
        }
        before = after;
    }
    assert!(before.iter().all(|&n| n > 3), "{before:?}");
    let full = t.path().join("full");
    ok(cli(&h, &full, &["index"]));
    let contents = fts_contents(&full);
    assert_eq!(fts_contents(&d), contents);
    ok(cli(&h, &d, &["__work", "merge"]));
    assert!(fts_segments(&d)
        .iter()
        .all(|&n| n <= agrep_core::fts_segments::CAP));
    assert_eq!(fts_contents(&d), contents);
    assert_eq!(
        ok(cli(&h, &d, &["--no-auto", "novel3x4", "-c"])).stdout,
        ok(cli(&h, &full, &["--no-auto", "novel3x4", "-c"])).stdout
    );
}

/// `index` within `limit`, its output read to the end as a capturing caller reads it.
fn index_output(h: &Path, d: &Path, limit: std::time::Duration) -> Output {
    let child = Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", h)
        .env("AGREP_HOME", h)
        .env("AGREP_DATA_DIR", d)
        .env("AGREP_PROFILE", "classic")
        .arg("index")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || sender.send(child.wait_with_output()));
    // A watchdog, not synchronization: output held open by a waiting merger never closes.
    receiver
        .recv_timeout(limit)
        .expect("index's output stayed open after it exited")
        .unwrap()
}

/// std's CreateProcess on Windows passes every inheritable handle to the child, and a
/// command's standard handles are inheritable when its caller passed pipes. The merger
/// `index` starts waits here while a writer is queued; had it inherited the pipes, the
/// output would stay open until that writer was served.
#[test]
fn index_output_closes_while_the_merger_it_started_waits() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = transcript(&h, &["prose turn marker"]);
    ok(cli(&h, &d, &["index"]));
    for (id, parent) in [("u0", "m0"), ("u1", "u0"), ("u2", "u1")] {
        write_records(
            &path,
            &[
                json!({"type":"message","id":id,"parentId":parent,"timestamp":"2026-10-30T12:00:00Z","message":{"role":"user","content":format!("change {id} cobalt")}}),
            ],
            true,
        );
        if id != "u2" {
            ok(cli(&h, &d, &["__work", "refresh"]));
        }
    }
    let c =
        Connection::open_with_flags(d.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    assert!(agrep_core::fts_segments::pending(&c).unwrap());
    drop(c);
    // What a blocking writer holds while it waits for `writer.lock`.
    let queued = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(d.join("writer.queue"))
        .unwrap();
    queued.lock_shared().unwrap();
    ok(index_output(&h, &d, std::time::Duration::from_secs(300)));
    queued.unlock().unwrap();
}

/// The automatic refresh commits the appended source before the query reads its snapshot.
#[test]
fn a_query_commits_a_changed_source_before_answering() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = transcript(&h, &["prose turn marker"]);
    ok(cli(&h, &d, &["index"]));
    write_records(
        &path,
        &[
            json!({"type":"message","id":"u0","parentId":"m0","timestamp":"2026-10-30T12:00:00Z","message":{"role":"user","content":"appended cobalt turn"}}),
        ],
        true,
    );
    let query = Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", &h)
        .env("AGREP_HOME", &h)
        .env("AGREP_DATA_DIR", &d)
        .env("AGREP_PROFILE", "classic")
        .env("AGREP_QUERY_TIMING", "1")
        .args(["cobalt", "-c"])
        .output()
        .unwrap();
    assert!(query.status.success());
    assert_eq!(String::from_utf8_lossy(&query.stdout).trim(), "1");
    let events: Vec<Value> = String::from_utf8_lossy(&query.stderr)
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();
    assert_eq!(
        events.iter().find_map(|e| e.get("auto_refresh")).unwrap()["parsed"],
        1
    );
    let found = ok(cli(&h, &d, &["cobalt", "-c", "--no-auto"]));
    assert_eq!(String::from_utf8_lossy(&found.stdout).trim(), "1");
    assert_eq!(pending_sources(&h, &d), 0);
}

/// The first build leaves one segment per text index; later refreshes' smaller segments
/// merge below it instead of rewriting it, until the cap forces a whole-table merge.
/// FTS5 moves a segment no larger than a newly written one down beside it, so the
/// fixture's first build outweighs a refresh.
#[test]
fn the_first_build_segment_stays_out_of_the_merges_of_later_refreshes() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let turns: Vec<String> = (0..2000)
        .map(|i| format!("prose turn {i} marker word{i}x"))
        .collect();
    let path = transcript(&h, &turns.iter().map(String::as_str).collect::<Vec<_>>());
    ok(cli(&h, &d, &["index"]));
    let sealed = || {
        let c = Connection::open_with_flags(d.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
        let levels = agrep_core::fts_segments::levels(&c, "prose_fts").unwrap();
        let top = levels.iter().rposition(|l| !l.pages.is_empty()).unwrap();
        (top, levels[top].pages.clone())
    };
    let first = sealed();
    assert!(first.0 >= 5 && first.1[0] > 1, "{first:?}");
    let mut parent = "m1999".to_string();
    for round in 0..4 {
        let id = format!("r{round}");
        write_records(
            &path,
            &[
                json!({"type":"message","id":&id,"parentId":&parent,"timestamp":"2026-10-30T12:00:00Z","message":{"role":"user","content":format!("round {round} novel{round}")}}),
            ],
            true,
        );
        parent = id;
        ok(cli(&h, &d, &["__work", "refresh"]));
        ok(cli(&h, &d, &["__work", "merge"]));
        assert_eq!(sealed(), first, "round {round}");
        assert!(fts_segments(&d)
            .iter()
            .all(|&n| n <= agrep_core::fts_segments::CAP));
    }
    let full = t.path().join("full");
    ok(cli(&h, &full, &["index"]));
    assert_eq!(fts_contents(&d), fts_contents(&full));
}

/// A merger mid-merge holds merge.lock and starts no step while a writer is queued.
/// The test holds both, so an `index` that merged inline or waited for the merger
/// would merge here or never return.
#[test]
fn index_waits_for_no_merge_step_and_leaves_merging_to_the_merger() {
    use serde_json::json;
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let path = transcript(&h, &["prose turn marker"]);
    ok(cli(&h, &d, &["index"]));
    let append = |id: &str, parent: &str, text: &str| {
        write_records(
            &path,
            &[
                json!({"type":"message","id":id,"parentId":parent,"timestamp":"2026-10-30T12:00:00Z","message":{"role":"user","content":text}}),
            ],
            true,
        );
    };
    append("u0", "m0", "first change cobaltzero");
    ok(cli(&h, &d, &["__work", "refresh"]));
    append("u1", "u0", "second change cobaltone");
    ok(cli(&h, &d, &["__work", "refresh"]));
    let pending = || {
        let c = Connection::open_with_flags(d.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
        agrep_core::fts_segments::pending(&c).unwrap()
    };
    assert!(pending());
    let before = fts_segments(&d);
    let root = DataRoot::open(&d, false).unwrap();
    let merging = root.merge_lock().unwrap().unwrap();
    let queued = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(d.join("writer.queue"))
        .unwrap();
    queued.lock_shared().unwrap();
    append("u2", "u1", "third change cobalttwo");
    let child = Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", &h)
        .env("AGREP_HOME", &h)
        .env("AGREP_DATA_DIR", &d)
        .env("AGREP_PROFILE", "classic")
        .arg("index")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || sender.send(child.wait_with_output()));
    // A watchdog, not synchronization: an index that waits for the merger never exits.
    let index = receiver
        .recv_timeout(std::time::Duration::from_secs(300))
        .expect("index waited for the merge in progress")
        .unwrap();
    ok(index);
    let after = fts_segments(&d);
    assert_eq!(after[0], before[0] + 1, "{before:?} -> {after:?}");
    assert_eq!(after[1..], before[1..]);
    assert!(pending());
    queued.unlock().unwrap();
    drop(merging);
    // Commands start a merger when their census reports merge work.
    let merge_pending = || {
        serde_json::from_slice::<Value>(&ok(cli(&h, &d, &["__census"])).stdout).unwrap()
            ["merge_pending"]
            .as_bool()
            .unwrap()
    };
    assert!(merge_pending());
    ok(cli(&h, &d, &["__work", "merge"]));
    assert!(!pending());
    assert!(!merge_pending());
    assert!(fts_segments(&d)
        .iter()
        .all(|&n| n <= agrep_core::fts_segments::CAP));
    let full = t.path().join("full");
    ok(cli(&h, &full, &["index"]));
    assert_eq!(fts_contents(&d), fts_contents(&full));
    for term in ["cobaltzero", "cobaltone", "cobalttwo"] {
        assert_eq!(
            ok(cli(&h, &d, &["--no-auto", term, "-c"])).stdout,
            ok(cli(&h, &full, &["--no-auto", term, "-c"])).stdout
        );
    }
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
/// Two pi chats for recall coverage expansion: a small one the budget can show whole,
/// and a long one whose other turns name single query terms.
fn expansion_fixture(home: &Path) {
    let base = home.join(".pi/agent/sessions/project");
    fs::create_dir_all(&base).unwrap();
    let notes = |turn: usize, n: usize| {
        (0..n)
            .map(|j| format!("note{turn}-{j}"))
            .collect::<Vec<_>>()
            .join(" ")
    };
    let write = |name: &str, id: &str, day: usize, texts: Vec<String>| {
        let mut lines = vec![serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/project","timestamp":format!("2026-10-{day:02}T12:00:00Z")}).to_string()];
        for (i, text) in texts.iter().enumerate() {
            lines.push(serde_json::json!({"type":"message","id":format!("m{i}"),"parentId":if i == 0 {Value::Null} else {Value::String(format!("m{}", i - 1))},"timestamp":format!("2026-10-{day:02}T12:{i:02}:00Z"),"message":{"role":"user","content":text}}).to_string());
        }
        fs::write(base.join(name), lines.join("\n") + "\n").unwrap();
    };
    let mut small: Vec<String> = (0..6)
        .map(|i| format!("small turn {i} {}", notes(i, 5)))
        .collect();
    small[0] = format!("where is the cobalt lantern checklist {}", notes(0, 5));
    write("small.jsonl", "small-session", 1, small);
    let mut large: Vec<String> = (0..40)
        .map(|i| format!("large turn {i} {}", notes(i, 400)))
        .collect();
    large[20] = format!("the cobalt lantern checklist moved {}", notes(20, 400));
    large[5] = format!("a cobalt note {}", notes(5, 400));
    large[35] = format!("a lantern note {}", notes(35, 400));
    write("large.jsonl", "large-session", 2, large);
}
/// The block of `text` whose head names `handle_prefix`, as turn numbers and gap lines.
fn block_turns(text: &str, handle_prefix: &str) -> Vec<String> {
    text.split("\n\n")
        .find(|block| block.starts_with(&format!("── {handle_prefix}")))
        .unwrap_or_else(|| panic!("no block for {handle_prefix}: {text}"))
        .lines()
        .skip(1)
        .map(|line| {
            let line = line.trim_start();
            if line.starts_with('[') {
                line.to_owned()
            } else {
                line.split_whitespace().next().unwrap().to_owned()
            }
        })
        .collect()
}
#[test]
fn recall_shows_fitting_chats_whole_and_covers_other_query_turns() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    expansion_fixture(&h);
    ok(cli(&h, &d, &["index"]));
    let out = ok(cli(&h, &d, &["recall", "cobalt lantern"]));
    let text = String::from_utf8(out.stdout).unwrap();
    let turns = |list: &[&str]| list.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    // agrep 0.4.9 renders this page byte for byte: the small chat fits its share and is
    // shown whole; the long chat adds one-turn windows around the turns holding a query
    // term, merged into its block with the turns between them named.
    assert_eq!(
        block_turns(&text, "@small-se:0."),
        turns(&["0", "1", "2", "3", "4", "5"])
    );
    assert_eq!(
        block_turns(&text, "@large-se:20."),
        turns(&[
            "4",
            "5",
            "6",
            "[turns 7-17 not shown]",
            "18",
            "19",
            "20",
            "21",
            "22",
            "[turns 23-33 not shown]",
            "34",
            "35",
            "36"
        ])
    );
    // An uncapped page has no share to fill: each hit keeps its own window.
    let out = ok(cli(&h, &d, &["recall", "cobalt lantern", "--budget", "0"]));
    let text = String::from_utf8(out.stdout).unwrap();
    assert_eq!(block_turns(&text, "@small-se:0."), turns(&["0", "1", "2"]));
    assert_eq!(
        block_turns(&text, "@large-se:20."),
        turns(&["18", "19", "20", "21", "22"])
    );
    // The JSON page carries the same merged windows.
    let out = ok(cli(&h, &d, &["recall", "cobalt lantern", "--json"]));
    let page: Value = serde_json::from_slice(&out.stdout).unwrap();
    let rows = |hit: &Value| {
        hit["window"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["turn"].as_i64().unwrap())
            .collect::<Vec<_>>()
    };
    assert_eq!(
        rows(&page["hits"][0]),
        [4, 5, 6, 18, 19, 20, 21, 22, 34, 35, 36]
    );
    assert_eq!(rows(&page["hits"][1]), [0, 1, 2, 3, 4, 5]);
}
/// `golden` with its UTC head times rendered in this machine's zone, as the CLI renders them.
fn localized(golden: &str, utc: &[&str]) -> String {
    let mut out = golden.to_owned();
    for stamp in utc {
        let local = chrono::NaiveDateTime::parse_from_str(stamp, "%Y-%m-%d %H:%M")
            .unwrap()
            .and_utc()
            .with_timezone(&chrono::Local)
            .format("%Y-%m-%d %H:%M")
            .to_string();
        out = out.replace(stamp, &local);
    }
    out
}
#[test]
fn recall_fits_budgets_like_0_4_9() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    expansion_fixture(&h);
    ok(cli(&h, &d, &["index"]));
    // The goldens are agrep 0.4.9's pages for this fixture: blocks share the budget with a
    // double share for the first, messages are capped to their block's share, and the
    // farthest context turns give way first.
    for budget in ["8000", "4000"] {
        let out = ok(cli(
            &h,
            &d,
            &["recall", "cobalt lantern", "--lexical", "--budget", budget],
        ));
        let golden = fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join(format!("tests/golden/recall-expansion-{budget}.txt")),
        )
        .unwrap();
        assert_eq!(
            String::from_utf8(out.stdout).unwrap(),
            localized(&golden, &["2026-10-02 12:20", "2026-10-01 12:00"]),
            "--budget {budget}"
        );
    }
    let out = cli(
        &h,
        &d,
        &["recall", "cobalt lantern", "--lexical", "--budget", "64"],
    );
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
    assert!(String::from_utf8_lossy(&out.stderr).contains(
        "--budget 64 is too small for this page: its first hit block and the over-budget line need at least 113 bytes"
    ));
}
/// A ranking as `--json` prints it: each row's turn, speaker, score, snippet and lane.
fn ranked_rows(out: Output) -> Vec<Value> {
    String::from_utf8(ok(out).stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|row| row.get("handle").is_some())
        .map(|row| {
            serde_json::json!({
                "turn": row["turn"],
                "who": row["who"],
                "score": row["score"],
                "snippet": row["snippet"],
                "matched": row.get("matched").cloned().unwrap_or_else(|| "phrase".into()),
            })
        })
        .collect()
}
/// Indexes `fixtures/<name>/home` and compares each case of `<name>/expected.json`, agrep
/// 0.4.9's `--json` rankings of that fixture.
#[track_caller]
fn rankings_match_0_4_9(name: &str, count: usize) {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    fixture(name, &h);
    ok(cli(&h, &d, &["index"]));
    let golden = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name)
        .join("expected.json");
    let cases: Vec<Value> = serde_json::from_str(&fs::read_to_string(golden).unwrap()).unwrap();
    assert_eq!(cases.len(), count);
    for case in cases {
        let mut args: Vec<&str> = case["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|arg| arg.as_str().unwrap())
            .collect();
        args.extend(["--json", "--no-auto"]);
        assert_eq!(
            ranked_rows(cli(&h, &d, &args)),
            case["rows"].as_array().unwrap().clone(),
            "{args:?}"
        );
    }
}
/// Scores count the query only inside each hit's snippet window: long repetitive replies,
/// tighter matches outside the window, whitespace runs that collapse, de-cut boundary
/// grading, all-terms windows stitched across cuts, -w and -E, an exact four-place tie
/// rounded half to even, session heads folding lanes, time order and bounded pages.
#[test]
fn keyword_rankings_match_0_4_9() {
    rankings_match_0_4_9("ranking_0_4_9", 13);
}
/// `-w` as Python's `re` matched it: `\w` is an alphanumeric character or `_`, so combining
/// marks, connector punctuation, joiners and circled letters bound a word while
/// superscript and circled digits extend one; IGNORECASE equates i, İ and ı, s and ſ, k and
/// the Kelvin sign, σ and ς. The term is literal: its spaces, and its punctuation edges,
/// whose outer neighbors are checked; an occurrence rejected at its edges does not hide one
/// overlapping it, and two matches in a snippet score higher than one.
#[test]
fn whole_words_match_0_4_9() {
    rankings_match_0_4_9("word_0_4_9", 12);
}
/// `-w` used to compile `(?<!\w)(?:test)(?!\w)` for fancy-regex, which charges a backtrack
/// for each start position it rejects: a row holding "test" only inside longer words
/// exceeded its limit of 100,000 and failed the search (exit 2).
#[test]
fn a_whole_word_search_reads_rows_past_the_old_backtrack_limit() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let long = "testing the latest tests ".repeat(10_000);
    let ending = format!("{long}test");
    transcript(&h, &[&long, &ending]);
    ok(cli(&h, &d, &["index"]));
    let count = ok(cli(&h, &d, &["test", "-w", "-c"])).stdout;
    assert_eq!(String::from_utf8(count).unwrap().trim(), "1");
    let rows = ranked_rows(cli(
        &h,
        &d,
        &["test", "-w", "-n", "0", "--json", "--no-auto"],
    ));
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["turn"], 1);
}
/// Rows rank by their four-place score, then time. The newest row of a tie may sit in a
/// block whose ceiling the first block's row outranks before rounding; agrep 0.4.9
/// returned it first, at 0.0133 each (raw 0.0133038 for the user row, 0.0132964 for the
/// agent row).
#[test]
fn a_four_place_tie_in_a_later_block_wins_on_time_as_in_0_4_9() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let mut messages = vec![
        ("user", format!("flaky{}windows", "-".repeat(439))),
        ("assistant", "noted.".to_owned()),
    ];
    for i in 0..300 {
        messages.push(("user", "next?".to_owned()));
        messages.push(("assistant", format!("flaky x windows {i}")));
    }
    messages.push(("user", "next?".to_owned()));
    messages.push(("assistant", format!("flaky{}windows", "-".repeat(349))));
    let mut lines = vec![serde_json::json!({"type":"session","id":"tie-session","version":3,"cwd":"/work/project","timestamp":"2099-01-01T00:00:00Z"}).to_string()];
    for (i, (role, text)) in messages.iter().enumerate() {
        let content = if *role == "user" {
            serde_json::json!(text)
        } else {
            serde_json::json!([{"type": "text", "text": text}])
        };
        let parent = if i == 0 {
            Value::Null
        } else {
            format!("m{}", i - 1).into()
        };
        let stamp = format!(
            "2099-01-01T{:02}:{:02}:{:02}Z",
            i / 3600,
            i / 60 % 60,
            i % 60
        );
        lines.push(serde_json::json!({"type":"message","id":format!("m{i}"),"parentId":parent,"timestamp":stamp,"message":{"role":role,"content":content}}).to_string());
    }
    let base = h.join(".pi/agent/sessions/project");
    fs::create_dir_all(&base).unwrap();
    fs::write(base.join("tie.jsonl"), lines.join("\n") + "\n").unwrap();
    ok(cli(&h, &d, &["index"]));
    let db =
        Connection::open_with_flags(d.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let block = |turn: i64, who: &str| {
        db.query_row(
            "SELECT id FROM prose WHERE position=?1 AND who=?2",
            rusqlite::params![turn, who],
            |r| r.get::<_, i64>(0),
        )
        .unwrap()
            / 512
    };
    assert!(block(0, "user") < block(301, "agent"));
    for (limit, expected) in [
        ("1", vec![(301, "agent")]),
        ("2", vec![(301, "agent"), (0, "user")]),
    ] {
        let rows = ranked_rows(cli(
            &h,
            &d,
            &["flaky windows", "--json", "--no-auto", "-n", limit],
        ));
        assert_eq!(
            rows.iter()
                .map(|row| (row["turn"].as_i64().unwrap(), row["who"].as_str().unwrap()))
                .collect::<Vec<_>>(),
            expected
        );
        assert!(rows.iter().all(|row| row["score"] == 0.0133));
    }
}
/// A block that only loses a score tie on time does not end the walk: blocks come in raw
/// ceiling order, and a later block's lower ceiling can round to the same score. Old
/// prompts keep the 0.35 recency floor, so eleven separated pairs give the middle block a
/// ceiling of 0.9 * (1 - 2^-11) * 0.35 = 0.314846, above the 0.96875 * 0.325 = 0.314844 of
/// the last block's recap. Both round to 0.3148, where that newer recap beats the first
/// block's older twin.
#[test]
fn a_tie_won_on_time_lies_past_a_block_that_loses_it() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let base = h.join(".pi/agent/sessions/project");
    fs::create_dir_all(&base).unwrap();
    let session = |id: &str, entries: Vec<(&str, &str, &str)>| {
        let mut lines = vec![serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/project","timestamp":"2099-01-01T00:00:00Z"}).to_string()];
        // More rows than a block holds, so the next session's rows start a later block.
        let filler = (0..600).map(|i| {
            let role = if i % 2 == 0 { "user" } else { "assistant" };
            (role, "2099-01-01T00:00:05Z", "ill")
        });
        let mut parent = Value::Null;
        for (i, (kind, stamp, text)) in entries.into_iter().chain(filler).enumerate() {
            let entry = format!("{id}-{i}");
            let line = if kind == "compaction" {
                serde_json::json!({"type":"compaction","id":entry,"parentId":parent,"timestamp":stamp,"summary":text,"tokensBefore":1000,"firstKeptEntryId":parent})
            } else {
                let content = if kind == "user" {
                    serde_json::json!(text)
                } else {
                    serde_json::json!([{"type": "text", "text": text}])
                };
                serde_json::json!({"type":"message","id":entry,"parentId":parent,"timestamp":stamp,"message":{"role":kind,"content":content}})
            };
            lines.push(line.to_string());
            parent = entry.into();
        }
        fs::write(base.join(format!("{id}.jsonl")), lines.join("\n") + "\n").unwrap();
    };
    let recap = ["quokZebra"; 5].join(" ");
    let pairs = ["quok zebra"; 11].join(" ");
    // A fresh prompt holding both words gives the first block the highest ceiling.
    session(
        "tie-a",
        vec![
            ("user", "2099-01-01T00:00:00Z", "zebra then quok"),
            ("compaction", "2099-01-01T00:00:01Z", &recap),
        ],
    );
    session(
        "tie-b",
        (0..300)
            .map(|i| {
                if i % 2 == 0 {
                    ("user", "2020-01-01T00:00:00Z", pairs.as_str())
                } else {
                    ("assistant", "2020-01-01T00:00:00Z", "ill")
                }
            })
            .collect(),
    );
    session(
        "tie-c",
        vec![
            ("user", "2099-06-01T00:00:00Z", "ill"),
            ("compaction", "2099-06-01T00:00:01Z", &recap),
        ],
    );
    ok(cli(&h, &d, &["index"]));
    let db =
        Connection::open_with_flags(d.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    // The recaps of the first and last sessions, and the middle session's old prompts.
    let blocks = |session: &str, rows: &str| {
        db.query_row(
            &format!("SELECT min(id)/512,max(id)/512 FROM prose WHERE session=?1 AND {rows}"),
            [session],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)),
        )
        .unwrap()
    };
    let (a, b, c) = (
        blocks("tie-a", "who='recap'"),
        blocks("tie-b", "who='user' AND ts<1600000000000"),
        blocks("tie-c", "who='recap'"),
    );
    assert!(a.1 < b.0 && b.1 < c.0, "{a:?} {b:?} {c:?}");
    let page = |n: &str| -> Vec<(String, i64, String, f64)> {
        let out = ok(cli(&h, &d, &["quok zebra", "--json", "--no-auto", "-n", n]));
        String::from_utf8(out.stdout)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .filter(|row| row.get("handle").is_some())
            .map(|row| {
                (
                    row["session"].as_str().unwrap().to_owned(),
                    row["turn"].as_i64().unwrap(),
                    row["who"].as_str().unwrap().to_owned(),
                    row["score"].as_f64().unwrap(),
                )
            })
            .collect()
    };
    let all = page("0");
    assert_eq!(
        all[..2],
        [
            ("tie-c".to_owned(), 1, "recap".to_owned(), 0.3148),
            ("tie-a".to_owned(), 1, "recap".to_owned(), 0.3148)
        ]
    );
    for n in [1, 2] {
        assert_eq!(page(&n.to_string()), all[..n], "-n {n}");
    }
}
/// Bounded pages equal the exhaustive ranking's head under snippet-window scores: rows
/// repeat terms far beyond their window, spread phrases over whitespace runs, mix Unicode
/// separators and round to equal scores across several blocks and chats. Timestamps lie
/// in the future (recency one) or far in the past for prompts (their floor), so the
/// clock cannot move a score across a rounding step between the two searches.
#[test]
fn bounded_pages_equal_exhaustive_rankings_with_snippet_windows() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let mut state = 0x2545_f491_4f6c_dd1du64;
    let mut next = move |n: usize| {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        (state % n as u64) as usize
    };
    let words = [
        "flaky",
        "windows",
        "cache",
        "flakywindows",
        "Flaky",
        "WINDOWS",
        "café",
        "the",
        "a",
        "runner",
        "xflaky",
        "windowsx",
        "build",
        "naïve",
        "release",
        "workflow",
    ];
    let gaps = [
        " ", " ", " ", "  ", "\n\n", "-", "_", " — ", "… ", ", ", "/", "\t\t\t ",
    ];
    let base = h.join(".pi/agent/sessions/project");
    for session in 0..6 {
        let mut lines = vec![serde_json::json!({"type":"session","id":format!("s{session}"),"version":3,"cwd":"/work/project","timestamp":"2099-01-01T00:00:00Z"}).to_string()];
        for i in 0..520 {
            let long = next(4) == 0;
            let length = 3 + next(if long { 400 } else { 40 });
            let mut text = String::new();
            for _ in 0..length {
                text += words[next(words.len())];
                text += gaps[next(gaps.len())];
            }
            let role = if i % 2 == 0 { "user" } else { "assistant" };
            let content = if role == "user" {
                serde_json::json!(text)
            } else {
                serde_json::json!([{"type": "text", "text": text}])
            };
            let stamp = if role == "user" && next(3) == 0 {
                format!("2020-01-{:02}T00:00:00Z", 1 + next(28))
            } else {
                format!("2099-01-{:02}T{:02}:00:00Z", 1 + next(28), next(24))
            };
            let parent = if i == 0 {
                Value::Null
            } else {
                format!("m{}", i - 1).into()
            };
            lines.push(serde_json::json!({"type":"message","id":format!("m{i}"),"parentId":parent,"timestamp":stamp,"message":{"role":role,"content":content}}).to_string());
        }
        fs::create_dir_all(&base).unwrap();
        fs::write(
            base.join(format!("s{session}.jsonl")),
            lines.join("\n") + "\n",
        )
        .unwrap();
    }
    ok(cli(&h, &d, &["index"]));
    let c = store::reader(&DataRoot::open(&d, false).unwrap()).unwrap();
    let search = |o: &agrep_core::query::Options| {
        let result = agrep_core::query::search(&c, o).unwrap();
        let hits = result
            .hits
            .into_iter()
            .map(|h| (h.session, h.turn, h.who, h.score, h.snippet))
            .collect::<Vec<_>>();
        (hits, result.examined)
    };
    let (mut bounded, mut exhaustive) = (0, 0);
    for (pattern, regex, word, chats) in [
        ("flaky windows", false, false, false),
        ("flaky windows", false, false, true),
        ("windows flaky", false, false, false),
        ("cache", false, false, false),
        ("café naïve", false, false, false),
        ("release workflow cache", false, false, false),
        ("flak[ey]", true, false, false),
        ("windows", false, true, false),
    ] {
        for limit in [1, 7, 25] {
            let mut o = retrieval_options(pattern);
            (o.regex, o.word, o.chats, o.limit) = (regex, word, chats, limit);
            let (fast, read) = search(&o);
            o.limit = 0;
            let (mut all, every) = search(&o);
            all.truncate(limit);
            assert_eq!(
                fast, all,
                "{pattern} regex={regex} word={word} chats={chats} -n {limit}"
            );
            (bounded, exhaustive) = (bounded + read, exhaustive + every);
        }
    }
    // Nearly every row holds the terms here, yet bounded pages still read fewer rows.
    assert!(bounded < exhaustive, "{bounded} of {exhaustive}");
}
