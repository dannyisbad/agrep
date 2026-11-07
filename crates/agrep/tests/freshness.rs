mod support;
use agrep_core::{data::DataRoot, freshness, store};
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    path::Path,
    process::{Command, Output, Stdio},
    time::{Duration, Instant},
};
use tempfile::TempDir;

fn command(t: &Path) -> Command {
    let mut c = Command::new(support::binary());
    c.env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", t.join("home"))
        .env("AGREP_HOME", t.join("home"))
        .env("AGREP_DATA_DIR", t.join("data"))
        .env("AGREP_PROFILE", "classic");
    c
}
fn chat(t: &Path, id: &str) -> std::path::PathBuf {
    let p = t.join(format!("home/.pi/agent/sessions/project/{id}.jsonl"));
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(&p, format!("{}\n", json!({"type":"session","version":3,"id":id,"cwd":"/work/project","timestamp":chrono::Utc::now().to_rfc3339()}))).unwrap();
    append(&p, "initial", None, "original prompt");
    p
}
fn append(p: &Path, id: &str, parent: Option<&str>, text: &str) {
    let mut file = OpenOptions::new().append(true).open(p).unwrap();
    writeln!(file, "{}", json!({"type":"message","id":id,"parentId":parent,"timestamp":chrono::Utc::now().to_rfc3339(),"message":{"role":"user","content":text}})).unwrap();
}
fn fixture() -> TempDir {
    let t = TempDir::new().unwrap();
    chat(t.path(), "freshness-session");
    let out = command(t.path()).arg("index").output().unwrap();
    assert!(out.status.success(), "{out:?}");
    t
}
#[track_caller]
fn checked(out: Output) -> String {
    assert!(out.status.success(), "{out:?}");
    let stderr = String::from_utf8(out.stderr).unwrap();
    for forbidden in ["freshness", "behind", "catching up", "may not show"] {
        assert!(!stderr.contains(forbidden), "{stderr}");
    }
    let text = String::from_utf8(out.stdout).unwrap();
    assert!(!text.contains("pending_sources"), "{text}");
    text
}
#[test]
fn every_reader_ingests_an_append_before_its_first_query() {
    let cases: &[&[&str]] = &[
        &["search", "cobaltneedle", "--json"],
        &["search", "cobaltneedle", "--lexical"],
        &["search", "cobaltneedle", "-c"],
        &["search", "cobaltneedle", "--chats", "--json"],
        &["search", "cobaltneedle"],
        &["recall", "cobaltneedle", "--lexical", "--json"],
        &["pack", "cobaltneedle", "--lexical", "--json"],
        &["around", "freshness-session", "--whole", "--json"],
        &["chats", "cobaltneedle", "--json"],
        &["summary", "--json", "--self"],
        &["summary", "pending", "--json", "--self"],
        &["status", "--json"],
    ];
    for args in cases {
        let t = fixture();
        let source = t
            .path()
            .join("home/.pi/agent/sessions/project/freshness-session.jsonl");
        append(
            &source,
            "late",
            Some("initial"),
            "please implement cobaltneedle",
        );
        let text = checked(
            command(t.path())
                .args(*args)
                .env("AGREP_PROFILE", "compact")
                .output()
                .unwrap(),
        );
        match args[0] {
            "status" => {
                let v: Value = serde_json::from_str(&text).unwrap();
                assert_eq!(v["index"]["counts"]["prose"], 2);
                assert_eq!(v["freshness"]["checked"], true);
            }
            "search" if args.contains(&"-c") => assert_eq!(text.trim(), "1"),
            _ => assert!(text.contains("cobaltneedle"), "{args:?}: {text}"),
        }
        let probe = command(t.path())
            .args(["cobaltneedle", "-c", "--no-auto"])
            .output()
            .unwrap();
        assert_eq!(
            String::from_utf8(probe.stdout).unwrap().trim(),
            "1",
            "{args:?}"
        );
        assert!(probe.status.success());
    }
}
#[test]
fn opt_outs_leave_sources_unchecked_and_last_good_rows_survive_parse_failure() {
    let t = fixture();
    let source = t
        .path()
        .join("home/.pi/agent/sessions/project/freshness-session.jsonl");
    append(&source, "late", Some("initial"), "cobaltneedle");
    for env in [None, Some(("AGREP_DATA_READONLY", "1"))] {
        let mut c = command(t.path());
        c.args(["original", "--json"]);
        if let Some((key, value)) = env {
            c.env(key, value);
        } else {
            c.arg("--no-auto");
        }
        let out = c.output().unwrap();
        assert!(out.status.success());
        let meta: Value =
            serde_json::from_str(String::from_utf8_lossy(&out.stdout).lines().next().unwrap())
                .unwrap();
        assert_eq!(meta["freshness"]["checked"], false);
        assert!(meta["freshness"].get("pending_sources").is_none());
        let probe = command(t.path())
            .args(["cobaltneedle", "-c", "--no-auto"])
            .output()
            .unwrap();
        assert_eq!(String::from_utf8(probe.stdout).unwrap().trim(), "0");
    }
    fs::write(&source, "{invalid transcript\n").unwrap();
    let text = checked(
        command(t.path())
            .args(["original", "--json"])
            .output()
            .unwrap(),
    );
    assert!(text.contains("original prompt"));
}
fn until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(30);
    while !predicate() {
        assert!(Instant::now() < deadline, "lock synchronization timed out");
        std::thread::yield_now();
    }
}
#[test]
fn a_reader_waits_for_the_writer_and_refreshes_what_it_did_not_cover() {
    const CHILD: &str = "AGREP_FRESHNESS_WRITER_TEST";
    if let Some(path) = std::env::var_os(CHILD) {
        let root = DataRoot(path.into());
        let check = freshness::check(&root).unwrap();
        println!("CENSUS COMPLETE");
        std::io::stdout().flush().unwrap();
        std::io::stdin().read_exact(&mut [0]).unwrap();
        let result = store::refresh_checked(&root, check).unwrap();
        let expected =
            std::env::var("AGREP_FRESHNESS_EXPECT_PARSED").map_or(1, |v| v.parse().unwrap());
        assert_eq!(result.parsed, expected);
    } else {
        let t = fixture();
        let other = chat(t.path(), "other-session");
        checked(
            command(t.path())
                .args(["status", "--json"])
                .output()
                .unwrap(),
        );
        let root = DataRoot(t.path().join("data"));
        let source = t
            .path()
            .join("home/.pi/agent/sessions/project/freshness-session.jsonl");
        append(&source, "covered", Some("initial"), "coveredneedle");
        // WAL readers can census while this transaction stops the preceding refresh's write.
        let mut c = store::writer(&root).unwrap();
        let gate = c
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .unwrap();
        let mut writer = Command::new(std::env::current_exe().unwrap())
            .env_clear()
            .env("HOME", t.path().join("home"))
            .env("AGREP_HOME", t.path().join("home"))
            .env("AGREP_DATA_DIR", root.path())
            .env(CHILD, root.path())
            .args([
                "--exact",
                "a_reader_waits_for_the_writer_and_refreshes_what_it_did_not_cover",
                "--nocapture",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdout = BufReader::new(writer.stdout.take().unwrap());
        loop {
            let mut line = String::new();
            assert!(
                stdout.read_line(&mut line).unwrap() > 0,
                "writer exited before census"
            );
            if line.trim() == "CENSUS COMPLETE" {
                break;
            }
        }
        append(&other, "uncovered", Some("initial"), "uncoveredneedle");
        writer.stdin.take().unwrap().write_all(&[1]).unwrap();
        until(|| root.lock(false).unwrap().is_none());
        let reader = command(t.path())
            .args(["uncoveredneedle", "--json"])
            .env("AGREP_QUERY_TIMING", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        until(|| root.writer_queued().unwrap());
        gate.commit().unwrap();
        let out = writer.wait_with_output().unwrap();
        assert!(out.status.success(), "{out:?}");
        let out = reader.wait_with_output().unwrap();
        assert!(out.status.success(), "{out:?}");
        assert!(String::from_utf8_lossy(&out.stdout).contains("uncoveredneedle"));
        let events: Vec<Value> = String::from_utf8_lossy(&out.stderr)
            .lines()
            .filter_map(|l| serde_json::from_str(l).ok())
            .collect();
        let refresh = events.iter().find_map(|e| e.get("auto_refresh")).unwrap();
        assert_eq!(
            refresh["parsed"], 1,
            "the preceding writer's covered source is not reparsed"
        );
        checked(
            command(t.path())
                .args(["coveredneedle", "--json"])
                .output()
                .unwrap(),
        );
        let check: Value =
            serde_json::from_slice(&command(t.path()).arg("__census").output().unwrap().stdout)
                .unwrap();
        assert_eq!(check["changed_sources"], 0);
        // A writer that committed a newer version than the saved census also counts.
        append(&source, "newer1", Some("covered"), "first newer marker");
        let mut writer = Command::new(std::env::current_exe().unwrap())
            .env_clear()
            .env("HOME", t.path().join("home"))
            .env("AGREP_HOME", t.path().join("home"))
            .env("AGREP_DATA_DIR", root.path())
            .env(CHILD, root.path())
            .env("AGREP_FRESHNESS_EXPECT_PARSED", "0")
            .args([
                "--exact",
                "a_reader_waits_for_the_writer_and_refreshes_what_it_did_not_cover",
                "--nocapture",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdout = BufReader::new(writer.stdout.take().unwrap());
        loop {
            let mut line = String::new();
            assert!(stdout.read_line(&mut line).unwrap() > 0);
            if line.trim() == "CENSUS COMPLETE" {
                break;
            }
        }
        append(&source, "newer2", Some("newer1"), "second newer marker");
        checked(
            command(t.path())
                .args(["status", "--json"])
                .output()
                .unwrap(),
        );
        writer.stdin.take().unwrap().write_all(&[1]).unwrap();
        let out = writer.wait_with_output().unwrap();
        assert!(out.status.success(), "{out:?}");
    }
}

#[test]
fn resume_and_why_resolve_a_session_created_since_the_last_index() {
    for args in [["resume", "--list"], ["why", "new-session"]] {
        let t = fixture();
        chat(t.path(), "new-session");
        let text = checked(command(t.path()).args(args).output().unwrap());
        assert!(text.contains("new-sess"), "{text}");
    }
}

struct Server(std::process::Child);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        self.0.wait().unwrap();
    }
}
fn http(address: &str, request: &str) -> Value {
    let mut stream = std::net::TcpStream::connect(address).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .unwrap();
    stream.write_all(request.as_bytes()).unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    assert!(response.starts_with("HTTP/1.1 200"), "{response}");
    serde_json::from_str(response.split_once("\r\n\r\n").unwrap().1).unwrap()
}
#[test]
fn explorer_refreshes_each_index_request() {
    let t = fixture();
    let source = t
        .path()
        .join("home/.pi/agent/sessions/project/freshness-session.jsonl");
    let mut server = Server(
        command(t.path())
            .args(["serve", "--port", "0"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let mut output = BufReader::new(server.0.stdout.take().unwrap());
    let mut line = String::new();
    assert!(output.read_line(&mut line).unwrap() > 0);
    let (address, bootstrap) = line
        .trim()
        .strip_prefix("agrep explorer: http://")
        .unwrap()
        .split_once("/#bootstrap=")
        .unwrap();
    let auth = http(address, &format!("POST /auth HTTP/1.1\r\nHost: {address}\r\nOrigin: http://{address}\r\nX-Agrep-Bootstrap: {bootstrap}\r\nContent-Length: 0\r\n\r\n"));
    let token = auth["token"].as_str().unwrap();
    for (i, endpoint) in [
        "/api/search?q=cobaltneedle",
        "/api/around?session=freshness-session&turn=3&radius=12",
        "/api/chats",
        "/api/status",
    ]
    .iter()
    .enumerate()
    {
        let parent = if i == 0 {
            "initial".into()
        } else {
            format!("http{}", i - 1)
        };
        append(
            &source,
            &format!("http{i}"),
            Some(&parent),
            &format!("cobaltneedle{i}"),
        );
        let body = http(address, &format!("GET {endpoint} HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\n\r\n"));
        assert!(!body.to_string().contains("pending_sources"));
        if i < 2 {
            assert!(
                body.to_string().contains(&format!("cobaltneedle{i}")),
                "{body}"
            );
        }
        let probe = command(t.path())
            .args([&format!("cobaltneedle{i}"), "-c", "--no-auto"])
            .output()
            .unwrap();
        assert!(probe.status.success(), "{probe:?}");
        assert_eq!(String::from_utf8(probe.stdout).unwrap().trim(), "1");
        if i == 3 {
            assert_eq!(body["messages"], 5);
        }
    }
}

#[test]
fn continuation_retains_its_first_page_when_sources_change() {
    use agrep_core::pages;
    let t = fixture();
    let root = DataRoot(t.path().join("data"));
    let frozen = pages::Frozen {
        records: vec![pages::Record {
            line: "saved first-page result".into(),
            family: "family".into(),
            score: 1.,
            lane: "phrase".into(),
            tool: false,
            identity: "row".into(),
        }],
        query: "original".into(),
        total: 1,
        exact: true,
        shown: 0,
        requested: None,
        chat_scoped: false,
        full_argv: vec![],
        exhaustible: true,
    };
    let handle = pages::save(&root, &frozen).unwrap().unwrap();
    let source = t
        .path()
        .join("home/.pi/agent/sessions/project/freshness-session.jsonl");
    append(&source, "late", Some("initial"), "cobaltneedle");
    let text = checked(
        command(t.path())
            .args(["--more", &handle])
            .output()
            .unwrap(),
    );
    assert!(text.contains("saved first-page result"));
    assert!(!text.contains("cobaltneedle"));
    let check: Value =
        serde_json::from_slice(&command(t.path()).arg("__census").output().unwrap().stdout)
            .unwrap();
    assert_eq!(check["changed_sources"], 1);
}
