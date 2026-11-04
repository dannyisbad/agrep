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
fn command(home: &Path, data: &Path, args: &[&str], env: &[(&str, &str)]) -> Command {
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
    cmd
}
fn cli(home: &Path, data: &Path, args: &[&str], env: &[(&str, &str)]) -> Output {
    command(home, data, args, env).output().unwrap()
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
// Expected values are agrep 0.4.9's output on a home without tool rows
// (py/surface_policy.py:843-848, 896-910).
#[test]
fn empty_filter_dimensions_are_named_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    let dir = h.join(".pi/agent/sessions/-work-plain");
    fs::create_dir_all(&dir).unwrap();
    let id = "01970000-0000-7000-8000-000000000001";
    let lines = [
        serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/plain","timestamp":"2026-01-02T03:04:05.000Z"}),
        serde_json::json!({"type":"message","id":"u1","parentId":null,"timestamp":"2026-01-02T03:04:06.000Z","message":{"role":"user","content":[{"type":"text","text":"why did the retry backoff cap"}]}}),
        serde_json::json!({"type":"message","id":"a1","parentId":"u1","timestamp":"2026-01-02T03:04:07.000Z","message":{"role":"assistant","content":[{"type":"text","text":"the retry cap is 30s"}]}}),
    ];
    fs::write(
        dir.join(format!("2026-01-02T03-04-05-000Z_{id}.jsonl")),
        lines.iter().map(|l| format!("{l}\n")).collect::<String>(),
    )
    .unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    let json = cli(&h, &d, &["retry", "--who", "tool", "--json"], &[]);
    assert_eq!(json.status.code(), Some(1));
    assert_eq!(
        values(&json)[0]["filter_coverage"]["empty_dimensions"],
        serde_json::json!([{"flag":"--who","dimension":"who","value":"tool","indexed_values":2,
            "known":["agent","user"],"known_complete":true}])
    );
    let stderr = |args: &[&str]| String::from_utf8(cli(&h, &d, args, &[]).stderr).unwrap();
    assert_eq!(
        stderr(&["retry", "--who", "tool"]),
        "--who tool isn't in the index - indexed speakers: agent, user\n"
    );
    assert_eq!(
        stderr(&["retry", "--project", "absent"]),
        "--project absent isn't in the index - indexed projects: /work/plain\n"
    );
    assert_eq!(
        stderr(&["recall", "retry", "--who", "tool"]),
        "no hits across 1 indexed session.\n--who tool isn't in the index - indexed speakers: agent, user\n"
    );
}
// Expected values are agrep 0.4.9's `summary --since 2025-01-01` output over the pi, claude
// and codex fixtures (py/summary.py:2099-2404).
#[test]
fn summary_briefs_families_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    for name in ["pi", "claude", "codex"] {
        copy(
            &Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("tests/fixtures/{name}/home")),
            &h,
        );
    }
    ok(cli(&h, &d, &["index"], &[]));
    let utc = [("TZ", "UTC")];
    let rows = values(&ok(cli(
        &h,
        &d,
        &["summary", "--since", "2025-01-01", "--json"],
        &utc,
    )));
    let meta = &rows[0];
    assert_eq!(meta["mode"], "briefing");
    assert_eq!(
        (meta["chats"].as_u64(), meta["side_chats"].as_u64()),
        (Some(9), Some(3))
    );
    assert_eq!(meta["totals_exact"], true);
    assert_eq!(meta["events_unavailable_chats"], 0);
    assert_eq!(meta["window"]["until_ts"], Value::Null);
    assert!(meta.get("total_estimated_active_ms").is_none());
    // `NAME (UTC+HH:MM)`; under TZ=UTC 0.4.9 printed `UTC (UTC+00:00)`.
    let zone = meta["timezone"].as_str().unwrap();
    let offset = &zone[zone.rfind(" (UTC").unwrap() + 5..];
    assert!(offset.len() == 7 && offset.ends_with(')') && offset.as_bytes()[3] == b':');
    let beta = &rows[1];
    assert_eq!(beta["project"], "beta");
    assert_eq!(
        beta["project_labels"],
        serde_json::json!(["/work/beta", "beta"])
    );
    assert!(beta["last_age"].is_string());
    let worked = &beta["worked_on"];
    assert_eq!(
        worked[0]["latest_handle"],
        "@01940000-0000-7000-8000-000000000003:0.03a3"
    );
    assert_eq!(worked[0]["side_chats"], 1);
    assert_eq!(worked[1]["parent"], "01990003-0001-7000-8000-000000000001");
    assert_eq!(worked[1]["parent_indexed"], false);
    assert_eq!(worked[2]["session_handle"], "@22222222");
    assert_eq!(worked[2]["latest_handle"], "@22222222:1.10d4");
    let open = &beta["open"][0];
    assert_eq!(
        open["signals"],
        serde_json::json!(["side chat has no captured reply to its last turn"])
    );
    assert!(open.get("caveats").is_none());
    let text = ok(cli(&h, &d, &["summary", "--since", "2025-01-01"], &utc));
    let out = String::from_utf8(text.stdout).unwrap();
    let lines: Vec<&str> = out.lines().collect();
    assert!(
        lines[0].starts_with("beta  ·  estimated active time 3m  ·  3 chats  ·  5 turns  ·  last ")
    );
    assert_eq!(lines[1], "  worked on:");
    assert!(lines[2].starts_with("    @01940000-0000-7000-8000-000000000003 pi "));
    assert!(lines[2].ends_with(" 1t (+1 side)  fork question about the omp panel · agrep around @01940000-0000-7000-8000-000000000003:0.03a3"));
    assert!(lines[3].contains(" 2t [side chat; parent not indexed]  [subagent task]"));
    assert_eq!(lines[5], "  open:");
    assert_eq!(lines[6], "    MEDIUM agent_work_incomplete @01940000-0000-7000-8000-000000000004:0.d74c  [side chat]  side chat has no captured reply to its last turn");
    assert_eq!(
        String::from_utf8(text.stderr).unwrap(),
        "4 projects, 9 chats since 2025-01-01, most active first · estimated active time with a 20m idle cap, side chats folded into their family\n"
    );
    let time = ok(cli(
        &h,
        &d,
        &["summary", "time", "--since", "2025-01-01"],
        &utc,
    ));
    let footer = String::from_utf8(time.stderr).unwrap();
    assert!(footer.starts_with(
        "estimated active time for 9 chats since 2025-01-01, idle cap 20m, local time "
    ));
    assert!(footer.ends_with(" · not elapsed span, not billable\n"));
    assert!(String::from_utf8(time.stdout)
        .unwrap()
        .starts_with("day         project      time  chats\n"));
}
// Expected values are agrep 0.4.9's `chats` output on this fixture (py/search.py:8862-8912,
// 8970-8987).
#[test]
fn chats_json_names_chats_by_shortest_prefix_as_0_4_9() {
    let (_t, h, d) = pi();
    let rows = values(&ok(cli(&h, &d, &["chats", "-n", "2", "--json"], &[])));
    assert_eq!(
        rows[0]["completeness"],
        serde_json::json!({"shown":2,"total":4,"total_basis":"exact","unit":"matching chat",
            "truncated":true,"more_command":"agrep chats --json -n 80"})
    );
    assert_eq!(
        rows[0]["filter_coverage"],
        serde_json::json!({"checked":true,"empty_dimensions":[]})
    );
    assert!(rows[0].get("engine").is_none() && rows[0].get("filters").is_none());
    assert_eq!(rows[1]["session_handle"], "@01950000");
    assert_eq!(rows[1]["latest_handle"], "@01950000:1.2e22");
    assert!(rows[1].get("self").is_none());
    assert_eq!(
        rows[2]["session_handle"],
        "@01940000-0000-7000-8000-000000000003"
    );
    let text = ok(cli(&h, &d, &["chats", "-n", "2"], &[]));
    assert!(String::from_utf8_lossy(&text.stderr).starts_with(
        "showing 2 of 4 matching chats, newest first (side chats hidden; --side shows them) · larger page: agrep chats -n 80\n"
    ));
    let content = values(&ok(cli(&h, &d, &["chats", "retry", "--json"], &[])));
    assert_eq!(content[1]["match_handle"], "@01930000:2.18ba");
    assert!(content[1].get("matched").is_none());
    let absent = cli(&h, &d, &["chats", "--project", "absent", "--json"], &[]);
    assert_eq!(absent.status.code(), Some(1));
    assert_eq!(
        values(&absent)[0]["filter_coverage"]["empty_dimensions"][0]["known"],
        serde_json::json!([
            "/work/alpha",
            "/work/beta",
            "/work/legacy",
            "/work/upstream"
        ])
    );
    // Full ids still resolve wherever a prefix does.
    let full = values(&ok(cli(
        &h,
        &d,
        &["chats", "01950000-0000-7000-8000-000000000005", "--json"],
        &[],
    )));
    assert_eq!(full[1]["session_handle"], "@01950000");
    ok(cli(
        &h,
        &d,
        &["around", "@01950000-0000-7000-8000-000000000005:1.2e22"],
        &[],
    ));
}
// 0.4.9 bounded a chat by its timed turns alone (agrep-core cache.rs `aggregate_sessions`):
// expected values are its sessions.jsonl golden for the codex fixture and its chats output.
#[test]
fn chat_bounds_ignore_tool_calls_and_untimed_turns_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let bounds = |h: &Path, d: &Path, session: &str| {
        let rows = values(&ok(cli(h, d, &["chats", "--side", "--json"], &[])));
        let row = rows
            .iter()
            .find(|r| r["session"] == session)
            .unwrap_or_else(|| panic!("{session} is not listed"));
        (
            row["first_ts"].as_i64().unwrap(),
            row["last_ts"].as_i64().unwrap(),
        )
    };
    let (h, d) = (t.path().join("codex"), t.path().join("codex-data"));
    copy(&fixtures.join("codex/home"), &h);
    ok(cli(&h, &d, &["index"], &[]));
    assert_eq!(
        bounds(&h, &d, "01990001-0002-7000-8000-000000000002"),
        (1767358800000, 1767358800000)
    );
    let rollout = h.join(".codex/sessions/2026/01/02/rollout-2026-01-02T10-00-00-22222222-2222-4222-8222-222222222222.jsonl");
    let mut body = fs::read_to_string(&rollout).unwrap();
    body.push_str(concat!(
        r#"{"type":"response_item","timestamp":"2026-01-02T10:05:00.000Z","payload":{"type":"function_call","name":"shell","arguments":"{\"command\":\"late call\"}","call_id":"late_call","status":"completed"}}"#,
        "\n",
        r#"{"type":"response_item","timestamp":"2026-01-02T10:05:01.000Z","payload":{"type":"function_call_output","call_id":"late_call","output":"late output"}}"#,
        "\n"
    ));
    fs::write(&rollout, body).unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    assert_eq!(
        bounds(&h, &d, "22222222-2222-4222-8222-222222222222"),
        (1767348001000, 1767348060000)
    );
    let (h, d) = (t.path().join("tools"), t.path().join("tools-data"));
    copy(&fixtures.join("around_tools/home"), &h);
    let untimed = [
        r#"{"type":"session","id":"0eee0000-0000-7000-8000-000000000002","version":3,"cwd":"/work/tools"}"#,
        r#"{"type":"message","id":"u0","parentId":null,"message":{"role":"user","content":[{"type":"text","text":"first prompt has no clock"}]}}"#,
        r#"{"type":"message","id":"a0","parentId":"u0","message":{"role":"assistant","model":"m","provider":"p","stopReason":"end_turn","content":[{"type":"text","text":"first answer"}]}}"#,
        r#"{"type":"message","id":"u1","parentId":"a0","timestamp":"2026-10-02T12:00:05.000Z","message":{"role":"user","content":[{"type":"text","text":"second prompt has a clock"}]}}"#,
        r#"{"type":"message","id":"a1","parentId":"u1","timestamp":"2026-10-02T12:00:06.000Z","message":{"role":"assistant","model":"m","provider":"p","stopReason":"end_turn","content":[{"type":"text","text":"second answer"}]}}"#,
    ];
    fs::write(
        h.join(".pi/agent/sessions/-work-tools/2026-10-02T12-00-00-000Z_0eee0000-0000-7000-8000-000000000002.jsonl"),
        untimed.join("\n") + "\n",
    )
    .unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    assert_eq!(
        bounds(&h, &d, "0fff0000-0000-7000-8000-000000000001"),
        (1790856001000, 1790856020000)
    );
    assert_eq!(
        bounds(&h, &d, "0eee0000-0000-7000-8000-000000000002"),
        (1790942405000, 1790942405000)
    );
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
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "handle digest matches turn 3, not turn 0 - either the session was renumbered since \
         the handle was minted, or the digest is wrong; verify the content before citing it\n"
    );
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
// Expected chats are agrep 0.4.9's `chats PATTERN` on this two-chat home: the identity
// haystack holds the agent and the project's display name (py/search.py:8579-8583), so
// "codex" finds a Codex chat whose words never say it, and the agent, display name and
// stored project sit side by side ("pi alpha", "beta beta").
#[test]
fn chats_pattern_matches_agent_and_project_name_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    let codex = h.join(
        ".codex/sessions/2026/01/02/rollout-2026-01-02T10-00-00-22222222-2222-4222-8222-222222222222.jsonl",
    );
    fs::create_dir_all(codex.parent().unwrap()).unwrap();
    fs::write(
        &codex,
        r#"{"type":"session_meta","payload":{"id":"22222222-2222-4222-8222-222222222222","cwd":"/work/beta","source":"cli"}}
{"type":"turn_context","payload":{"model":"gpt-5.5"}}
{"type":"response_item","timestamp":"2026-01-02T10:00:01.000Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"add a retry to the fetch helper"}]}}
{"type":"event_msg","timestamp":"2026-01-02T10:00:01.000Z","payload":{"type":"user_message","message":"add a retry to the fetch helper"}}
{"type":"response_item","timestamp":"2026-01-02T10:00:04.000Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Wrapped fetch in a 3-try loop with backoff."}]}}
"#,
    )
    .unwrap();
    let pi = h.join(".pi/agent/sessions/alpha/2026-01-03_root-alpha-0001.jsonl");
    fs::create_dir_all(pi.parent().unwrap()).unwrap();
    fs::write(
        &pi,
        r#"{"type":"session","id":"root-alpha-0001","version":3,"cwd":"/work/alpha","timestamp":"2026-01-03T00:00:00Z"}
{"type":"message","id":"u0","parentId":null,"timestamp":"2026-01-03T00:00:00Z","message":{"role":"user","content":[{"type":"text","text":"design the cache"}],"timestamp":"2026-01-03T00:00:00Z"}}
{"type":"message","id":"a0","parentId":"u0","timestamp":"2026-01-03T00:00:00Z","message":{"role":"assistant","model":"test","content":[{"type":"text","text":"Finished."}],"timestamp":"2026-01-03T00:00:00Z"}}
"#,
    )
    .unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    for (pattern, expected) in [
        ("codex", &["22222222-2222-4222-8222-222222222222"][..]),
        ("codex beta", &["22222222-2222-4222-8222-222222222222"]),
        ("beta beta", &["22222222-2222-4222-8222-222222222222"]),
        ("pi alpha", &["root-alpha-0001"]),
        ("codex alpha", &[]),
        ("beta /work", &[]),
    ] {
        let o = cli(&h, &d, &["chats", "--json", pattern], &[]);
        assert_eq!(
            o.status.code(),
            Some(if expected.is_empty() { 1 } else { 0 }),
            "{pattern}"
        );
        let sessions: Vec<_> = values(&o)[1..]
            .iter()
            .map(|r| r["session"].as_str().unwrap().to_owned())
            .collect();
        assert_eq!(sessions, expected, "{pattern}");
    }
}
// Expected output is agrep 0.4.9's on the pi fixture: --no-auto says the history may be
// stale on every surface that answered, and JSON carries the same story.
#[test]
fn no_auto_reads_with_results_say_history_may_be_stale_as_0_4_9() {
    let (_t, h, d) = pi();
    let stale = "history may be stale: automatic freshness checks are disabled by --no-auto\n";
    for args in [
        &["retry", "--no-auto"][..],
        &["retry", "--no-auto", "-c"],
        &["recall", "retry", "--no-auto"],
        &["recall", "retry", "--no-auto", "--probe"],
        &["pack", "retry", "jitter", "--no-auto"],
    ] {
        let o = ok(cli(&h, &d, args, &[]));
        assert_eq!(String::from_utf8(o.stderr).unwrap(), stale, "{args:?}");
    }
    let o = ok(cli(&h, &d, &["chats", "--no-auto"], &[]));
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        format!(
            "showing 4 of 4 matching chats, newest first (side chats hidden; --side shows them)\n\
             {stale}"
        )
    );
    let expected = serde_json::json!({"state":"degraded","failing":true,"may_be_stale":true,
        "code":"search-index-stale",
        "reason":"automatic freshness checks are disabled by --no-auto","checked":false});
    for args in [
        &["retry", "--no-auto", "--json"][..],
        &["recall", "retry", "--no-auto", "--json"],
        &["chats", "--no-auto", "--json"],
    ] {
        let o = ok(cli(&h, &d, args, &[]));
        assert_eq!(values(&o)[0]["freshness"], expected, "{args:?}");
    }
}
// Expected items are agrep 0.4.9's `summary pending --json` on this fixture, recorded in
// tests/fixtures/pending/expected.json: one chat per root or side-chat signal.
#[test]
fn pending_signals_match_0_4_9() {
    let t = TempDir::new().unwrap();
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/pending");
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    copy(&fixture.join("home"), &h);
    ok(cli(&h, &d, &["index"], &[]));
    let args = ["summary", "pending", "--json", "--since", "2026-09-01"];
    let keep = [
        "project",
        "status",
        "confidence",
        "source",
        "turn",
        "signals",
        "evidence",
        "items",
        "caveats",
        "handle",
        "evidence_session",
    ];
    let mut got: Vec<Value> = values(&ok(cli(&h, &d, &args, &[])))
        .into_iter()
        .filter(|r| r["kind"] == "pending")
        .map(|r| {
            Value::Object(
                keep.iter()
                    .filter_map(|k| r.get(*k).map(|v| (k.to_string(), v.clone())))
                    .collect(),
            )
        })
        .collect();
    got.sort_by(|a, b| a["project"].as_str().cmp(&b["project"].as_str()));
    let expected: Value =
        serde_json::from_str(&fs::read_to_string(fixture.join("expected.json")).unwrap()).unwrap();
    assert_eq!(Value::Array(got), expected);
}
// Expected output is agrep 0.4.9's on the pi fixture (py/around.py:245-251,762-772): around's
// metadata carries no freshness; a record after the rows says when they may be stale.
#[test]
fn around_freshness_record_matches_0_4_9() {
    let (_t, h, d) = pi();
    let unchecked = serde_json::json!({"state":"unchecked","failing":false,"checked":false,
        "may_be_stale":true});
    let rows = values(&ok(cli(
        &h,
        &d,
        &["around", "01930000", "1", "--json"],
        &[],
    )));
    assert!(rows[0].get("freshness").is_none());
    assert_eq!(
        (rows.len(), &rows[3]["kind"]),
        (4, &serde_json::json!("msg"))
    );
    let rows = values(&ok(cli(
        &h,
        &d,
        &["around", "01930000", "1", "--json", "--no-auto"],
        &[],
    )));
    assert!(rows[0].get("freshness").is_none());
    assert_eq!(rows.len(), 5);
    assert_eq!(
        rows[4],
        serde_json::json!({"kind":"agrep-meta","freshness":unchecked})
    );
    let text = ok(cli(&h, &d, &["around", "01930000", "1", "--no-auto"], &[]));
    assert_eq!(String::from_utf8(text.stderr).unwrap(), "");
    let args = [
        "around",
        "01930000",
        "1",
        "--json",
        "--who",
        "subagent",
        "--no-auto",
    ];
    let miss = cli(&h, &d, &args, &[]);
    assert_eq!(miss.status.code(), Some(2));
    assert_eq!(values(&miss)[0]["freshness"], unchecked);
    // 0.5 also counts changed sources awaiting refresh on that record.
    let root = agrep_core::data::DataRoot::open(&d, false).unwrap();
    let _work = root.work_lock(true).unwrap().unwrap();
    let source = h.join(".pi/agent/sessions/-work-alpha/2026-01-02T03-04-05-000Z_01930000-0000-7000-8000-000000000001.jsonl");
    let mut body = fs::read_to_string(&source).unwrap();
    body.push_str(r#"{"type":"message","id":"late","parentId":null,"timestamp":"2026-01-02T03:10:00.000Z","message":{"role":"user","content":[{"type":"text","text":"a later turn"}]}}"#);
    body.push('\n');
    fs::write(&source, body).unwrap();
    let rows = values(&ok(cli(
        &h,
        &d,
        &["around", "01930000", "1", "--json"],
        &[],
    )));
    assert_eq!(
        rows.last().unwrap()["freshness"],
        serde_json::json!({"state":"no-known-failure","failing":false,"checked":true,
            "pending_sources":1})
    );
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
    assert!(values(&user)
        .iter()
        .filter(|v| v["kind"] != "agrep-meta")
        .all(|v| v["who"] == "user"));
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
    // Expected values are agrep 0.4.9's output for the same command.
    let note = "explicit result handle wins: --chat not applied (--chat would have excluded it)";
    assert_eq!(
        v["filter_coverage"],
        serde_json::json!({"empty_dimensions":[],"checked":false,"reason":note})
    );
    assert!(o.stderr.is_empty());
    let o = ok(cli(
        &h,
        &d,
        &["recall", "@01930000:0.e5c8", "--chat", "missing-chat"],
        &[],
    ));
    assert_eq!(String::from_utf8(o.stderr).unwrap(), format!("{note}\n"));
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

// Expected output is agrep 0.4.9's for the same commands on the pi fixture.
#[test]
fn recall_handle_hits_and_lane_disclosures_match_0_4_9() {
    let (_t, h, d) = pi();
    let moved = "handle digest matches turn 0, not turn 1 - either the session was renumbered \
                 since the handle was minted, or the digest is wrong; verify the content before \
                 citing it";
    let o = ok(cli(&h, &d, &["recall", "@01930000:1.cd22", "--json"], &[]));
    assert_eq!(String::from_utf8(o.stderr).unwrap(), format!("{moved}\n"));
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(
        v["served"],
        serde_json::json!({"served_as_requested":false,"divergences":[
            {"kind":"content_moved","requested":1,"served":0,"note":moved}]})
    );
    assert_eq!(
        (&v["hits"][0]["turn"], &v["hits"][0]["score"]),
        (&0.into(), &1.0.into())
    );
    let o = ok(cli(&h, &d, &["recall", "@01930000:1.cd22"], &[]));
    assert_eq!(String::from_utf8(o.stderr).unwrap(), format!("{moved}\n"));
    let o = ok(cli(
        &h,
        &d,
        &["recall", "@01930000:0.cd22", "--project", "/work/beta"],
        &[],
    ));
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "explicit result handle wins: --project not applied (--project would have excluded it)\n"
    );
    let pack = [
        "pack",
        "retry",
        "jitter",
        "backoff",
        "--hits",
        "1",
        "--lexical",
    ];
    let o = ok(cli(&h, &d, &pack, &[]));
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "--hits 1 raised to 3: pack reserves one slot per query\n"
    );
    let o = ok(cli(&h, &d, &[&pack[..], &["--json"]].concat(), &[]));
    assert!(o.stderr.is_empty());
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(
        v["hits_floored"],
        serde_json::json!({"requested":1,"raised_to":3})
    );
    let o = ok(cli(
        &h,
        &d,
        &["recall", "retry backoff", "--who", "tool", "--json"],
        &[],
    ));
    let v: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(
        v["semantic_status"],
        serde_json::json!({"state":"not-run","reason":"tool-lane"})
    );
}

// Expected output is agrep 0.4.9's for the same commands on the pi fixture.
#[test]
fn cut_search_pages_name_their_larger_page_as_0_4_9() {
    let (_t, h, d) = pi();
    let args = [
        "backoff",
        "-n",
        "1",
        "--project",
        "/work/alpha",
        "--sort",
        "time",
    ];
    let o = ok(cli(&h, &d, &args, &[]));
    assert_eq!(
        String::from_utf8(o.stderr).unwrap(),
        "showing 1 of 3 matching rows (1 of them in tool output) · larger page: agrep \
         --project=/work/alpha --sort=time --classic -n 80 -- backoff\n"
    );
    let o = ok(cli(&h, &d, &[&args[..], &["--json"]].concat(), &[]));
    let meta = &values(&o)[0]["completeness"];
    assert_eq!(
        (
            &meta["more_command"],
            &meta["more_command_kind"],
            &meta["full_command"]
        ),
        (
            &"agrep --project=/work/alpha --sort=time --json -n 80 -- backoff".into(),
            &"broader-rerun".into(),
            &"agrep --project=/work/alpha --sort=time --json -n 0 -- backoff".into()
        )
    );
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
/// Six pi chats, `days` old, asking how to handle one secret-rotation topic each.
fn rotation_chats(home: &Path) {
    let now = chrono::Utc::now().timestamp();
    let topics = [
        "rotating the release signing keys",
        "renewing the TLS certificates for the gateway",
        "replacing the deploy credentials in the vault",
        "resetting the database passwords",
        "expiring the old api tokens",
        "revoking leaked ssh keys",
    ];
    for (i, topic) in topics.iter().enumerate() {
        let t0 = now - (20 - i as i64) * 86_400;
        let at = |s: i64| {
            chrono::DateTime::from_timestamp(t0 + s, 0)
                .unwrap()
                .format("%Y-%m-%dT%H:%M:%S.000Z")
                .to_string()
        };
        let dir = home.join(format!(".pi/agent/sessions/-work-sec{i}"));
        fs::create_dir_all(&dir).unwrap();
        let id = format!("01960000-0000-7000-8000-00000000f{i:03}");
        let mut lines = vec![
            serde_json::json!({"type":"session","id":id,"version":3,"cwd":format!("/work/sec{i}"),"timestamp":at(0)}),
        ];
        let turns = [
            (format!("how do we handle {topic}?"), format!("Here is the procedure for {topic}: the operator checks the inventory, updates the secret store, restarts the affected services and records the change.")),
            ("and the retry backoff for that job?".to_string(), "cap the retry backoff at 30s".to_string()),
        ];
        let mut parent = serde_json::Value::Null;
        for (n, (user, reply)) in turns.iter().enumerate() {
            let s = 2 * n as i64;
            lines.push(serde_json::json!({"type":"message","id":format!("u{n}"),"parentId":parent,"timestamp":at(s + 1),"message":{"role":"user","content":[{"type":"text","text":user}],"timestamp":at(s + 1)}}));
            lines.push(serde_json::json!({"type":"message","id":format!("a{n}"),"parentId":format!("u{n}"),"timestamp":at(s + 2),"message":{"role":"assistant","model":"pi/sonnet-5","provider":"anthropic","stopReason":"end_turn","timestamp":at(s + 2),"content":[{"type":"text","text":reply}]}}));
            parent = serde_json::json!(format!("a{n}"));
        }
        let body: String = lines.iter().map(|l| format!("{l}\n")).collect();
        fs::write(
            dir.join(format!("{}_{id}.jsonl", at(0).replace([':', '.'], "-"))),
            body,
        )
        .unwrap();
    }
}
// Expected rows are agrep 0.4.9's keyword-only `recall --json` on this fixture: content rows
// come from the three rarest words, a row holding only "keys" never qualifies, and each
// scores by its share of the query's IDF weight (py/corpusdb.py:4863-4911,
// py/search.py:1852-1876). Scores allow one unit of drift for the seconds the run takes.
#[test]
fn recall_content_fallback_admits_and_scores_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    rotation_chats(&h);
    ok(cli(&h, &d, &["index"], &[]));
    for (query, expected) in [
        (
            "rotating signing keys and TLS certificates",
            vec![("f000", 0.2167), ("f001", 0.1627)],
        ),
        (
            "how do we rotate secret credentials",
            vec![
                ("f002", 0.1752),
                ("f005", 0.082),
                ("f004", 0.0781),
                ("f003", 0.0743),
                ("f001", 0.0673),
                ("f000", 0.064),
            ],
        ),
    ] {
        let out = ok(cli(&h, &d, &["recall", query, "--json"], &[]));
        let v: Value = serde_json::from_slice(&out.stdout).unwrap();
        let hits = v["hits"].as_array().unwrap();
        let got: Vec<_> = hits
            .iter()
            .map(|h| {
                let session = h["session"].as_str().unwrap();
                (&session[session.len() - 4..], h["score"].as_f64().unwrap())
            })
            .collect();
        assert_eq!(got.len(), expected.len(), "{query}: {got:?}");
        for ((session, score), (want, want_score)) in got.iter().zip(&expected) {
            assert_eq!(session, want, "{query}: {got:?}");
            assert!((score - want_score).abs() <= 1.0001e-4, "{query}: {got:?}");
        }
        assert!(hits.iter().all(|h| h["matched"] == "content-terms"));
    }
}
/// Lived rows next to the three kinds 0.4.9 scaled as tooling about history: a harness row
/// (by the corpus's harness policy), a synthetic row (a `<synthetic>` model) and a
/// subagent's first turn, with a later subagent turn that stays lived.
fn meta_chats(home: &Path) {
    let now = chrono::Utc::now().timestamp();
    let at = |t0: i64, s: i64| {
        chrono::DateTime::from_timestamp(t0 + s, 0)
            .unwrap()
            .format("%Y-%m-%dT%H:%M:%S.000Z")
            .to_string()
    };
    let write = |path: std::path::PathBuf, records: Vec<Value>| {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            path,
            records.iter().map(|r| format!("{r}\n")).collect::<String>(),
        )
        .unwrap();
    };
    let pi = |dir: &str,
              id: &str,
              days: i64,
              cwd: &str,
              user: &str,
              reply: &str,
              model: Option<&str>| {
        let t0 = now - days * 86_400;
        let mut lines = vec![
            serde_json::json!({"type":"session","id":id,"version":3,"cwd":cwd,"timestamp":at(t0, 0)}),
        ];
        let mut parent = Value::Null;
        if let Some(model) = model {
            lines.push(serde_json::json!({"type":"model_change","id":"m0","parentId":null,"timestamp":at(t0, 0),"provider":"anthropic","modelId":model}));
            parent = serde_json::json!("m0");
        }
        lines.push(serde_json::json!({"type":"message","id":"u0","parentId":parent,"timestamp":at(t0, 1),"message":{"role":"user","content":[{"type":"text","text":user}],"timestamp":at(t0, 1)}}));
        lines.push(serde_json::json!({"type":"message","id":"a0","parentId":"u0","timestamp":at(t0, 2),"message":{"role":"assistant","model":"pi/sonnet-5","provider":"anthropic","stopReason":"end_turn","timestamp":at(t0, 2),"content":[{"type":"text","text":reply}]}}));
        let name = format!("{}_{id}.jsonl", at(t0, 0).replace([':', '.'], "-"));
        write(home.join(".pi/agent/sessions").join(dir).join(name), lines);
    };
    pi("-work-vault", "01970000-0000-7000-8000-00000000a001", 30, "/work/vault", "how do we rotate the signing keys for the release pipeline?", "Rotate the signing keys by generating a new pair, publishing the public key and retiring the old one.", None);
    pi(
        "-work-vault",
        "01970000-0000-7000-8000-00000000a002",
        1,
        "/work/vault",
        "maintenance bot: rotate the signing keys on schedule",
        "Scheduled rotation queued.",
        Some("<synthetic>"),
    );
    pi(
        "-work-bench",
        "01970000-0000-7000-8000-00000000a003",
        1,
        "/work/bench",
        "benchmark task: rotate the signing keys and report the timing",
        "Rotated the signing keys in 2.1s.",
        None,
    );
    let main = "11111111-2222-4333-8444-555555555555";
    let project = home.join(".claude/projects/-work-app");
    let t0 = now - 2 * 86_400;
    write(
        project.join(format!("{main}.jsonl")),
        vec![
            serde_json::json!({"sessionId":main,"cwd":"/work/app","version":"2.1.0","type":"user","userType":"external","uuid":"m0","parentUuid":null,"timestamp":at(t0, 1),"message":{"role":"user","content":"audit the release tooling before friday"}}),
            serde_json::json!({"sessionId":main,"cwd":"/work/app","version":"2.1.0","type":"assistant","uuid":"m1","parentUuid":"m0","timestamp":at(t0, 2),"message":{"role":"assistant","model":"claude-fable-5","content":[{"type":"text","text":"I will ask a subagent how we rotate the signing keys."}]}}),
        ],
    );
    let t1 = now - 86_400 + 600;
    let side = |uuid: &str, parent: Value, s: i64, role: &str, text: &str| {
        let mut r = serde_json::json!({"sessionId":main,"cwd":"/work/app","version":"2.1.0","agentId":"a0123456789abcdef","isSidechain":true,"type":role,"uuid":uuid,"parentUuid":parent,"timestamp":at(t1, s),"message":{"role":role,"content":[{"type":"text","text":text}]}});
        if role == "assistant" {
            r["message"]["model"] = serde_json::json!("claude-fable-5");
        }
        r
    };
    write(
        project
            .join(main)
            .join("subagents/agent-a0123456789abcdef.jsonl"),
        vec![
            side(
                "s0",
                Value::Null,
                1,
                "user",
                "Find out how we rotate the signing keys and list the steps.",
            ),
            side(
                "s1",
                serde_json::json!("s0"),
                2,
                "assistant",
                "The release job calls scripts/keys.sh, which writes the new pair.",
            ),
            side(
                "s2",
                serde_json::json!("s1"),
                3,
                "user",
                "also check how staging should rotate the signing keys",
            ),
            side(
                "s3",
                serde_json::json!("s2"),
                4,
                "assistant",
                "Staging reuses the same script with its own vault path.",
            ),
        ],
    );
}
// Expected rows are agrep 0.4.9's output on this fixture: every tier scales a harness or
// synthetic row and a subagent's first turn by 0.45 (py/search.py:1222-1238, 1871-1876);
// recall and pack take each family's best row and rank lived rows before ~meta ones
// (py/recall.py:983-994, 2025-2035); grouped pages sink and mark ~meta rows
// (py/search.py:5507-5516, 5608-5645); compact pages compose as py/compact.py:160-442
// does. Scores allow one unit of drift for the seconds the run takes.
#[test]
fn meta_rows_scale_sink_and_mark_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    meta_chats(&h);
    ok(cli(&h, &d, &["index"], &[]));
    fs::write(d.join("harness_prefixes.txt"), "benchmark task:\n").unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    let rows = |args: &[&str]| -> Vec<(String, i64, String, f64, String)> {
        let out = ok(cli(&h, &d, args, &[]));
        let text = String::from_utf8(out.stdout).unwrap();
        let hits: Vec<Value> = if args.contains(&"recall") || args.contains(&"pack") {
            serde_json::from_str::<Value>(&text).unwrap()["hits"]
                .as_array()
                .unwrap()
                .clone()
        } else {
            text.lines()
                .skip(1)
                .map(|l| serde_json::from_str(l).unwrap())
                .collect()
        };
        hits.iter()
            .map(|v| {
                let session = v["session"].as_str().unwrap();
                (
                    session[session.len() - 6..].to_owned(),
                    v["turn"].as_i64().unwrap(),
                    v["who"].as_str().unwrap_or_default().to_owned(),
                    v["score"].as_f64().unwrap(),
                    v["matched"].as_str().unwrap_or("phrase").to_owned(),
                )
            })
            .collect()
    };
    let phrase = "rotate the signing keys";
    let terms = "signing keys rotate";
    let content = "how often should we rotate signing certificates";
    // Each row: session tail, turn, speaker (search surfaces), score and match lane.
    type Row = (&'static str, i64, &'static str, f64, &'static str);
    let cases: [(&[&str], &[Row]); 6] = [
        (
            &[phrase, "--json"],
            &[
                ("555555", 0, "agent", 0.315, "phrase"),
                ("abcdef", 1, "subagent", 0.2991, "phrase"),
                ("00a001", 0, "user", 0.1522, "phrase"),
                ("abcdef", 0, "subagent", 0.1346, "phrase"),
                ("00a001", 0, "agent", 0.0788, "phrase"),
                ("00a002", 0, "synthetic", 0.0559, "phrase"),
                ("00a003", 0, "harness", 0.0559, "phrase"),
                ("00a003", 0, "agent", 0.3783, "all-terms"),
            ],
        ),
        (
            &[terms, "--json"],
            &[
                ("555555", 0, "agent", 0.6301, "all-terms"),
                ("abcdef", 1, "subagent", 0.5981, "all-terms"),
                ("00a003", 0, "agent", 0.3775, "all-terms"),
                ("00a001", 0, "user", 0.3043, "all-terms"),
                ("abcdef", 0, "subagent", 0.2692, "all-terms"),
                ("00a001", 0, "agent", 0.1575, "all-terms"),
                ("00a002", 0, "synthetic", 0.1117, "all-terms"),
                ("00a003", 0, "harness", 0.1117, "all-terms"),
            ],
        ),
        (
            &[phrase, "-l", "--json"],
            &[
                ("555555", 0, "agent", 0.315, "phrase"),
                ("abcdef", 1, "subagent", 0.2991, "phrase"),
                ("00a003", 0, "agent", 0.3783, "all-terms"),
                ("00a001", 0, "user", 0.1522, "phrase"),
                ("00a002", 0, "synthetic", 0.0559, "phrase"),
            ],
        ),
        (
            &["recall", phrase, "--json"],
            &[
                ("555555", 0, "", 0.315, "phrase"),
                ("00a001", 0, "", 0.1522, "phrase"),
                ("00a002", 0, "", 0.0559, "phrase"),
                ("00a003", 0, "", 0.0559, "phrase"),
            ],
        ),
        (
            &["recall", content, "--json"],
            &[
                ("00a003", 0, "", 0.189, "content-terms"),
                ("555555", 0, "", 0.1799, "content-terms"),
                ("00a001", 0, "", 0.0869, "content-terms"),
                ("00a002", 0, "", 0.0319, "content-terms"),
            ],
        ),
        (
            &["pack", phrase, terms, "--json"],
            &[
                ("555555", 0, "", 0.315, "phrase"),
                ("00a003", 0, "", 0.3775, "all-terms"),
                ("00a001", 0, "", 0.1522, "phrase"),
                ("00a002", 0, "", 0.1117, "all-terms"),
            ],
        ),
    ];
    for (args, expected) in cases {
        let got = rows(args);
        assert_eq!(got.len(), expected.len(), "{args:?}: {got:?}");
        for (row, want) in got.iter().zip(expected) {
            assert_eq!(
                (row.0.as_str(), row.1, row.2.as_str(), row.4.as_str()),
                (want.0, want.1, want.2, want.4),
                "{args:?}: {got:?}"
            );
            assert!((row.3 - want.3).abs() <= 1.0001e-4, "{args:?}: {got:?}");
        }
    }
    let plain = |out: Output| {
        let text = String::from_utf8(out.stdout).unwrap();
        let mut plain = String::new();
        let mut chars = text.chars();
        while let Some(c) = chars.next() {
            if c == '\x1b' {
                chars.by_ref().find(|&c| c == 'm');
            } else {
                plain.push(c);
            }
        }
        plain
    };
    let grouped = plain(ok(cli(&h, &d, &[phrase, "--color", "always"], &[])));
    let heads: Vec<&str> = grouped
        .lines()
        .filter_map(|l| l.split("  @").nth(1))
        .map(|l| l.split(' ').next().unwrap())
        .collect();
    assert_eq!(
        heads,
        [
            "11111111:0.de2e",
            "agent-a0:1.34cd",
            "01970000-0000-7000-8000-00000000a001:0.76ad",
            "01970000-0000-7000-8000-00000000a003:0.bedb",
            "01970000-0000-7000-8000-00000000a002:0.3e1a"
        ]
    );
    let turns: Vec<&str> = grouped.lines().filter(|l| l.starts_with("     ")).collect();
    assert_eq!(
        turns,
        [
            "     0 → I will ask a subagent how we rotate the signing keys.",
            "     1 ⇒ also check how staging should rotate the signing keys",
            "     0 ⇒ Find out how we rotate the signing keys and list the steps. ~meta",
            "     0 › how do we rotate the signing keys for the release pipeline?",
            "     0 → Rotate the signing keys by generating a new pair, publishing the public key and retiring the old one.",
            "     0 → Rotated the signing keys in 2.1s.",
            "     0 › benchmark task: rotate the signing keys and report the timing ~meta",
            "     0 › maintenance bot: rotate the signing keys on schedule ~meta",
        ]
    );
    let recall = plain(ok(cli(&h, &d, &["recall", phrase], &[])));
    let marked: Vec<&str> = recall
        .lines()
        .filter(|l| l.starts_with("── @") && l.ends_with(" · ~meta"))
        .map(|l| &l["── @".len()..l.find(" · ").unwrap()])
        .collect();
    assert_eq!(
        marked,
        [
            "01970000-0000-7000-8000-00000000a002:0.3e1a",
            "01970000-0000-7000-8000-00000000a003:0.62c4"
        ]
    );
    let a001 = "01970000-0000-7000-8000-00000000a001:0.76ad";
    let a003 = "01970000-0000-7000-8000-00000000a003:0.bedb";
    for (query, expected) in [
        (phrase, ["11111111:0.de2e", a001, "agent-a0:1.34cd", a003]),
        (terms, ["11111111:0.de2e", a003, a001, "agent-a0:1.34cd"]),
        (content, [a003, "11111111:0.de2e", a001, "agent-a0:1.34cd"]),
    ] {
        let page = plain(ok(cli(&h, &d, &[query], &[("AGREP_PROFILE", "compact")])));
        let handles: Vec<&str> = page.lines().map(|l| &l[1..l.find(' ').unwrap()]).collect();
        assert_eq!(handles, expected, "{query}: {page}");
    }
}

// 0.4.9 flushed a compact page's rows before its footer (py/search.py:8374-8377). Its real
// output for `retry -n 1 --no-auto` on the pi fixture, compact and agent profiles alike, on a
// terminal and in one pipe holding both streams, was the row, then `3 matches · broader rerun
// (may repeat): agrep --deeper m.<TOKEN>`, then the --no-auto line. A classic terminal page
// printed its rows, then its summary, then that line. The footers' own wording differs as
// PORTING.md says (a floor, no --deeper, the classic summary).
#[cfg(unix)]
#[test]
fn page_footers_follow_rows_on_a_terminal_and_in_a_pipe() {
    use std::io::Read;
    use std::os::fd::FromRawFd;
    let (_t, h, d) = pi();
    let age = regex::Regex::new(r"(?m) \d+[a-z]{1,2}( |$)").unwrap();
    let transcript = |env: &[(&str, &str)], terminal: bool| -> String {
        let mut cmd = command(&h, &d, &["retry", "-n", "1", "--no-auto"], env);
        let mut bytes = Vec::new();
        let mut child = if terminal {
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
            let mut master = unsafe { fs::File::from_raw_fd(master) };
            let slave = unsafe { fs::File::from_raw_fd(slave) };
            cmd.stdout(slave.try_clone().unwrap()).stderr(slave);
            let child = cmd.spawn().unwrap();
            drop(cmd);
            let mut chunk = [0; 4096];
            loop {
                match master.read(&mut chunk) {
                    Ok(0) => break,
                    Ok(n) => bytes.extend_from_slice(&chunk[..n]),
                    // Linux reports a terminal whose last writer closed as EIO.
                    Err(e) if e.raw_os_error() == Some(libc::EIO) => break,
                    Err(e) => panic!("terminal read: {e}"),
                }
            }
            child
        } else {
            let (mut reader, writer) = std::io::pipe().unwrap();
            cmd.stdout(writer.try_clone().unwrap()).stderr(writer);
            let child = cmd.spawn().unwrap();
            drop(cmd);
            reader.read_to_end(&mut bytes).unwrap();
            child
        };
        assert!(child.wait().unwrap().success());
        let raw = String::from_utf8(bytes).unwrap().replace('\r', "");
        let mut text = String::new();
        let mut rest = raw.as_str();
        while let Some(at) = rest.find('\x1b') {
            text.push_str(&rest[..at]);
            rest = &rest[at + rest[at..].find('m').unwrap() + 1..];
        }
        text.push_str(rest);
        age.replace_all(&text, " <age>$1").into_owned()
    };
    let stale = "history may be stale: automatic freshness checks are disabled by --no-auto";
    let compact = [
        "@01930000:2.18ba pi/user alpha <age> now add jitter to the retry",
        "2+ matches (floor; -c exact)",
        stale,
    ];
    for env in [
        &[("AGREP_PROFILE", "compact")][..],
        &[
            ("AGREP_PROFILE", ""),
            ("CODEX_THREAD_ID", "sentinel-thread"),
        ][..],
    ] {
        for terminal in [true, false] {
            let out = transcript(env, terminal);
            assert_eq!(
                out.lines().collect::<Vec<_>>(),
                compact,
                "{env:?} {terminal}"
            );
        }
    }
    let classic = transcript(&[], true);
    assert_eq!(
        classic.lines().collect::<Vec<_>>(),
        [
            "pi · alpha  @01930000:2.18ba · 1 hit · <age>",
            "     2 › now add jitter to the retry",
            "key: › you · → agent · ⇒ subagent · ⚙ tool",
            "showing 1 of 2+ matching rows (a floor, not the total; -c counts exactly) · larger page: agrep --no-auto --classic -n 80 -- retry",
            stale,
        ]
    );
    // 0.4.9 held these piped rows in its output buffer until it exited, so its stderr lines
    // came first; PORTING.md keeps a terminal's order.
    assert_eq!(
        transcript(&[], false).lines().collect::<Vec<_>>(),
        [
            "01930000-0000-7000-8000-000000000001\tpi\talpha\t2\tuser\tnow add jitter to the retry",
            "showing 1 of 2+ matching rows (a floor, not the total; -c counts exactly) · larger page: agrep --no-auto --classic -n 80 -- retry",
            stale,
        ]
    );
}

// Expected lines and exit codes are agrep 0.4.9's real `archive` output on the pi fixture:
// decimal sizes, an append's suffix as its new content, a stderr line per captured file, its
// status sentences and notes (py/archive.py:212-222, 2282-2295, 2432-2468, 3182-3286). The
// stored sizes are 0.5's compressed chunks, which PORTING.md says differ from 0.4.9's.
#[test]
fn archive_reports_as_0_4_9() {
    let (_t, h, d) = pi();
    let run = |args: &[&str]| cli(&h, &d, args, &[]);
    let out = |o: &Output| String::from_utf8_lossy(&o.stdout).into_owned();
    let never = "archive keeps the last 3 version(s) per source file: 0 files, 0 B stored, \
                 never captured yet\narchive freshness-unknown: no capture pass has completed\n";
    assert_eq!(
        out(&ok(run(&["archive", "--on"]))),
        "archive auto-capture enabled\n"
    );
    let empty = run(&["archive", "--status"]);
    assert_eq!(
        (empty.status.code(), out(&empty).as_str()),
        (Some(1), never)
    );
    let time = regex::Regex::new(r" in \d+\.\ds\n$").unwrap();
    let first = ok(run(&["archive"]));
    assert_eq!(
        time.replace(&out(&first), " in <T>s\n"),
        "archive: 5 files - 0 unchanged, 0 appended, 5 full captures, 5.1 KB new content in <T>s\n"
    );
    let stderr = String::from_utf8_lossy(&first.stderr);
    let mut captured: Vec<&str> = stderr.lines().collect();
    captured.sort_unstable();
    assert_eq!(
        captured,
        [
            "archive: captured 2025-11-01T00-00-00-000Z_01920000-0000-7000-8000-000000000002.jsonl (494 bytes)",
            "archive: captured 2026-01-02T03-04-05-000Z_01930000-0000-7000-8000-000000000001.jsonl (1925 bytes)",
            "archive: captured 2026-02-03T04-05-06-000Z_01940000-0000-7000-8000-000000000003.jsonl (971 bytes)",
            "archive: captured 2026-03-04T05-06-07-000Z_01950000-0000-7000-8000-000000000005.jsonl (1358 bytes)",
            "archive: captured advisor.jsonl (344 bytes)",
        ]
    );
    let status = out(&ok(run(&["archive", "--status"])));
    assert!(
        regex::Regex::new(
            r"^archive keeps the last 3 version\(s\) per source file: 5 files, \d+\.\d KB stored, \d+\.\d+x smaller than the originals, last capture \d+s ago\n$"
        )
        .unwrap()
        .is_match(&status),
        "{status}"
    );
    let alpha = h.join(".pi/agent/sessions/-work-alpha/2026-01-02T03-04-05-000Z_01930000-0000-7000-8000-000000000001.jsonl");
    let mut file = fs::OpenOptions::new().append(true).open(alpha).unwrap();
    std::io::Write::write_all(&mut file, br#"{"type":"message","id":"zz","parentId":"a3","timestamp":"2026-01-02T03:05:00.000Z","message":{"role":"user","content":[{"type":"text","text":"one more line"}],"timestamp":"2026-01-02T03:05:00.000Z"}}
"#).unwrap();
    drop(file);
    let second = ok(run(&["archive"]));
    assert_eq!(
        time.replace(&out(&second), " in <T>s\n"),
        "archive: 5 files - 4 unchanged, 1 appended, 0 full captures, 200 B new content in <T>s\n"
    );
    assert_eq!(
        String::from_utf8_lossy(&second.stderr),
        "archive: appended 2026-01-02T03-04-05-000Z_01930000-0000-7000-8000-000000000001.jsonl (2125 bytes)\n"
    );
    assert_eq!(
        out(&ok(run(&["archive", "--prune"]))),
        "pruned 0 old version record(s), reclaimed 0 B\n"
    );
    let size = regex::Regex::new(r"\d+\.\d KB").unwrap();
    let dir = d.join("archive").display().to_string();
    let sized = |o: &Output| size.replace_all(&out(o), "<S>").replace(&dir, "<D>");
    assert_eq!(
        sized(&ok(run(&["archive", "--off"]))),
        "archive auto-capture disabled\nthe copies already taken stay: <S> in <D>; `agrep archive --purge` deletes them\n"
    );
    assert_eq!(
        sized(&ok(run(&["archive", "--status"]))),
        "archive retention is off (`agrep archive --on` turns it on)\nearlier captures still stored: <S> in <D>; `agrep archive --purge` deletes them\n"
    );
    let refused = run(&["archive", "--purge"]);
    assert_eq!(
        (refused.status.code(), sized(&refused).as_str()),
        (
            Some(1),
            "archive purge would delete <S> of archived copies in <D>; pass --yes to confirm\n"
        )
    );
    ok(run(&["archive", "--on"]));
    assert_eq!(
        sized(&ok(run(&["archive", "--purge", "--yes"]))),
        "archive purged: <S> of archived copies deleted from <D>\nretention is still on: the next capture pass re-archives the live stores (`agrep archive --off` stops that)\n"
    );
    let purged = run(&["archive", "--status"]);
    assert_eq!(
        (purged.status.code(), out(&purged).as_str()),
        (Some(1), never)
    );
}

// Expected exit codes and stderr lines are agrep 0.4.9's for the same commands on the pi
// fixture: an option another makes inert, an impossible window and a bad bound are refused
// by name, with a command that drops the refused option (py/surface_policy.py:943-1219,
// py/search.py:1102-1120, 7262-7373, py/recall.py:1682-1703, py/around.py:808-860).
#[test]
fn usage_errors_exit_and_read_as_0_4_9() {
    let (_t, h, d) = pi();
    let cases: &[(&[&str], i32, &str)] = &[
        (&["retry", "-l", "-c"], 2, "agrep: -l cannot be combined with -c, which prints one number; run: agrep search retry -c"),
        (&["retry", "-l", "--count-by-tier"], 2, "agrep: -l cannot be combined with --count-by-tier, which prints one number per tier; run: agrep search retry --count-by-tier"),
        (&["retry", "--json", "-c"], 2, "agrep: --json cannot be combined with -c, which prints one number; run: agrep search retry -c"),
        (&["retry", "--json", "--count-by-tier"], 2, "agrep: --json cannot be combined with --count-by-tier, which prints one number per tier; run: agrep search retry --count-by-tier"),
        (&["retry", "-n", "5", "-c"], 2, "agrep: -n cannot be combined with -c, which prints one number; run: agrep search retry -c"),
        (&["retry", "-n", "5", "--count-by-tier"], 2, "agrep: -n cannot be combined with --count-by-tier, which prints one number per tier; run: agrep search retry --count-by-tier"),
        (&["retry", "--sort", "time", "-c"], 2, "agrep: --sort cannot be combined with -c, which prints one number; run: agrep search retry -c"),
        (&["retry", "--sort", "score", "--count-by-tier"], 2, "agrep: --sort cannot be combined with --count-by-tier, which prints one number per tier; run: agrep search retry --count-by-tier"),
        (&["retry", "--flat", "-c"], 2, "agrep: --flat cannot be combined with -c, which prints one number; run: agrep search retry -c"),
        (&["retry", "--flat", "--count-by-tier"], 2, "agrep: --flat cannot be combined with --count-by-tier, which prints one number per tier; run: agrep search retry --count-by-tier"),
        (&["retry", "--flat", "--json"], 2, "agrep: --flat cannot be combined with --json, which already writes one machine row per hit; run: agrep search retry --json"),
        (&["retry", "--flat", "-l"], 2, "agrep: --flat cannot be combined with -l, which lists matching chats, one line each; run: agrep search retry -l"),
        (&["retry", "--classic", "-c"], 2, "agrep: --classic cannot be combined with -c, which prints one number; run: agrep search retry -c"),
        (&["retry", "--classic", "--json"], 2, "agrep: --classic cannot be combined with --json, which already writes one machine row per hit; run: agrep search retry --json"),
        (&["retry", "--classic", "--flat"], 2, "agrep: --classic cannot be combined with --flat, which writes one tab-separated row per hit; run: agrep search retry --flat"),
        (&["retry", "--classic", "-l"], 2, "agrep: --classic cannot be combined with -l, which lists matching chats, one line each; run: agrep search retry -l"),
        (&["retry", "--soft"], 2, "agrep: --soft has no effect without --model; run: agrep search retry"),
        (&["search", "two words", "--model-soft", "--soft", "-c"], 2, "agrep: --soft has no effect without --model; run: agrep search 'two words' -c"),
        (&["retry", "-lic"], 2, "agrep: -l cannot be combined with -c, which prints one number; run: agrep search retry -ic"),
        (&["retry", "--chats", "--count"], 2, "agrep: -l cannot be combined with -c, which prints one number; run: agrep search retry --count"),
        (&["retry", "-cin3", "--max=5", "-n", "8"], 2, "agrep: -n cannot be combined with -c, which prints one number; run: agrep search retry -ci"),
        (&["--no-auto", "retry", "--color", "never", "-lc"], 2, "agrep: -l cannot be combined with -c, which prints one number; run: agrep search --no-auto retry --color never -c"),
        (&["retry", "--classic", "--json", "-l", "-c"], 2, "agrep: -l cannot be combined with -c, which prints one number; run: agrep search retry -c"),
        (&["it's", "--flat", "--json", "-l"], 2, "agrep: --flat cannot be combined with --json, which already writes one machine row per hit; run: agrep search 'it'\"'\"'s' --json -l"),
        (&["--more", "abc", "-c"], 2, "agrep: -c/--count-by-tier cannot be combined with --more"),
        (&["--more", "abc", "--flat"], 2, "agrep: --more continues a frozen compact page; --flat freezes none - rerun the query with --flat -n 80 for a bounded larger page"),
        (&["--more", "abc", "retry"], 2, "agrep: --more cannot be combined with search, filter, or output options"),
        (&["--more", "abc", "--sort", "score"], 2, "invalid continuation handle"),
        (&["search"], 2, "empty pattern - add a word or phrase to search (a word that is also a command searches with: agrep search \"search\")"),
        (&["recall", "retry", "--all-side-chats", "--no-side"], 0, ""),
        (&["retry", "-c", "--count-by-tier"], 2, "agrep: -c and --count-by-tier are mutually exclusive"),
        (&["retry", "--count-by-tier", "-E"], 2, "agrep: --count-by-tier requires the default keyword matcher; drop -s/-E/-w"),
        (&["retry", "--count-by-tier", "-w"], 2, "agrep: --count-by-tier requires the default keyword matcher; drop -s/-E/-w"),
        (&["retry", "--count-by-tier", "-s"], 2, "agrep: --count-by-tier requires the default keyword matcher; drop -s/-E/-w"),
        (&["retry", "-E", "-w"], 2, "agrep: search matcher modes are mutually exclusive"),
        (&["retry", "-s", "-E"], 2, "agrep: search matcher modes are mutually exclusive"),
        (&["retry", "-s", "--lexical"], 2, "agrep: -s and --lexical are contradictory; pick one"),
        (&["--no-auto", "search", "-s", "foo_bar"], 2, "agrep: unrecognized arguments: foo_bar"),
        (&["search", "--no-auto", "foo", "-s", "bar"], 2, "agrep: unrecognized arguments: bar"),
        (&["--no-auto", "foo", "-n", "5", "bar"], 2, "agrep: unrecognized arguments: bar"),
        (&["--no-auto", "foo", "--json", "bar", "baz"], 2, "agrep: unrecognized arguments: bar baz"),
        (&["--no-auto", "foo", "--json", "--", "bar"], 2, "agrep: unrecognized arguments: -- bar"),
        (&["chats", "retry", "--json", "bar"], 2, "agrep chats: unrecognized arguments: bar; to search for the word \"chats\": agrep search chats retry --json bar"),
        (&["chats", "retry", "-n", "3", "bar", "baz"], 2, "agrep chats: unrecognized arguments: bar baz; to search for the word \"chats\": agrep search chats retry -n 3 bar baz"),
        (&["chats", "retry", "-n2", "bar"], 2, "agrep chats: unrecognized arguments: bar"),
        (&["recall", "retry", "--json", "--", "bar"], 2, "agrep recall: unrecognized arguments: -- bar; to search for the word \"recall\": agrep search recall retry --json -- bar"),
        (&["recall", "a b", "--json", "c'd"], 2, "agrep recall: unrecognized arguments: c'd; to search for the word \"recall\": agrep search recall 'a b' --json 'c'\"'\"'d'"),
        (&["recall", "retry", "--hits", "2", "bar"], 2, "agrep recall: unrecognized arguments: bar"),
        (&["pack", "retry", "--json", "bar"], 2, "agrep pack: unrecognized arguments: bar; to search for the word \"pack\": agrep search pack retry --json bar"),
        (&["summary", "--group", "day"], 2, "agrep summary: --group applies to `agrep summary time`"),
        (&["summary", "time", "--idle-cap", "0m", "--group", "day"], 2, "agrep summary: --idle-cap must be a positive duration like 5m, 20m or 1h"),
        (&["summary", "--idle-cap", "xyz", "--since", "bad"], 2, "agrep summary: --idle-cap must be a positive duration like 5m, 20m or 1h"),
        (&["retry", "-c", "-s"], 2, "agrep: -c cannot be combined with --semantic; semantic totals are bounded"),
        (&["retry", "-c", "--no-meta"], 2, "agrep: --no-meta cannot narrow -c/--count-by-tier; machine counts keep grep parity"),
        (&["retry", "--agent", "nosuch"], 2, "unknown agent 'nosuch' - valid: claude, codex, opencode, antigravity, agy, kimi, cline, gemini, crush, cursor, pi, omp"),
        (&["chats", "--agent=PI"], 2, "unknown agent 'PI' - valid: claude, codex, opencode, antigravity, agy, kimi, cline, gemini, crush, cursor, pi, omp"),
        (&["retry", "--coverage", "--json"], 2, "agrep: --coverage cannot be combined with -c/--count-by-tier/-l/--json/--flat"),
        (&["retry", "--coverage", "-l"], 2, "agrep: --coverage cannot be combined with -c/--count-by-tier/-l/--json/--flat"),
        (&["retry", "--coverage", "-E"], 2, "agrep: --coverage pins the coverage lane; drop the other matcher mode"),
        (&["retry", "--coverage", "--lexical"], 2, "agrep: --coverage pins the coverage lane; drop the other matcher mode"),
        (&["retry", "-s", "--who", "tool"], 2, "agrep: --semantic cannot search --who tool because tool rows are not embedded; use --lexical"),
        (&["retry", "-s", "--sort", "position"], 2, "agrep: --sort position is undefined for semantic candidates; use score or time"),
        (&["retry", "--all-side-chats"], 2, "agrep: --all-side-chats requires --semantic"),
        (&["recall", "retry", "--hits", "2", "--probe"], 2, "agrep recall: --hits cannot be combined with --probe, which prints one pointer line; run: agrep recall retry --probe"),
        (&["pack", "retry", "two", "--hits=2", "--probe"], 2, "agrep pack: --hits cannot be combined with --probe, which prints one pointer line; run: agrep pack retry two --probe"),
        (&["recall", "retry", "--hits", "0", "--probe"], 2, "agrep recall: --hits must be at least 1"),
        (&["recall", "retry", "--probe", "--json"], 2, "agrep recall: --probe emits a pointer line and cannot be combined with --json"),
        (&["recall", "retry", "--soft"], 2, "agrep recall: --soft has no effect without --model; run: agrep recall retry"),
        (&["doctor", "--fix", "--json"], 2, "agrep doctor: --fix cannot be combined with --json, which prints one machine report; run: agrep doctor --json"),
        (&["doctor", "--deep", "--fix", "--json"], 2, "agrep doctor: --fix cannot be combined with --json, which prints one machine report; run: agrep doctor --deep --json"),
        (&["tail", "--events", "done", "--snapshot"], 2, "agrep tail: --events cannot be combined with --snapshot, which prints current session state once, not an event stream; run: agrep tail --snapshot"),
        (&["tail", "--agent", "foo"], 2, "agrep tail: unknown agent 'foo'; supported for live observation: claude, codex, opencode, antigravity, cursor, pi"),
        (&["tail", "--agent", "pi,KIMI"], 2, "agrep tail: kimi has no live observation support: no validated incremental journal tailer"),
        (&["tail", "--agent", ","], 2, "agrep tail: --agent requires at least one supported agent name"),
        (&["audit", "--agent", "pi,codex"], 2, "agrep audit: --agent must name one of: claude, codex, opencode, antigravity, kimi, cline, gemini, crush, cursor, pi"),
        (&["audit", "--agent", "pi", "--agent", "codex"], 2, "agrep audit: --agent may be supplied only once"),
        (&["audit", "--agent", " "], 2, "agrep audit: --agent needs a value (claude, codex, opencode, antigravity, kimi, cline, gemini, crush, cursor, pi) - an empty value would silently match every agent"),
        (&["run", "Claude"], 2, "unknown agent 'Claude'; supported for native resume: claude, codex, opencode, antigravity, pi"),
        (&["run", "cursor"], 2, "cursor has no native resume support: sessions are editor-owned and have no safe CLI resume command"),
        (&["around", "@01930000:2.18ba", "--max-chars", "100", "--full"], 2, "agrep around: --max-chars cannot be combined with --full, which uncaps indexed message text; run: agrep around @01930000:2.18ba --full"),
        (&["around", "@01930000:2.18ba", "--max-chars", "4000", "--full"], 2, "agrep around: --max-chars cannot be combined with --full, which uncaps indexed message text; run: agrep around @01930000:2.18ba --full"),
        (&["around", "@01930000:2.18ba", "--tool-output", "100", "--no-tools"], 2, "agrep around: --tool-output cannot be combined with --no-tools, which hides tool-call lines; run: agrep around @01930000:2.18ba --no-tools"),
        (&["around", "@01930000:2.18ba", "--max-chars", "10", "--tool-output", "5", "--full", "--no-tools"], 2, "agrep around: --max-chars cannot be combined with --full, which uncaps indexed message text; run: agrep around @01930000:2.18ba --full --no-tools"),
        (&["around", "@01930000:2.18ba", "--whole", "-C", "2"], 2, "agrep around: --whole reads the whole chat; drop -C N (or write -C all)"),
        (&["around", "@01930000:2.18ba", "-C", "0", "--whole", "--json"], 2, "agrep around: --whole reads the whole chat; drop -C N (or write -C all)"),
        (&["around", "@01930000:2.18ba", "-C", "-1", "--json"], 2, "agrep around: --context must be 0 or greater"),
        (&["around", "@01930000:2.18ba", "-C", "abc"], 2, "agrep around: argument -C/--context/--radius: expected an integer or 'all', got 'abc'"),
        (&["around", "@01930000:2.18ba", "--no-tools", "--who", "tool"], 1, "no tool messages or events in turns 2-2 of 01930000"),
        (&["retry", "--since", "2025-01-02", "--until", "2025-01-01"], 2, "empty time window: --since 2025-01-02 is at or after --until 2025-01-01, so [since, until) selects no time at all - swap them for the range between them"),
        (&["retry", "--since", "2025-01-01", "--until", "2025-01-01", "-c"], 2, "empty time window: --since 2025-01-01 and --until 2025-01-01 name the same instant, and [since, until) excludes its end, so it selects no time at all - use --until 2025-01-02 for the whole of 2025-01-01"),
        (&["retry", "--since", "1d", "--until", "1d"], 2, "empty time window: --since 1d and --until 1d name the same instant, and [since, until) excludes its end, so it selects no time at all - give --until a later moment"),
        (&["recall", "retry", "--since", "2025-01-02", "--until", "2025-01-01"], 2, "empty time window: --since 2025-01-02 is at or after --until 2025-01-01, so [since, until) selects no time at all - swap them for the range between them"),
        (&["pack", "retry", "--since", "2025-01-02", "--before", "2025-01-01"], 2, "empty time window: --since 2025-01-02 is at or after --until 2025-01-01, so [since, until) selects no time at all - swap them for the range between them"),
        (&["chats", "--since", "3d", "--until", "3d"], 2, "empty time window: --since 3d and --until 3d name the same instant, and [since, until) excludes its end, so it selects no time at all - give --until a later moment"),
        (&["summary", "--since", "2025-01-02", "--until", "2025-01-02"], 2, "empty time window: --since 2025-01-02 and --until 2025-01-02 name the same instant, and [since, until) excludes its end, so it selects no time at all - use --until 2025-01-03 for the whole of 2025-01-02"),
        (&["retry", "--since", "2025-01-01T00:00:00Z"], 2, "bad time '2025-01-01T00:00:00Z' - use 7d / 24h / 2w / 30m, YYYY-MM-DD or YYYY-MM-DDTHH:MM"),
        (&["retry", "--since", "1.5h"], 2, "bad time '1.5h' - use 7d / 24h / 2w / 30m, YYYY-MM-DD or YYYY-MM-DDTHH:MM"),
        (&["retry", "--since", "500ms"], 2, "bad time '500ms' - use 7d / 24h / 2w / 30m, YYYY-MM-DD or YYYY-MM-DDTHH:MM"),
        (&["retry", "--since", " 7 D ", "-c"], 1, ""),
        (&["summary", "--since", "2030-01-01"], 1, "0 projects, 0 chats since 2030-01-01, most active first · estimated active time with a 20m idle cap, side chats folded into their family"),
    ];
    for (args, code, stderr) in cases {
        let o = cli(&h, &d, args, &[]);
        assert_eq!(
            (
                o.status.code(),
                String::from_utf8_lossy(&o.stderr).trim_end()
            ),
            (Some(*code), *stderr),
            "{args:?}"
        );
        if *code == 2 {
            assert!(o.stdout.is_empty(), "{args:?}");
        }
    }
    // Absolute bounds are local wall time, with a space or `T` before the time.
    #[cfg(unix)]
    {
        let east = [("TZ", "America/New_York")];
        for (args, code, stdout) in [
            (vec!["retry", "--since", "2026-01-01T22:05", "-c"], 1, "0\n"),
            (vec!["retry", "--until", "2026-01-01 22:05", "-c"], 0, "3\n"),
        ] {
            let o = cli(&h, &d, &args, &east);
            assert_eq!(
                (o.status.code(), o.stdout.as_slice()),
                (Some(code), stdout.as_bytes())
            );
        }
        let summary = cli(
            &h,
            &d,
            &["summary", "time", "--since", "2026-01-01", "--json"],
            &east,
        );
        assert_eq!(
            values(&summary)[0]["window"]["since_ts"],
            1_767_243_600_000_i64
        );
    }
}
// Expected exit codes and stderr lines are agrep 0.4.9's: --who/--no-who skip blank list
// items, refuse an empty list or an unknown speaker by name, and refuse each other
// (py/surface_policy.py:632-664); a blank filter value names its vocabulary and what it
// would silently select (py/surface_policy.py:747-825, py/session_context.py:1500-1522).
#[test]
fn speaker_and_blank_filter_refusals_read_as_0_4_9() {
    let (_t, h, d) = pi();
    let speakers = "user, subagent, agent, tool, control, synthetic, recap, harness";
    let project =
        "a chat's stored project label or its last path segment, exact; * and ? make it a glob";
    let cases: Vec<(&[&str], String)> = vec![
        (&["retry", "--who", "user", "--no-who", "tool"], "agrep: --who and --no-who are mutually exclusive".into()),
        (&["retry", "-s", "--who", "tool,user", "--no-who", "agent"], "agrep: --who and --no-who are mutually exclusive".into()),
        (&["retry", "--who", ","], format!("agrep: empty speaker list (choose from {speakers})")),
        (&["retry", "--no-who", ", "], format!("agrep: empty speaker list (choose from {speakers})")),
        (&["retry", "--who", "bogus"], format!("agrep: unknown speaker 'bogus' (choose from {speakers})")),
        (&["retry", "--who", "it's"], format!("agrep: unknown speaker \"it's\" (choose from {speakers})")),
        (&["retry", "--who="], format!("agrep: --who needs a value ({speakers}) - an empty value would silently include every speaker")),
        (&["retry", "--who", "you,user,you", "--no-who="], format!("agrep: --no-who needs a value ({speakers}) - an empty value would silently exclude no speaker")),
        (&["recall", "retry", "--who", "user", "--no-who", "tool"], "agrep recall: --who and --no-who are mutually exclusive".into()),
        (&["recall", "retry", "--no-who", "user", "--who", "bogus"], format!("agrep recall: unknown speaker 'bogus' (choose from {speakers})")),
        (&["recall", "retry", "--who="], format!("agrep recall: --who needs a value ({speakers}) - an empty value would silently include every speaker")),
        (&["pack", "retry", "one", "--who", "user", "--no-who", "tool"], "agrep pack: --who and --no-who are mutually exclusive".into()),
        (&["pack", "retry", "--who", ", "], format!("agrep pack: empty speaker list (choose from {speakers})")),
        (&["retry", "--agent="], "--agent needs a value (claude, codex, opencode, antigravity, kimi, cline, gemini, crush, cursor, pi) - an empty value would silently match every agent".into()),
        (&["chats", "--agent= "], "--agent needs a value (claude, codex, opencode, antigravity, kimi, cline, gemini, crush, cursor, pi) - an empty value would silently match every agent".into()),
        (&["retry", "--project="], format!("agrep: --project needs a value ({project}) - an empty value would silently match every project")),
        (&["retry", "--exclude-project="], format!("agrep: --exclude-project needs a value ({project}) - an empty value would silently exclude nothing")),
        (&["retry", "--model="], "agrep: --model needs a value (an exact model name; --soft substring-matches) - an empty value would silently match every model".into()),
        (&["retry", "--chat="], "agrep: --chat needs a value (an 8-char id prefix (as shown) or a full session uuid) - an empty value would silently match every chat".into()),
        (&["retry", "--since="], "agrep: --since needs a value (7d / 24h / 2w / 30m, or 2026-06-01) - an empty value would silently match the whole history".into()),
        (&["recall", "retry", "--until", " "], "agrep recall: --until needs a value (7d / 24h / 2w / 30m, or 2026-06-01) - an empty value would silently match the whole history".into()),
        (&["chats", "--until="], "agrep chats: --until needs a value (7d / 24h / 2w / 30m, or 2026-06-01) - an empty value would silently match the whole history".into()),
        (&["summary", "--since="], "agrep summary: --since needs a value (7d / 24h / 2w / 30m, or 2026-06-01) - an empty value would silently match the whole history".into()),
        (&["chats", "--project="], format!("agrep chats: --project needs a value ({project}) - an empty value would silently match every project")),
        (&["summary", "--project", " "], format!("agrep summary: --project needs a value ({project}) - an empty value would silently match every project")),
        (&["chats", ""], "agrep chats: pattern arguments must not be empty".into()),
        // 0.4.9 checked the filters, in their order, before its option gates.
        (&["chats", "--project=", "--since="], format!("agrep chats: --project needs a value ({project}) - an empty value would silently match every project")),
        (&["summary", "--group", "day", "--since=", "--project="], format!("agrep summary: --project needs a value ({project}) - an empty value would silently match every project")),
        (&["retry", "--who=", "--since", "bad"], format!("agrep: --who needs a value ({speakers}) - an empty value would silently include every speaker")),
        (&["retry", "--soft", "--project="], format!("agrep: --project needs a value ({project}) - an empty value would silently match every project")),
        (&["retry", "--soft", "--who", "bogus"], format!("agrep: unknown speaker 'bogus' (choose from {speakers})")),
        (&["retry", "-c", "--json", "--who", "user", "--no-who", "tool"], "agrep: --who and --no-who are mutually exclusive".into()),
        (&["recall", "retry", "--hits", "0", "--project="], format!("agrep recall: --project needs a value ({project}) - an empty value would silently match every project")),
    ];
    for (args, stderr) in &cases {
        let o = cli(&h, &d, args, &[]);
        assert_eq!(
            (
                o.status.code(),
                String::from_utf8_lossy(&o.stderr).trim_end()
            ),
            (Some(2), stderr.as_str()),
            "{args:?}"
        );
        assert!(o.stdout.is_empty(), "{args:?}");
    }
    // A blank item is skipped, as is a repeat, so these read as `--who user`.
    let plain = ok(cli(&h, &d, &["retry", "--who", "user", "--json"], &[])).stdout;
    for who in ["user,", " user , ,you"] {
        let o = ok(cli(&h, &d, &["retry", "--who", who, "--json"], &[]));
        assert_eq!(o.stdout, plain, "{who}");
    }
    // around's --who names one speaker, as 0.4.9's choices did (py/around.py:826).
    let o = cli(
        &h,
        &d,
        &["around", "@01930000:2.18ba", "--who", "user,"],
        &[],
    );
    assert_eq!(o.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&o.stderr).lines().next(),
        Some("error: invalid value 'user,' for '--who <WHO>'")
    );
    ok(cli(
        &h,
        &d,
        &["around", "@01930000:2.18ba", "--who", "you"],
        &[],
    ));
}
// agrep 0.4.9 matched a word-less `chats` pattern such as `-` against chat identity alone
// (py/search.py:8764-8801): every fixture chat id holds a `-`.
#[test]
fn chats_matches_a_pattern_without_words_by_identity() {
    let (_t, h, d) = pi();
    let rows = values(&ok(cli(&h, &d, &["chats", "-", "--json"], &[])));
    assert_eq!(rows[0]["completeness"]["total"], 4);
    let sessions: Vec<&str> = rows[1..]
        .iter()
        .map(|r| {
            assert_eq!(r["match_source"], "identity");
            r["session"].as_str().unwrap()
        })
        .collect();
    assert_eq!(
        sessions,
        [
            "01950000-0000-7000-8000-000000000005",
            "01940000-0000-7000-8000-000000000003",
            "01930000-0000-7000-8000-000000000001",
            "01920000-0000-7000-8000-000000000002",
        ]
    );
}

// agrep 0.4.9's refusal of `--no-auto` without an index, and its record under `--json`
// less the publication age (py/indexd_runtime.py:5700-5706, py/search.py:7606-7612).
#[test]
fn no_auto_without_an_index_reads_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    let missing = "no index yet (--no-auto skipped the build). next: run `agrep setup` to make this machine's agent history searchable.";
    for args in [
        &["retry", "--no-auto"][..],
        &["chats", "--no-auto", "--json"],
        &["summary", "--no-auto"],
        &["around", "01930000", "0", "--no-auto"],
    ] {
        let o = cli(&h, &d, args, &[]);
        assert_eq!(
            (
                o.status.code(),
                String::from_utf8_lossy(&o.stderr).trim_end()
            ),
            (Some(2), missing),
            "{args:?}"
        );
        assert!(o.stdout.is_empty(), "{args:?}");
    }
    let unavailable = serde_json::json!({"query":"retry","engine":"index:unavailable","hits":[],"error":{"code":"index-unavailable"},"self_exclusion":{"active":false,"reason":"index-unavailable"},"filter_coverage":{"empty_dimensions":[],"checked":false,"reason":"the query returned before the index was filtered"},"freshness":{"state":"unavailable","failing":false,"checked":false,"may_be_stale":true,"code":"index-unavailable"},"semantic_coverage":null});
    let mut search = unavailable.clone();
    search["kind"] = "agrep-meta".into();
    search["completeness"] = serde_json::json!({"shown":0,"total":0,"total_basis":"floor","unit":"row","truncated":true,"action_unavailable_reason":"the query did not produce a result set"});
    let mut pack = unavailable.clone();
    pack["query"] = serde_json::json!(["retry", "cap"]);
    for (args, record) in [
        (&["retry", "--no-auto", "--json"][..], search),
        (&["recall", "retry", "--no-auto", "--json"], unavailable),
        (&["pack", "retry", "cap", "--no-auto", "--json"], pack),
    ] {
        let o = cli(&h, &d, args, &[]);
        assert_eq!(o.status.code(), Some(2), "{args:?}");
        assert_eq!(values(&o), [record], "{args:?}");
        assert_eq!(String::from_utf8_lossy(&o.stderr).trim_end(), missing);
    }
    assert!(!d.exists());
}

// agrep 0.4.9's lines when a page's automatic meaning lane had no model to run
// (py/search.py:8063-8104, py/surface_policy.py:2912-2958, py/recall.py:2601-2616).
#[test]
fn a_missing_model_reads_keyword_only_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    let dir = h.join(".pi/agent/sessions/-work-plain");
    fs::create_dir_all(&dir).unwrap();
    let id = "01970000-0000-7000-8000-000000000001";
    let lines = [
        serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/plain","timestamp":"2026-01-02T03:04:05.000Z"}),
        serde_json::json!({"type":"message","id":"u1","parentId":null,"timestamp":"2026-01-02T03:04:06.000Z","message":{"role":"user","content":[{"type":"text","text":"database migration failure in the staging cluster"}]}}),
    ];
    fs::write(
        dir.join(format!("2026-01-02T03-04-05-000Z_{id}.jsonl")),
        lines.iter().map(|l| format!("{l}\n")).collect::<String>(),
    )
    .unwrap();
    ok(cli(&h, &d, &["index"], &[]));
    let notice =
        "meaning unavailable; keyword-only (semantic model not installed: run agrep setup)";
    let compact = [("AGREP_PROFILE", "compact")];
    for (args, code, stderr) in [
        (&["database migration failure"][..], 0, notice.to_owned()),
        (&["zebra unicorn quantum"], 1, "no match across 1 session - meaning unavailable; keyword-only".to_owned()),
        (&["zebra unicorn quantum", "--no-auto"], 2, "no match across 1 session - history may be stale: automatic freshness checks are disabled by --no-auto".to_owned()),
        (&["zebra unicorn quantum", "--who", "user"], 1, format!("{notice}\nkeyword: 0 matching rows across 1 indexed messages")),
        (&["database migration failure", "--lexical"], 0, String::new()),
        (&["recall", "database migration failure"], 0, notice.to_owned()),
        (&["recall", "zebra unicorn quantum"], 2, format!("{notice}\nno hits across 1 indexed session.")),
        (&["recall", "zebra unicorn quantum", "--lexical"], 1, "no hits across 1 indexed session.".to_owned()),
    ] {
        let o = cli(&h, &d, args, &compact);
        assert_eq!(
            (o.status.code(), String::from_utf8_lossy(&o.stderr).trim_end()),
            (Some(code), stderr.as_str()),
            "{args:?}"
        );
    }
}

// Expected values are agrep 0.4.9's output on this fixture: a chat whose only user record
// is a synthetic sidecar mirror has no turn, so 0.4.9 neither listed it nor searched its
// tool calls, and both appear once a real turn arrives (py/corpusdb.py:2221-2234).
#[test]
fn chats_without_turns_stay_unlisted_and_unsearched_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    let (alpha, beta) = (
        "01970000-0000-7000-8000-000000000001",
        "01970000-0000-7000-8000-000000000002",
    );
    let write = |id: &str, day: u32, cwd: &str, records: &[Value]| {
        let dir = h
            .join(".pi/agent/sessions")
            .join(cwd.trim_start_matches('/').replace('/', "-"));
        fs::create_dir_all(&dir).unwrap();
        let ts = |n: usize| format!("2026-10-{day:02}T12:00:{n:02}.000Z");
        let mut lines = vec![
            serde_json::json!({"type":"session","id":id,"version":3,"cwd":cwd,"timestamp":ts(0)}),
        ];
        let mut parent = Value::Null;
        for (n, record) in records.iter().enumerate() {
            let mut message = record.clone();
            let rid = message["id"].take();
            message["timestamp"] = ts(n + 1).into();
            lines.push(serde_json::json!({"type":"message","id":rid,"parentId":parent,"timestamp":ts(n + 1),"message":message}));
            parent = rid;
        }
        let body: Vec<String> = lines.iter().map(Value::to_string).collect();
        fs::write(
            dir.join(format!("2026-10-{day:02}T12-00-00-000Z_{id}.jsonl")),
            body.join("\n") + "\n",
        )
        .unwrap();
    };
    let call = |n: usize, command: &str| {
        [
            serde_json::json!({"id":format!("a{n}"),"role":"assistant","model":"pi/sonnet-5","provider":"anthropic","stopReason":"toolUse","content":[{"type":"toolCall","id":format!("tc{n}"),"name":"bash","arguments":{"command":command}}]}),
            serde_json::json!({"id":format!("r{n}"),"role":"toolResult","toolCallId":format!("tc{n}"),"toolName":"bash","isError":false,"content":[{"type":"text","text":format!("{command}: found")}]}),
        ]
    };
    let user = |n: usize, text: &str| serde_json::json!({"id":format!("u{n}"),"role":"user","content":[{"type":"text","text":text}]});
    write(
        alpha,
        1,
        "/work/alpha",
        &[
            &[user(0, "find the needle")][..],
            &call(0, "grep needle src"),
        ]
        .concat(),
    );
    let mut mirror = user(0, "mirror of a watched chat");
    mirror["synthetic"] = true.into();
    let sidecar = [&[mirror][..], &call(0, "grep needle docs")].concat();
    write(beta, 2, "/work/beta", &sidecar);
    ok(cli(&h, &d, &["index"], &[]));
    let listed = |o: Output| -> Vec<Value> {
        values(&o)[1..]
            .iter()
            .map(|r| r["session"].clone())
            .collect()
    };
    let alpha_tool =
        format!("{alpha}\tpi\talpha\t0\ttool\tbash: grep needle src grep needle src: found\n");
    assert_eq!(listed(ok(cli(&h, &d, &["chats", "--json"], &[]))), [alpha]);
    let tools = ok(cli(&h, &d, &["needle", "--who", "tool"], &[]));
    assert_eq!(String::from_utf8(tools.stdout).unwrap(), alpha_tool);
    assert_eq!(ok(cli(&h, &d, &["needle", "-c"], &[])).stdout, b"2\n");
    assert_eq!(cli(&h, &d, &["around", beta], &[]).status.code(), Some(2));
    let status = values(&ok(cli(&h, &d, &["status", "--json"], &[])));
    assert_eq!(status[0]["index"]["counts"]["sessions"], 1);
    assert_eq!(status[0]["index"]["counts"]["tools"], 1);
    let answer = serde_json::json!({"id":"a1","role":"assistant","model":"pi/sonnet-5","provider":"anthropic","stopReason":"end_turn","content":[{"type":"text","text":"a real answer"}]});
    write(
        beta,
        2,
        "/work/beta",
        &[&sidecar[..], &[user(1, "now a real question"), answer]].concat(),
    );
    ok(cli(&h, &d, &["index"], &[]));
    assert_eq!(
        listed(ok(cli(&h, &d, &["chats", "--json"], &[]))),
        [beta, alpha]
    );
    let tools = ok(cli(&h, &d, &["needle", "--who", "tool"], &[]));
    assert_eq!(
        String::from_utf8(tools.stdout).unwrap(),
        format!("{beta}\tpi\tbeta\t0\ttool\tbash: grep needle docs grep needle docs: found\n{alpha_tool}")
    );
}

// Expected values are agrep 0.4.9's `chats --side --json` on the codex fixture: a chat's
// latest handle claimed its last row by turn, then time, then insertion, tool rows
// included, so a call timed after the reply is the claim (py/search.py:8624-8662). Session
// prefixes differ by design (PORTING.md), so only the turn and digest are compared.
#[test]
fn chats_latest_handle_claims_the_last_row_as_0_4_9() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    copy(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/codex/home"),
        &h,
    );
    ok(cli(&h, &d, &["index"], &[]));
    let rows = values(&ok(cli(
        &h,
        &d,
        &["chats", "--side", "--json", "-n", "50"],
        &[],
    )));
    let mut claims: Vec<(String, String)> = rows[1..]
        .iter()
        .map(|r| {
            let handle = r["latest_handle"].as_str().unwrap();
            (
                r["session"].as_str().unwrap().to_owned(),
                handle.split_once(':').unwrap().1.to_owned(),
            )
        })
        .collect();
    claims.sort();
    let expected: Vec<(String, String)> = [
        ("01990001-0002-7000-8000-000000000002", "0.5a5e"),
        ("01990002-0002-7000-8000-000000000002", "0.ca74"),
        ("01990003-0003-7000-8000-000000000003", "1.b260"),
        ("22222222-2222-4222-8222-222222222222", "1.fc20"),
        ("33333333-3333-4333-8333-333333333333", "1.fadf"),
    ]
    .iter()
    .map(|(s, c)| (s.to_string(), c.to_string()))
    .collect();
    assert_eq!(claims, expected);
}
// Expected lines are agrep 0.4.9's `index` and `reindex` reports on the pi fixture, times
// masked, less the lines about files 0.5 does not write (crates/agrep-cli/src/main.rs:
// 6245-6251, 7580-7615, reindex.py:72-85, 280-334). The statistics 0.5 printed instead
// are AGREP_QUERY_TIMING events, absent without the variable.
#[test]
fn index_reports_as_0_4_9_and_its_statistics_as_timing_events() {
    let t = TempDir::new().unwrap();
    let (h, d) = (t.path().join("h"), t.path().join("d"));
    copy(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/pi/home"),
        &h,
    );
    let times = regex::Regex::new(r"\d+(\.\d)?(ms|s)\b").unwrap();
    let run = |args: &[&str]| {
        let o = ok(cli(&h, &d, args, &[]));
        (
            times
                .replace_all(&String::from_utf8(o.stdout).unwrap(), "T")
                .into_owned(),
            String::from_utf8(o.stderr).unwrap(),
        )
    };
    let stats = |data: &Path| -> Value {
        let o = ok(cli(&h, data, &["index"], &[("AGREP_QUERY_TIMING", "1")]));
        String::from_utf8_lossy(&o.stderr)
            .lines()
            .filter_map(|line| serde_json::from_str::<Value>(line).ok())
            .find_map(|event| event.get("index").cloned())
            .expect("index statistics event")
    };
    let hint = "  next: `agrep setup` - teach your agents to search this history\n";
    let indexed = format!(
        "=== indexing transcripts ===\n  indexed 8 messages across 5 sessions (T)\n  phases: census T · parse T · write T · bounds T · stats T\n{hint}"
    );
    assert_eq!(
        run(&["index"]),
        (
            indexed.clone(),
            "building the search db (a first build can take a while on a large history) …\n".into()
        )
    );
    let built = stats(&t.path().join("d2"));
    assert_eq!(
        (built["sources"].clone(), built["parsed"].clone()),
        (5.into(), 5.into())
    );
    let unchanged = "=== indexing transcripts ===\n  unchanged since last index (8 messages); skipped ingest + writes (T)\n  phases: census T\n";
    assert_eq!(run(&["index"]), (unchanged.into(), String::new()));
    let again = stats(&d);
    assert_eq!(again["parsed"], 0);
    assert!(again["census_ms"].is_number() && again["after_refresh_ms"].is_number());
    assert!(!d.join("index-stats.json").exists());
    assert_eq!(
        run(&["reindex"]),
        (
            format!("\n=== ingest transcripts ===\n{}  (T)\n\n=== semantic embeddings (optional; not installed) ===\n  skipped without launching a worker; keyword search is ready. Enable it with `agrep setup`.\n\n  reindex complete in T.\n", &indexed["=== indexing transcripts ===\n".len()..]),
            "refreshing the search db …\n".into()
        )
    );
    // Enrollment retires the pointer, as 0.4.9's teach.json did.
    ok(cli(
        &h,
        &d,
        &["setup", "--yes", "--no-semantic", "--no-archive"],
        &[],
    ));
    let source = fs::read_dir(h.join(".pi/agent/sessions/-work-alpha"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let mut body = fs::read_to_string(&source).unwrap();
    body.push('\n');
    fs::write(&source, body).unwrap();
    let (stdout, _) = run(&["index", "--full"]);
    assert_eq!(stdout, indexed.replace(hint, ""));
}
// Expected records and lines are agrep 0.4.9's for these usage errors on this fixture
// (py/search.py:7502-7621, py/recall.py:1785-1975), less `corpus_age_s`, which 0.5 does
// not publish. A bad regex and a stale handle carry 0.5's own reason, the line stderr
// prints (PORTING.md).
#[test]
fn usage_errors_write_0_4_9_json_records() {
    use serde_json::json;
    let (t, h, d) = pi();
    let floor = json!({"shown":0,"total":0,"total_basis":"floor","unit":"row","truncated":true,"action_unavailable_reason":"the query did not produce a result set"});
    let unfiltered = json!({"empty_dimensions":[],"checked":false,"reason":"the query returned before the index was filtered"});
    let fresh = json!({"state":"no-known-failure","failing":false,"checked":true});
    let machine = json!({"active":false,"reason":"machine-surface"});
    let chat = json!({"active":false,"reason":"chat-filter"});
    let search = |query: &str, error: Value, exclusion: &Value| json!({"kind":"agrep-meta","query":query,"engine":"query:error","hits":[],"error":error,"self_exclusion":exclusion,"completeness":floor,"filter_coverage":unfiltered,"freshness":fresh,"semantic_coverage":null});
    let recall = |query: Value, engine: &str, error: Value, exclusion: &Value| json!({"query":query,"engine":engine,"hits":[],"error":error,"self_exclusion":exclusion,"filter_coverage":unfiltered,"freshness":fresh,"semantic_coverage":null});
    let window = "empty time window: --since 2026-02-01 is at or after --until 2026-01-01, so [since, until) selects no time at all - swap them for the range between them";
    let bad_time = "bad time 'garbage' - use 7d / 24h / 2w / 30m, YYYY-MM-DD or YYYY-MM-DDTHH:MM";
    let no_chat = "no session matches 'nosuchchat' - ids come from `agrep <pattern> --json`.";
    let empty = |query: Value| {
        recall(
            query,
            "query:error",
            json!({"code":"empty-query"}),
            &machine,
        )
    };
    let cases: Vec<(&[&str], Value, &str)> = vec![
        (&["--json"], search("", json!({"code":"empty-query"}), &machine), "empty pattern - add a word or phrase to search (a word that is also a command searches with: agrep search \"search\")"),
        (&["search", "--json", "--since", "garbage", "retry"], search("retry", json!({"code":"invalid-time-filter"}), &machine), bad_time),
        (&["search", "--json", "--since", "2026-02-01", "--until", "2026-01-01", "retry"], search("retry", json!({"code":"empty-time-window","reason":window}), &machine), window),
        (&["search", "--json", "--chat", "nosuchchat", "retry"], search("retry", json!({"code":"chat-unresolved"}), &chat), no_chat),
        (&["recall", "--json", ""], empty(json!("")), "query arguments must not be empty (searching the word itself: agrep search \"recall\")"),
        (&["pack", "--json", "", "retry"], empty(json!(["retry"])), "query arguments must not be empty (searching the word itself: agrep search \"pack\")"),
        (&["recall", "--json", "-s", "--lexical", "retry"], recall(json!("retry"), "query:error", json!({"code":"conflicting-modes"}), &machine), "-s and --lexical are contradictory; pick one"),
        (&["recall", "--json", "-s", "--who", "tool", "retry"], recall(json!("retry"), "query:error", json!({"code":"unsupported-speaker"}), &machine), "-s cannot search --who tool: tool rows are not embedded; use --lexical"),
        (&["recall", "--json", "--since", "garbage", "retry"], recall(json!("retry"), "query:error", json!({"code":"invalid-time-filter"}), &machine), bad_time),
        (&["pack", "--json", "retry", "--since", "2026-02-01", "--until", "2026-01-01"], recall(json!(["retry"]), "query:error", json!({"code":"empty-time-window","reason":window}), &machine), window),
        (&["recall", "--json", "--chat", "nosuchchat", "retry"], recall(json!("retry"), "query:error", json!({"code":"chat-unresolved"}), &chat), no_chat),
    ];
    for (args, expected, line) in cases {
        let o = cli(&h, &d, args, &[]);
        assert_eq!(o.status.code(), Some(2), "{args:?}");
        assert_eq!(
            serde_json::from_slice::<Value>(&o.stdout).unwrap(),
            expected,
            "{args:?}"
        );
        assert_eq!(
            String::from_utf8(o.stderr).unwrap(),
            format!("{line}\n"),
            "{args:?}"
        );
    }
    let o = cli(&h, &d, &["search", "--json", "-E", "a)(b"], &[]);
    let line = String::from_utf8(o.stderr).unwrap();
    let reason = line.trim_end().strip_prefix("bad regex: ").unwrap();
    assert_eq!((o.status.code(), line.lines().count()), (Some(2), 1));
    assert_eq!(
        serde_json::from_slice::<Value>(&o.stdout).unwrap(),
        search(
            "a)(b",
            json!({"code":"invalid-regex","reason":reason}),
            &machine
        )
    );
    let o = cli(&h, &d, &["recall", "--json", "@01930000:99.18ba"], &[]);
    let line = String::from_utf8(o.stderr).unwrap();
    assert_eq!(o.status.code(), Some(2));
    assert_eq!(
        serde_json::from_slice::<Value>(&o.stdout).unwrap(),
        recall(
            json!("@01930000:99.18ba"),
            "handle",
            json!({"code":"stale-result-handle","reason":line.trim_end()}),
            &machine
        )
    );
    // The bounds are read before the first index is built, as in 0.4.9.
    let unbuilt = t.path().join("unbuilt");
    let o = cli(
        &h,
        &unbuilt,
        &["search", "--json", "--since", "garbage", "retry"],
        &[],
    );
    assert_eq!(o.status.code(), Some(2));
    assert!(!unbuilt.join("index.db").exists());
}
/// Repeated runs print the same bytes on every result surface, and rows that tie on score
/// keep the documented order across blocks: newer first, then session, turn and speaker,
/// then row order. Timestamps in the future hold recency at one, so the clock cannot move a
/// score. Every eight messages share a time across three sessions whose ids sort against
/// their files, and the two tool calls of one reply tie on everything but their order.
#[test]
fn repeated_runs_print_the_same_bytes_on_every_surface() {
    let t = TempDir::new().unwrap();
    let h = t.path().join("h");
    let d = t.path().join("d");
    let dir = h.join(".pi/agent/sessions/-work-ties");
    fs::create_dir_all(&dir).unwrap();
    for (file, id) in [("1", "tie-c"), ("2", "tie-a"), ("3", "tie-b")] {
        let mut lines = vec![
            serde_json::json!({"type":"session","id":id,"version":3,"cwd":"/work/ties","timestamp":"2099-01-01T00:00:00Z"}),
        ];
        let mut parent = Value::Null;
        let mut add = |n: usize, message: Value| {
            let entry = format!("m{}", lines.len());
            let at = format!("2099-01-01T00:{:02}:{:02}Z", n / 8 / 60, n / 8 % 60);
            lines.push(serde_json::json!({"type":"message","id":entry,"parentId":parent,"timestamp":at,"message":message}));
            parent = entry.into();
        };
        for n in (0..600).step_by(2) {
            add(
                n,
                serde_json::json!({"role":"user","content":format!("zebra crossing check {n}")}),
            );
            if n % 50 != 0 {
                add(
                    n + 1,
                    serde_json::json!({"role":"assistant","content":[{"type":"text","text":format!("zebra noted {n}")}]}),
                );
                continue;
            }
            let call = |file: &str| serde_json::json!({"type":"toolCall","id":format!("{id}-{n}-{file}"),"name":"bash","arguments":{"command":format!("grep zebra src/{file}.rs")}});
            add(
                n + 1,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":format!("crossing then zebra {n}")},call("a"),call("b")]}),
            );
            for file in ["a", "b"] {
                add(
                    n + 1,
                    serde_json::json!({"role":"toolResult","toolCallId":format!("{id}-{n}-{file}"),"toolName":"bash","isError":false,"content":[{"type":"text","text":format!("src/{file}.rs:1: zebra")}]}),
                );
            }
        }
        let body: String = lines.iter().map(|line| format!("{line}\n")).collect();
        fs::write(dir.join(format!("{file}.jsonl")), body).unwrap();
    }
    ok(cli(&h, &d, &["index"], &[]));
    // A compact page's continuation handle names a new frozen page on each run.
    let masked = |bytes: &[u8]| {
        let text = String::from_utf8_lossy(bytes).into_owned();
        match text.find("--more m.") {
            Some(at) => format!("{}{}", &text[..at], &text[at + 17..]),
            None => text,
        }
    };
    // Each surface's arguments, and whether it runs under the compact profile.
    let surfaces: [(&[&str], bool); 17] = [
        (&["zebra", "--json", "-n", "0", "--no-auto"], false),
        (&["zebra", "--json", "-n", "40", "--no-auto"], false),
        (&["zebra", "--flat", "-n", "0", "--no-auto"], false),
        (
            &["zebra", "--sort", "time", "--json", "-n", "40", "--no-auto"],
            false,
        ),
        (&["zebra", "-l", "--json", "-n", "0", "--no-auto"], false),
        (&["zebra", "-l", "--no-auto"], false),
        (&["zebra", "--count-by-tier", "--no-auto"], false),
        (&["zebra", "-n", "60", "--no-auto", "--color=always"], false),
        (&["zebra", "--no-auto"], true),
        (&["zebra crossing", "--no-auto"], true),
        (&["zebra crossing", "--coverage", "--no-auto"], false),
        (&["recall", "zebra", "--no-auto"], false),
        (&["recall", "zebra crossing", "--json", "--no-auto"], false),
        (&["pack", "zebra", "crossing", "--no-auto"], false),
        (&["chats", "zebra", "--no-auto"], false),
        (&["chats", "--json", "-n", "0", "--no-auto"], false),
        (
            &["summary", "--json", "--since", "2026-01-01", "--no-auto"],
            false,
        ),
    ];
    for (args, compact) in surfaces {
        let env: &[(&str, &str)] = if compact {
            &[("AGREP_PROFILE", "compact")]
        } else {
            &[]
        };
        let runs: Vec<_> = (0..3)
            .map(|_| {
                let o = cli(&h, &d, args, env);
                (o.status.code(), o.stdout, masked(&o.stderr))
            })
            .collect();
        assert_eq!(runs[0].0, Some(0), "{args:?}: {}", runs[0].2);
        assert!(runs.iter().all(|run| *run == runs[0]), "{args:?}");
    }
    let rows = |n: &str| {
        values(&ok(cli(
            &h,
            &d,
            &["zebra", "--json", "-n", n, "--no-auto"],
            &[],
        )))[1..]
            .to_vec()
    };
    let all = rows("0");
    let order = |row: &Value| {
        (
            std::cmp::Reverse((row["score"].as_f64().unwrap() * 1e4).round() as i64),
            std::cmp::Reverse(row["ts"].as_i64().unwrap()),
            row["session"].as_str().unwrap().to_owned(),
            row["turn"].as_i64().unwrap(),
            row["who"].as_str().unwrap().to_owned(),
        )
    };
    assert!(all.windows(2).all(|w| order(&w[0]) <= order(&w[1])));
    // The newest time's prompts lead, three sessions of them tied in session order.
    let lead: Vec<_> = all[..12]
        .iter()
        .map(|row| row["session"].as_str().unwrap())
        .collect();
    assert_eq!(lead, [["tie-a"; 4], ["tie-b"; 4], ["tie-c"; 4]].concat());
    // Calls of one reply tie on everything else and keep the order they were made in.
    let tools: Vec<_> = all.iter().filter(|row| row["who"] == "tool").collect();
    assert_eq!(tools.len(), 72);
    for pair in tools.chunks(2) {
        assert_eq!(order(pair[0]), order(pair[1]));
        assert!(
            pair[0]["snippet"].as_str().unwrap().contains("src/a.rs"),
            "{pair:?}"
        );
        assert!(
            pair[1]["snippet"].as_str().unwrap().contains("src/b.rs"),
            "{pair:?}"
        );
    }
    assert_eq!(rows("40"), all[..40]);
}
