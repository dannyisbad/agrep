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
/// A user text, its reply, and an optional shell command with its output.
type Turn<'a> = (&'a str, &'a str, Option<(&'a str, &'a str)>);
/// One pi transcript of `turns`.
fn pi_chat(home: &Path, id: &str, day: u32, cwd: &str, turns: &[Turn]) {
    let dir = home
        .join(".pi/agent/sessions")
        .join(cwd.trim_start_matches('/').replace('/', "-"));
    fs::create_dir_all(&dir).unwrap();
    let ts = |n: usize| format!("2026-10-{day:02}T12:00:{n:02}.000Z");
    let mut lines =
        vec![serde_json::json!({"type":"session","id":id,"version":3,"cwd":cwd,"timestamp":ts(0)})];
    let (mut parent, mut n) = (Value::Null, 0);
    let mut add = |id: String, message: Value, parent: &mut Value| {
        n += 1;
        let mut message = message;
        message["timestamp"] = ts(n).into();
        lines.push(serde_json::json!({"type":"message","id":id,"parentId":parent.clone(),"timestamp":ts(n),"message":message}));
        *parent = Value::String(id);
    };
    for (i, (user, reply, tool)) in turns.iter().enumerate() {
        add(
            format!("u{i}"),
            serde_json::json!({"role":"user","content":[{"type":"text","text":user}]}),
            &mut parent,
        );
        let mut content = vec![];
        if !reply.is_empty() {
            content.push(serde_json::json!({"type":"text","text":reply}));
        }
        if let Some((command, _)) = tool {
            content.push(serde_json::json!({"type":"toolCall","id":format!("tc{i}"),"name":"bash","arguments":{"command":command}}));
        }
        if !content.is_empty() {
            add(
                format!("a{i}"),
                serde_json::json!({"role":"assistant","model":"pi/sonnet-5","provider":"anthropic","stopReason":"end_turn","content":content}),
                &mut parent,
            );
        }
        if let Some((_, output)) = tool {
            add(
                format!("r{i}"),
                serde_json::json!({"role":"toolResult","toolCallId":format!("tc{i}"),"toolName":"bash","isError":false,"content":[{"type":"text","text":output}]}),
                &mut parent,
            );
        }
    }
    let body: Vec<String> = lines.iter().map(Value::to_string).collect();
    fs::write(
        dir.join(format!("2026-10-{day:02}T12-00-00-000Z_{id}.jsonl")),
        body.join("\n") + "\n",
    )
    .unwrap();
}
// Expected values are agrep 0.4.9's output on this fixture. It counted a listed chat's
// rows in that exact session (`session IN (...)`), never in the chats whose ids extend
// it, while `--chat` stays a prefix filter.
#[test]
fn chats_count_each_listed_session_exactly_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    let a = "01960000-0000-7000-8000-000000000001";
    let fork = format!("{a}-fork");
    let other = "01960000-0000-7000-8000-000000000003";
    pi_chat(
        &h,
        a,
        20,
        "/work/alpha",
        &[
            (
                "where is the lantern",
                "the copper lantern is in the shed",
                None,
            ),
            ("copper pipes only", "lantern wick trimmed", None),
            ("check it", "", Some(("ls lantern", "copper lantern.txt"))),
        ],
    );
    pi_chat(
        &h,
        &fork,
        21,
        "/work/beta",
        &[
            ("lantern copy one", "copper lantern copy", None),
            ("lantern copy two", "lantern copy three", None),
            ("copper lantern again", "a copper lantern", None),
        ],
    );
    pi_chat(
        &h,
        other,
        22,
        "/work/gamma",
        &[("one more lantern", "nothing copper here", None)],
    );
    ok(cli(&h, &d, &["index"], &[]));
    for (args, expected) in [
        (
            vec!["lantern"],
            vec![(a, 4), (fork.as_str(), 6), (other, 1)],
        ),
        (vec!["copper lantern"], vec![(a, 2), (fork.as_str(), 3)]),
        (
            vec!["-E", "cop+er"],
            vec![(a, 3), (fork.as_str(), 3), (other, 1)],
        ),
    ] {
        let mut argv = args.clone();
        argv.extend(["--chats", "--json", "--no-auto"]);
        let rows = values(&ok(cli(&h, &d, &argv, &[])));
        let mut found: Vec<(String, u64)> = rows[1..]
            .iter()
            .map(|r| {
                (
                    r["session"].as_str().unwrap().to_owned(),
                    r["session_hits"].as_u64().unwrap(),
                )
            })
            .collect();
        found.sort();
        let mut expected: Vec<(String, u64)> = expected
            .into_iter()
            .map(|(s, n)| (s.to_owned(), n))
            .collect();
        expected.sort();
        assert_eq!(found, expected, "{args:?}");
    }
    for (pattern, expected) in [("lantern", "10\n"), ("copper lantern", "5\n")] {
        let o = ok(cli(&h, &d, &[pattern, "--chat", a, "-c", "--no-auto"], &[]));
        assert_eq!(String::from_utf8(o.stdout).unwrap(), expected);
    }
}
// 0.4.9's status corpus row gave the last refresh as an age ("updated just now" under a
// minute), its footprint row the data size, and its instructions row the setup nudge.
#[test]
fn status_reports_refresh_age_and_footprint_as_0_4_9() {
    let (_t, h, d) = pi();
    let text = String::from_utf8(ok(cli(&h, &d, &["status"], &[])).stdout).unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(
        lines[1],
        "5 sessions · 11 prose rows · 2 tool rows · updated just now"
    );
    assert!(
        lines[2].starts_with("footprint: ") && lines[2].contains(" MiB across "),
        "{text}"
    );
    assert!(lines.contains(
        &"instructions: not installed - `agrep setup` teaches your agents to search this history"
    ));
    let v: Value =
        serde_json::from_slice(&ok(cli(&h, &d, &["status", "--json"], &[])).stdout).unwrap();
    assert!(v["index"]["last_refresh"].as_i64().unwrap() > 0);
    assert!(v["last_indexed_age_s"].as_i64().unwrap() < 60);
    assert!(v["disk_files"].as_u64().unwrap() > 0);
}
// Expected values are agrep 0.4.9's output on this fixture: one head line per matching
// chat with its matching rows (`_emit_chats`), `session_hits` in JSON, and the
// freshness-story line for an absence read with --no-auto.
#[test]
fn chats_listing_and_no_auto_absence_match_0_4_9() {
    let (_t, h, d) = pi();
    for (query, handle) in [
        ("retry", "@01930000:2.18ba"),
        ("backoff", "@01930000:0.cd22"),
    ] {
        let o = ok(cli(&h, &d, &[query, "--chats"], &[]));
        let line = String::from_utf8(o.stdout).unwrap();
        let prefix = format!("pi · alpha  [{handle} · 3 hits · ");
        assert!(
            line.starts_with(&prefix) && line.ends_with("d]\n"),
            "{line}"
        );
        assert_eq!(line.lines().count(), 1);
    }
    let rows = values(&ok(cli(&h, &d, &["retry", "--chats", "--json"], &[])));
    assert_eq!(rows[1]["session"], "01930000-0000-7000-8000-000000000001");
    assert_eq!(rows[1]["session_hits"], 3);
    let stale = "history may be stale: automatic freshness checks are disabled by --no-auto";
    let o = cli(&h, &d, &["zzzqqq", "--no-auto"], &[]);
    assert_eq!(o.status.code(), Some(2));
    assert_eq!(
        String::from_utf8(o.stderr).unwrap().lines().last(),
        Some(stale)
    );
    let o = cli(&h, &d, &["recall", "zzzqqq", "--lexical", "--no-auto"], &[]);
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        format!("no hits across 5 indexed sessions.\n{stale}\n")
    );
    let o = cli(
        &h,
        &d,
        &["recall", "zzzqqq", "--lexical", "--probe", "--no-auto"],
        &[],
    );
    assert_eq!(
        String::from_utf8(o.stdout).unwrap(),
        "recall: no confident match in 5 past conversations\n"
    );
    assert_eq!(String::from_utf8(o.stderr).unwrap(), format!("{stale}\n"));
}
// Expected text is agrep 0.4.9's on this fixture: the empty-search line counts one
// message per user-side row and names its matcher, chats lists short handles and
// project leaves, and summary prints one footer line.
#[test]
fn empty_search_chats_and_summary_lines_match_0_4_9() {
    let (_t, h, d) = pi();
    let o = cli(&h, &d, &["zzzqqq"], &[]);
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "keyword: 0 matching rows across 8 indexed messages\n"
    );
    let o = cli(&h, &d, &["zzzqqq", "-w"], &[]);
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "word: 0 matching rows across 8 indexed messages - `-s` runs semantic search\n"
    );
    let o = ok(cli(&h, &d, &["chats"], &[]));
    let lines: Vec<String> = String::from_utf8(o.stdout)
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect();
    assert!(
        lines[0].starts_with("@01950000 pi upstream "),
        "{}",
        lines[0]
    );
    assert!(lines[0].ends_with("· agrep around @01950000:1.2e22"));
    assert!(lines[1].starts_with("@01940000-0000-7000-8000-000000000003 pi beta "));
    assert!(lines[3].starts_with("@01920000 pi legacy 1y 1t  legacy linear question"));
    let o = ok(cli(&h, &d, &["chats", "retry"], &[]));
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "showing 1 of 1 matching chat, best match first (side chats hidden; --side shows them)\n"
    );
    let o = cli(&h, &d, &["summary"], &[]);
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "0 projects, 0 chats in the last 7d, most active first · estimated active time with a 20m idle cap, side chats folded into their family\n"
    );
}
// tests/golden/pi-recall.json is agrep 0.4.9's `recall retry --lexical --json` on this
// fixture, scores rounded to four decimals. 0.5 names its engine `sqlite`, has no
// publication age, and adds each hit's handle and `self` bit (PORTING.md).
#[test]
fn recall_json_matches_0_4_9_golden() {
    let (_t, h, d) = pi();
    let o = ok(cli(
        &h,
        &d,
        &["recall", "retry", "--lexical", "--json"],
        &[],
    ));
    let mut current: Value = serde_json::from_slice(&o.stdout).unwrap();
    let mut expected: Value = serde_json::from_str(
        &fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden/pi-recall.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(expected["engine"], "corpusdb");
    assert_eq!(current["engine"], "sqlite");
    for key in ["engine", "corpus_age_s"] {
        expected.as_object_mut().unwrap().remove(key);
        current.as_object_mut().unwrap().remove(key);
    }
    for hit in current["hits"].as_array_mut().unwrap() {
        let hit = hit.as_object_mut().unwrap();
        assert!(hit.remove("handle").is_some() && hit.remove("self").is_some());
    }
    assert_eq!(current, expected);
    let o = ok(cli(
        &h,
        &d,
        &["recall", "jitter retry", "--lexical", "--json"],
        &[],
    ));
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(
        v["note"],
        "no exact phrase match; showing hits containing all terms"
    );
}
// Expected output is agrep 0.4.9's: a direct handle into the caller's window is withheld
// and counted, and a handle before it shows only the turns before the window.
#[test]
fn recall_hides_a_direct_handle_into_the_callers_window() {
    let (_t, h, d) = pi();
    let caller = [("CODEX_THREAD_ID", "01930000-0000-7000-8000-000000000001")];
    let o = cli(&h, &d, &["recall", "@01930000:2.18ba"], &caller);
    assert_eq!(o.status.code(), Some(1));
    assert!(o.stdout.is_empty());
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "no hits across 5 indexed sessions.\nexcluded 1 hit from the current window\n"
    );
    let o = cli(&h, &d, &["recall", "@01930000:2.18ba", "--json"], &caller);
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(v["engine"], "handle");
    assert_eq!(v["semantic_status"]["reason"], "handle-lookup");
    assert_eq!(v["hits"].as_array().unwrap().len(), 0);
    assert_eq!(v["self_exclusion"]["excluded_hits"], 1);
    assert_eq!(v["self_exclusion"]["excluded_hits_known"], true);
    let local = chrono::DateTime::parse_from_rfc3339("2026-01-02T03:04:00Z")
        .unwrap()
        .with_timezone(&chrono::Local)
        .format("%Y-%m-%d %H:%M")
        .to_string();
    let o = ok(cli(&h, &d, &["recall", "@01930000:0.cd22"], &caller));
    assert_eq!(
        String::from_utf8(o.stdout).unwrap(),
        format!("── @01930000:0.cd22 · pi · alpha · {local} · ~self\n     0 user: why did the retry backoff cap at 30s\n     0 agent: the 60s ceiling tripped the account throttle\n       [+1 tool calls - agrep around 01930000 0 -C 0 --tool-output 200]\n")
    );
}
// Expected lines are agrep 0.4.9's stderr for an agent shell that names no session.
#[test]
fn unknown_caller_no_self_notices_match_0_4_9() {
    let (_t, h, d) = pi();
    let unknown = [("CODEX_SANDBOX", "seatbelt")];
    let session = "--no-self was not applied: could not identify this session - results may include the current conversation";
    let caller = "--no-self was not applied: caller identity is unknown; no session was excluded";
    for (args, line) in [
        (vec!["retry", "--no-self"], session),
        (vec!["chats", "retry", "--no-self"], session),
        (vec!["summary", "--no-self"], session),
        (vec!["recall", "retry", "--no-self", "--lexical"], caller),
        (vec!["pack", "retry", "jitter", "--no-self"], caller),
    ] {
        let o = cli(&h, &d, &args, &unknown);
        let stderr = String::from_utf8(o.stderr).unwrap();
        assert_eq!(stderr.lines().next(), Some(line), "{args:?}");
        assert_eq!(stderr.matches("not applied").count(), 1, "{args:?}");
    }
}
// Expected values are agrep 0.4.9's output on this fixture and caller.
#[test]
fn caller_window_counts_withheld_hits_as_0_4_9() {
    let (_t, h, d) = pi();
    let caller = [("CODEX_THREAD_ID", "01930000-0000-7000-8000-000000000001")];
    let rows = values(&ok(cli(&h, &d, &["retry", "--json"], &caller)));
    assert_eq!(
        agrep_core::json_output::to_string(&rows[0]["self_exclusion"]).unwrap(),
        r#"{"active":true,"reason":"window","scope":"current-window","session":"01930000-0000-7000-8000-000000000001","excluded_hits":1,"excluded_hits_known":true,"identity":"codex","from_turn":1}"#
    );
    let flat = ok(cli(&h, &d, &["retry", "--flat"], &caller));
    assert!(String::from_utf8(flat.stderr)
        .unwrap()
        .lines()
        .any(|l| l == "excluded 1 hit from the current window"));
    let recall = ok(cli(
        &h,
        &d,
        &["recall", "retry", "--lexical", "--json"],
        &caller,
    ));
    let recall: Value = serde_json::from_slice(&recall.stdout).unwrap();
    assert_eq!(recall["self_exclusion"]["excluded_hits"], 1);
    let pack = ok(cli(&h, &d, &["pack", "retry", "jitter", "--json"], &caller));
    let pack: Value = serde_json::from_slice(&pack.stdout).unwrap();
    assert_eq!(pack["self_exclusion"]["excluded_hits"], Value::Null);
    assert_eq!(pack["self_exclusion"]["excluded_hits_known"], false);
    let tool = values(&ok(cli(
        &h,
        &d,
        &["backoff", "--who", "tool", "--json"],
        &caller,
    )));
    assert_eq!(tool[0]["self_exclusion"]["excluded_hits"], 0);
    let plain = values(&ok(cli(&h, &d, &["retry", "--json"], &[])));
    assert_eq!(
        agrep_core::json_output::to_string(&plain[0]["self_exclusion"]).unwrap(),
        r#"{"active":false,"reason":"machine-surface"}"#
    );
}
// 0.4.9 flagged event output truncated only past the 800-character ingest cap.
#[test]
fn around_flags_truncation_only_past_the_ingest_cap() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let dir = h.join(".pi/agent/sessions/work-cap");
    fs::create_dir_all(&dir).unwrap();
    let long = "y".repeat(900);
    let lines = [
        serde_json::json!({"type":"session","id":"0aaa0000-0000-7000-8000-000000000001","version":3,"cwd":"/work/cap","timestamp":"2026-10-01T12:00:00.000Z"}),
        serde_json::json!({"type":"message","id":"u0","parentId":null,"timestamp":"2026-10-01T12:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"show the capfixture output"}]}}),
        serde_json::json!({"type":"message","id":"a0","parentId":"u0","timestamp":"2026-10-01T12:00:02.000Z","message":{"role":"assistant","model":"m","provider":"p","stopReason":"end_turn","content":[{"type":"toolCall","id":"t1","name":"bash","arguments":{"command":"echo capfixture"}},{"type":"toolCall","id":"t2","name":"bash","arguments":{"command":"echo long"}}]}}),
        serde_json::json!({"type":"message","id":"r1","parentId":"a0","timestamp":"2026-10-01T12:00:03.000Z","message":{"role":"toolResult","toolCallId":"t1","toolName":"bash","isError":false,"content":[{"type":"text","text":"capfixture done\n"}]}}),
        serde_json::json!({"type":"message","id":"r2","parentId":"r1","timestamp":"2026-10-01T12:00:04.000Z","message":{"role":"toolResult","toolCallId":"t2","toolName":"bash","isError":false,"content":[{"type":"text","text":long}]}}),
    ];
    fs::write(
        dir.join("2026-10-01T12-00-00-000Z_0aaa0000-0000-7000-8000-000000000001.jsonl"),
        lines.map(|l| l.to_string()).join("\n") + "\n",
    )
    .unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    let rows = values(&ok(cli(
        &h,
        &d,
        &["around", "0aaa0000", "0", "--json", "--full"],
        &[],
    )));
    let events: Vec<_> = rows.iter().filter(|r| r["kind"] == "tool").collect();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["output_truncated"], false);
    assert_eq!(events[1]["output_chars"], 900);
    assert_eq!(events[1]["output_truncated"], true);
    assert_eq!(rows[0]["scope"]["truncation"]["source_truncated_events"], 1);
}
// Expected pages are agrep 0.4.9's output on this fixture: `_tool_line` collapses and caps
// inputs and previews the first nonblank output line, `_tool_rows` folds a run of
// successful calls, `_marker_line` draws an API error, and repeated calls all show.
#[test]
fn around_tool_lines_match_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    copy(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/around_tools/home"),
        &h,
    );
    ok(cli(&h, &d, &["index"], &[]));
    let local = chrono::DateTime::parse_from_rfc3339("2026-10-01T12:00:00Z")
        .unwrap()
        .with_timezone(&chrono::Local)
        .format("%Y-%m-%d %H:%M")
        .to_string();
    for (name, args) in [
        (
            "around-tools-full",
            vec!["around", "0fff0000", "0", "--full"],
        ),
        (
            "around-tools-repeated",
            vec!["around", "0fff0000", "1", "-C", "0", "--full"],
        ),
        (
            "around-tools-collapsed",
            vec!["around", "0fff0000", "0", "--who", "tool"],
        ),
        (
            "around-tools-output",
            vec![
                "around",
                "0fff0000",
                "0",
                "--tool-output",
                "30",
                "--color",
                "always",
            ],
        ),
        (
            "around-marker",
            vec!["around", "33333333", "--whole", "--full"],
        ),
        (
            "around-marker-omitted",
            vec!["around", "33333333", "--whole"],
        ),
    ] {
        let o = ok(cli(&h, &d, &args, &[]));
        let expected = fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("tests/golden/{name}.txt")),
        )
        .unwrap();
        assert_eq!(
            String::from_utf8(o.stdout).unwrap(),
            expected.replace("2026-10-01 12:00", &local),
            "{name}"
        );
    }
    let rows = values(&ok(cli(
        &h,
        &d,
        &["around", "33333333", "--whole", "--full", "--json"],
        &[],
    )));
    assert_eq!(
        agrep_core::json_output::to_string(&rows[0]["scope"]["markers"]).unwrap(),
        r#"{"available_by_name":{"api_error":1},"shown_by_name":{"api_error":1},"hidden":0}"#
    );
    assert_eq!(rows[0]["scope"]["tools"]["available"], 0);
    let control = rows.iter().find(|r| r["kind"] == "control").unwrap();
    assert_eq!(
        control["input"],
        format!(
            "API Error: 529   overloaded_error\nplease retry {}",
            "later ".repeat(25).trim_end()
        )
    );
    let miss = cli(
        &h,
        &d,
        &["around", "0fff0000", "1", "-C", "0", "--who", "subagent"],
        &[],
    );
    assert_eq!(miss.status.code(), Some(1));
    assert!(miss.stdout.is_empty());
    assert_eq!(
        String::from_utf8(miss.stderr).unwrap(),
        "no subagent messages or events in turns 1-1 of 0fff0000\n"
    );
}
// Expected rows are agrep 0.4.9's output: snippets keep control characters for the
// sanitizer to quote, and the compact page narrows each to 32 characters around its match.
#[test]
fn snippets_quote_controls_and_compact_narrows_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    let dir = h.join(".pi/agent/sessions/work-project");
    fs::create_dir_all(&dir).unwrap();
    let row = |id: &str, parent: Option<&str>, n: u32, role: &str, text: &str| serde_json::json!({"type":"message","id":id,"parentId":parent,"timestamp":format!("2026-10-04T12:00:{n:02}.000Z"),"message":{"role":role,"model":"m","provider":"p","content":[{"type":"text","text":text}]}});
    let lines = [
        serde_json::json!({"type":"session","id":"0ccc0000-0000-7000-8000-000000000001","version":3,"cwd":"/work/project","timestamp":"2026-10-04T12:00:00.000Z"}),
        row("u0", None, 1, "user", "find the zebrafish \x1b[31mred\x1b[0m marker\x07 and \x00nul \x7fdel \u{85}nel \u{200b}zw \u{202e}bidi\ttab\x0bvt\x1cfs end"),
        row("a0", Some("u0"), 2, "assistant", "ok"),
        row("u1", Some("a0"), 3, "user", &format!("zebrafish {}\x1b[32m tail zebrafish", "pad ".repeat(40))),
        row("a1", Some("u1"), 4, "assistant", "ok"),
    ];
    fs::write(
        dir.join("2026-10-04T12-00-00-000Z_0ccc0000-0000-7000-8000-000000000001.jsonl"),
        lines.map(|l| l.to_string()).join("\n") + "\n",
    )
    .unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    let controls = r"find the zebrafish \u001b[31mred\u001b[0m marker\u0007 and \u0000nul \u007f";
    let flat = ok(cli(&h, &d, &["zebrafish", "--flat"], &[]));
    assert_eq!(
        String::from_utf8(flat.stdout).unwrap(),
        format!(
            "0ccc0000-0000-7000-8000-000000000001\tpi\tproject\t1\tuser\tzebrafish {}…\n0ccc0000-0000-7000-8000-000000000001\tpi\tproject\t0\tuser\t{controls}del nel \\u200bzw \\u202ebidi tab vt fs end\n",
            "pad ".repeat(20).trim_end()
        )
    );
    let compact = ok(cli(
        &h,
        &d,
        &["zebrafish", "--lexical"],
        &[("AGREP_PROFILE", "compact")],
    ));
    let snippets: Vec<String> = String::from_utf8(compact.stdout)
        .unwrap()
        .lines()
        .map(|l| l.splitn(5, ' ').nth(4).unwrap().to_owned())
        .collect();
    assert_eq!(
        snippets,
        [
            format!("zebrafish {}…", "pad ".repeat(8).trim_end()),
            format!("{controls}…"),
        ]
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
        exhaustible: true,
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
