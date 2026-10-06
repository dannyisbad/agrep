//! A parser panic on one source costs that source, never the run. `AGREP_TEST_PARSE_PANIC` (test
//! builds only) panics the parse of one named source; every lane must keep its last-good rows,
//! disclose it without store text, publish other agents' churn, and reindex it once fixed.

mod common;

use common::*;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const PANIC_TARGET: &str = "AGREP_TEST_PARSE_PANIC";
const INJECTED_TEXT: &str = "injected transcript excerpt";
const CODEX_ROLLOUT: &str =
    "2026/01/02/rollout-2026-01-02T10-00-00-22222222-2222-4222-8222-222222222222.jsonl";

fn codex_rollout(home: &Path) -> PathBuf {
    join_native(&join_native(home, ".codex/sessions"), CODEX_ROLLOUT)
}
/// The panicking build, and the release that fixed its parser.
const BUILD: &str = "a1a1a1a1a1a1a1a1a1a1";
const FIXED_BUILD: &str = "b2b2b2b2b2b2b2b2b2b2";

/// `agrep-rs index --agent all <extra>` with nothing inherited but the sandbox.
fn run(home: &Path, data: &Path, build: &str, target: Option<&Path>, extra: &[&str]) -> Output {
    let mut cmd = Command::new(BIN);
    cmd.args(["index", "--agent", "all"])
        .args(extra)
        .env_clear()
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_RUNTIME_BUILD_ID", build);
    if let Some(target) = target {
        cmd.env(PANIC_TARGET, target);
    }
    cmd.output().expect("spawn agrep-rs")
}

fn index(home: &Path, data: &Path, target: Option<&Path>) -> Output {
    run(home, data, BUILD, target, &[])
}

/// A panic is deterministic for one build and one source: an unchanged source may stay served
/// from the last good pass until the build changes, as a parser fix ships.
fn index_fixed_build(home: &Path, data: &Path) -> Output {
    run(home, data, FIXED_BUILD, None, &[])
}

fn assert_published(output: &Output, step: &str) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    assert!(
        output.status.success(),
        "{step}: exit {:?}\nstdout:\n{}\nstderr:\n{stderr}",
        output.status.code(),
        String::from_utf8_lossy(&output.stdout),
    );
    stderr
}

fn messages(data: &Path) -> String {
    fs::read_to_string(data.join("messages.jsonl")).unwrap_or_default()
}

fn agent_events(data: &Path, agent: &str) -> Vec<(String, Vec<u8>)> {
    event_rows(data)
        .into_iter()
        .filter(|(name, _)| name.starts_with(agent))
        .collect()
}

/// The caught panic is one disclosed source issue: never the default hook's report, never the
/// panicking operand's store text.
fn assert_disclosed(data: &Path, stderr: &str, agent: &str, path: &Path) {
    let health: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".source-health.json")).unwrap()).unwrap();
    let issue = health["issues"]
        .as_array()
        .unwrap()
        .iter()
        .find(|issue| issue["agent"] == agent && issue["path"] == path.to_string_lossy().as_ref())
        .unwrap_or_else(|| panic!("no {agent} issue for {}: {health}", path.display()));
    assert_eq!(issue["kind"], "source-read-failed", "{issue}");
    let reason = issue["reason"].as_str().unwrap();
    assert!(reason.starts_with("parser panicked at "), "{reason}");
    assert!(
        reason.ends_with("called `Result::unwrap()` on an `Err` value: …"),
        "{reason}"
    );
    assert!(!health.to_string().contains(INJECTED_TEXT), "{health}");
    assert!(stderr.contains("parser panicked at "), "{stderr}");
    assert!(!stderr.contains(INJECTED_TEXT), "{stderr}");
    assert!(!stderr.contains("RUST_BACKTRACE"), "{stderr}");
}

fn append_claude_turn(source: &Path, text: &str) {
    let mut body = fs::read_to_string(source).unwrap();
    body.push_str(&format!(
        concat!(
            "{{\"type\":\"user\",\"userType\":\"external\",",
            "\"sessionId\":\"11111111-1111-4111-8111-111111111111\",",
            "\"timestamp\":\"2026-01-02T11:00:00.000Z\",\"cwd\":\"/work/alpha\",",
            "\"message\":{{\"role\":\"user\",\"content\":\"{}\"}}}}\n"
        ),
        text
    ));
    fs::write(source, body).unwrap();
}

fn append_codex_turn(source: &Path, text: &str) {
    let mut body = fs::read_to_string(source).unwrap();
    body.push_str(&format!(
        concat!(
            "{{\"type\":\"response_item\",\"timestamp\":\"2026-01-02T10:05:00.000Z\",",
            "\"payload\":{{\"type\":\"message\",\"role\":\"user\",",
            "\"content\":[{{\"type\":\"input_text\",\"text\":\"{0}\"}}]}}}}\n",
            "{{\"type\":\"event_msg\",\"timestamp\":\"2026-01-02T10:05:00.000Z\",",
            "\"payload\":{{\"type\":\"user_message\",\"message\":\"{0}\"}}}}\n"
        ),
        text
    ));
    fs::write(source, body).unwrap();
}

/// A claude + codex home: (home, the claude transcript to panic on, a codex rollout to churn).
fn claude_codex_home(tag: &str) -> (PathBuf, PathBuf, PathBuf) {
    let home = temp_dir(tag);
    copy_dir(&fixture_home("claude"), &home);
    copy_dir(&fixture_home("codex"), &home);
    let claude = join_native(&home, ".claude/projects/proj-alpha/sess-claude-0001.jsonl");
    let codex = codex_rollout(&home);
    (home, claude, codex)
}

#[test]
fn stat_lane_panic_keeps_last_good_rows_and_publishes_other_agents() {
    let (home, claude, codex) = claude_codex_home("parse-panic-stat-home");
    let data = temp_dir("parse-panic-stat-data");
    assert_published(&index(&home, &data, None), "first index");
    let indexed = messages(&data);
    for row in [
        "how do i fix the flaky timer test",
        "add a retry to the fetch helper",
    ] {
        assert!(indexed.contains(row), "{row} missing from the first index");
    }
    let claude_events = agent_events(&data, "claude");
    assert!(!claude_events.is_empty());
    let book = |data: &Path| -> serde_json::Value {
        let book: serde_json::Value =
            serde_json::from_slice(&fs::read(data.join("intake_stats.json")).unwrap()).unwrap();
        book["files"][claude.to_string_lossy().as_ref()].clone()
    };
    let last_good_tally = book(&data);
    assert!(
        last_good_tally["rows"].as_u64().unwrap() >= 2,
        "{last_good_tally}"
    );

    // Two panicking passes: the second runs the guarded-retry lane the first one leaves behind.
    for round in 1..=2 {
        append_claude_turn(&claude, &format!("claude churn {round}"));
        append_codex_turn(&codex, &format!("codex churn {round}"));
        let output = index(&home, &data, Some(&claude));
        let stderr = assert_published(&output, &format!("panicking pass {round}"));
        let published = messages(&data);
        assert!(
            published.contains(&format!("codex churn {round}")),
            "other agent froze"
        );
        for row in indexed
            .lines()
            .filter(|row| row.contains("\"agent\":\"claude\""))
        {
            assert!(published.contains(row), "last-good row dropped: {row}");
        }
        assert!(
            !published.contains("claude churn"),
            "the panicking parse published rows"
        );
        assert_eq!(agent_events(&data, "claude"), claude_events);
        assert_eq!(
            book(&data),
            last_good_tally,
            "a half-counted tally replaced the last good one"
        );
        assert_disclosed(&data, &stderr, "claude", &claude);
        check_intake_identity(&data);
    }

    let stderr = assert_published(&index(&home, &data, None), "healed pass");
    assert!(!stderr.contains("parser panicked"), "{stderr}");
    let healed = messages(&data);
    for row in [
        "claude churn 1",
        "claude churn 2",
        "codex churn 2",
        "how do i fix the flaky",
    ] {
        assert!(
            healed.contains(row),
            "{row} missing after the parser healed"
        );
    }
    assert!(!data.join(".source-health.json").exists());
    assert_ne!(book(&data), last_good_tally);
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn stat_lane_panic_on_a_fresh_box_does_not_block_other_agents() {
    let (home, claude, codex) = claude_codex_home("parse-panic-fresh-home");
    let data = temp_dir("parse-panic-fresh-data");
    let stderr = assert_published(&index(&home, &data, Some(&claude)), "fresh panicking pass");
    let published = messages(&data);
    assert!(
        published.contains("add a retry to the fetch helper"),
        "codex was blocked"
    );
    assert!(!published.contains("how do i fix the flaky timer test"));
    assert_disclosed(&data, &stderr, "claude", &claude);

    append_codex_turn(&codex, "codex churn");
    let stderr = assert_published(&index(&home, &data, Some(&claude)), "second panicking pass");
    assert!(
        messages(&data).contains("codex churn"),
        "codex froze on the second pass"
    );
    assert_disclosed(&data, &stderr, "claude", &claude);

    assert_published(&index_fixed_build(&home, &data), "fixed-build pass");
    assert!(messages(&data).contains("how do i fix the flaky timer test"));
    assert!(!data.join(".source-health.json").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// The first-search stream is JSON lines on stdout: a caught panic must leave it parseable.
#[test]
fn emit_rows_stream_stays_parseable_through_a_parser_panic() {
    let (home, claude, _) = claude_codex_home("parse-panic-emit-home");
    let data = temp_dir("parse-panic-emit-data");
    let output = run(&home, &data, BUILD, Some(&claude), &["--emit-rows"]);
    let stderr = assert_published(&output, "streaming pass");
    let stdout = String::from_utf8(output.stdout).unwrap();
    let texts: Vec<String> = stdout
        .lines()
        .filter(|line| line.starts_with('{'))
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .filter_map(|record| record["row"]["text"].as_str().map(str::to_owned))
        .collect();
    assert!(texts
        .iter()
        .any(|text| text == "add a retry to the fetch helper"));
    assert!(!texts
        .iter()
        .any(|text| text == "how do i fix the flaky timer test"));
    assert!(stderr.contains("parser panicked at "), "{stderr}");
    assert!(!stderr.contains("RUST_BACKTRACE"), "{stderr}");
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

#[test]
fn token_lane_panic_keeps_each_conversation_last_good() {
    let home = crush_home();
    copy_dir(&fixture_home("codex"), &home);
    let crush = join_native(&home, ".local/share/crush/crush.db");
    let codex = codex_rollout(&home);
    let data = temp_dir("parse-panic-token-data");
    assert_published(&index(&home, &data, None), "first index");
    let indexed = messages(&data);
    assert!(indexed.contains("convert the readme to asciidoc"));

    let connection = rusqlite::Connection::open(&crush).unwrap();
    connection
        .execute_batch(concat!(
            "INSERT INTO messages(id, session_id, role, parts, model, created_at, updated_at) ",
            "VALUES ('m9', 'sc1', 'user', '[{\"type\":\"text\",\"data\":{\"text\":\"crush churn\"}}]',",
            " NULL, 1767348900000, 1767348900000);",
            "UPDATE sessions SET updated_at = 1767348900000 WHERE id = 'sc1';",
        ))
        .unwrap();
    drop(connection);
    append_codex_turn(&codex, "codex churn");
    let output = index(&home, &data, Some(&crush));
    let stderr = assert_published(&output, "panicking pass");
    let published = messages(&data);
    assert!(published.contains("codex churn"), "other agent froze");
    for row in indexed
        .lines()
        .filter(|row| row.contains("\"agent\":\"crush\""))
    {
        assert!(published.contains(row), "last-good row dropped: {row}");
    }
    assert!(!published.contains("crush churn"));
    assert_disclosed(&data, &stderr, "crush", &crush);

    assert_published(&index(&home, &data, None), "healed pass");
    assert!(messages(&data).contains("crush churn"));
    assert!(!data.join(".source-health.json").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A whole-store agent's fixture plus codex: (home, the session the injected panic lands in, the
/// file whose parse panics, a codex rollout). Without `sibling` it is the store's only session.
fn whole_store_home(tag: &str, agent: &str, sibling: bool) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let home = temp_dir(tag);
    copy_dir(&fixture_home(agent), &home);
    copy_dir(&fixture_home("codex"), &home);
    let codex = codex_rollout(&home);
    let (session, target) = match agent {
        "kimi" => {
            let session = fs::read_dir(join_native(&home, ".kimi/sessions"))
                .unwrap()
                .flatten()
                .next()
                .unwrap()
                .path()
                .join("44444444-4444-4444-8444-444444444444");
            if !sibling {
                fs::remove_dir_all(session.join("subagents")).unwrap();
            }
            let target = session.join("wire.jsonl");
            (session, target)
        }
        "antigravity" => {
            let brain = join_native(&home, ".gemini/antigravity-cli/brain");
            let session = brain.join("33333333-3333-4333-8333-333333333333");
            if sibling {
                copy_dir(
                    &session,
                    &brain.join("66666666-6666-4666-8666-666666666666"),
                );
            }
            let target = join_native(&session, ".system_generated/logs/transcript.jsonl");
            (session, target)
        }
        _ => unreachable!("not a whole-store fixture: {agent}"),
    };
    (home, session, target, codex)
}

#[test]
fn always_lane_panic_keeps_the_session_last_good() {
    let (home, session, _, codex) = whole_store_home("parse-panic-always-home", "kimi", true);
    let data = temp_dir("parse-panic-always-data");
    assert_published(&index(&home, &data, None), "first index");
    let indexed = messages(&data);
    assert!(indexed.contains("port the script to argparse"));

    let context = session.join("context.jsonl");
    let mut body = fs::read_to_string(&context).unwrap();
    body.push_str("{\"role\":\"user\",\"content\":\"kimi churn\"}\n");
    fs::write(&context, body).unwrap();
    append_codex_turn(&codex, "codex churn");
    let output = index(&home, &data, Some(&session.join("wire.jsonl")));
    let stderr = assert_published(&output, "panicking pass");
    let published = messages(&data);
    assert!(published.contains("codex churn"), "other agent froze");
    for row in indexed
        .lines()
        .filter(|row| row.contains("\"agent\":\"kimi\""))
    {
        assert!(published.contains(row), "last-good row dropped: {row}");
    }
    assert!(!published.contains("kimi churn"));
    assert_disclosed(&data, &stderr, "kimi", &session);

    assert_published(&index_fixed_build(&home, &data), "fixed-build pass");
    assert!(messages(&data).contains("kimi churn"));
    assert!(!data.join(".source-health.json").exists());
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// With no last-good snapshot (the parse cache was lost, or `--full` starts cold) the rows a
/// whole-store pass just read cannot vouch for the session that panicked: the published
/// generation is kept until that session parses again.
#[test]
fn whole_store_panic_without_last_good_rows_keeps_the_published_generation() {
    for agent in ["kimi", "antigravity"] {
        let (home, session, target, codex) =
            whole_store_home(&format!("parse-panic-cold-{agent}-home"), agent, true);
        let data = temp_dir(&format!("parse-panic-cold-{agent}-data"));
        assert_published(&index(&home, &data, None), "first index");
        let published = normalize(&data);
        let session_id = session.file_name().unwrap().to_string_lossy().into_owned();
        assert!(published.contains(&session_id));
        // Churn elsewhere keeps the unchanged-source shortcut from skipping the parse.
        for (round, extra) in [&[][..], &["--full"][..]].into_iter().enumerate() {
            for cache in [".ingest_cache.bin", ".ingest_cache.bin.journal"] {
                let _ = fs::remove_file(data.join(cache));
            }
            append_codex_turn(&codex, &format!("codex churn {round}"));
            let output = run(&home, &data, BUILD, Some(&target), extra);
            let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
            assert!(
                !output.status.success(),
                "{agent} {extra:?} published without its panicking session:\n{}",
                String::from_utf8_lossy(&output.stdout)
            );
            assert!(stderr.contains("retained the old generation"), "{stderr}");
            assert_eq!(normalize(&data), published, "{agent} {extra:?}");
            assert_disclosed(&data, &stderr, agent, &session);
        }
        assert_published(&index(&home, &data, None), "healed pass");
        let healed = normalize(&data);
        assert!(healed.contains("codex churn 1"));
        let agent_row = format!("\"agent\":\"{agent}\"");
        for row in published.lines().filter(|row| row.contains(&agent_row)) {
            assert!(healed.contains(row), "{agent} lost a published row: {row}");
        }
        assert!(!data.join(".source-health.json").exists());
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// A whole-store agent that never published a row has nothing a panic could cost, so its only
/// session panicking pass after pass, warm or cold, never freezes the other agents.
#[test]
fn never_indexed_whole_store_that_keeps_panicking_does_not_freeze_other_agents() {
    for agent in ["kimi", "antigravity"] {
        let (home, session, target, codex) = whole_store_home(
            &format!("parse-panic-unpublished-{agent}-home"),
            agent,
            false,
        );
        let data = temp_dir(&format!("parse-panic-unpublished-{agent}-data"));
        let agent_row = format!("\"agent\":\"{agent}\"");
        for (round, extra) in [&[][..], &[][..], &["--full"][..]].into_iter().enumerate() {
            let churn = format!("codex churn {round}");
            append_codex_turn(&codex, &churn);
            let output = run(&home, &data, BUILD, Some(&target), extra);
            let stderr = assert_published(&output, &format!("{agent} panicking pass {round}"));
            let published = messages(&data);
            assert!(
                published.contains(&churn),
                "{agent} froze codex on pass {round}"
            );
            assert!(!published.contains(&agent_row));
            assert_disclosed(&data, &stderr, agent, &session);
        }
        assert_published(&index_fixed_build(&home, &data), "fixed-build pass");
        assert!(messages(&data).contains(&agent_row));
        assert!(!data.join(".source-health.json").exists());
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// An unchanged pass publishes nothing, so the disclosure of the pass that did stands as it was:
/// the session still panics, beside any preflight issue the same snapshot records.
#[test]
fn an_unchanged_pass_keeps_the_disclosure_of_the_publication_it_skips() {
    for foreign_crush in [false, true] {
        let (home, claude, _) = claude_codex_home("parse-panic-unchanged-home");
        if foreign_crush {
            let foreign = home.join(".crush").join("crush.db");
            fs::create_dir_all(foreign.parent().unwrap()).unwrap();
            fs::write(foreign, b"plain text where crush keeps its database\n").unwrap();
        }
        let data = temp_dir("parse-panic-unchanged-data");
        let stderr = assert_published(&index(&home, &data, Some(&claude)), "panicking pass");
        assert_disclosed(&data, &stderr, "claude", &claude);
        let health = fs::read(data.join(".source-health.json")).unwrap();

        let output = index(&home, &data, Some(&claude));
        assert_published(&output, "unchanged pass");
        assert!(
            String::from_utf8_lossy(&output.stdout).contains("unchanged since last index"),
            "the second pass reparsed"
        );
        assert_eq!(
            fs::read(data.join(".source-health.json")).ok(),
            Some(health),
            "foreign crush {foreign_crush}: the unchanged pass rewrote the disclosure"
        );
        let _ = fs::remove_dir_all(&home);
        let _ = fs::remove_dir_all(&data);
    }
}

/// A data dir that never recorded its harness policy (an `--emit-rows` first index before this
/// build took none) records it with its first publication: a source it keeps failing to read
/// holds the source snapshot back, yet costs one full corpus refresh, not one on every pass.
#[test]
fn the_first_publication_records_the_policy_even_while_it_holds_the_snapshot_back() {
    let (home, claude, codex) = claude_codex_home("parse-panic-policy-home");
    let data = temp_dir("parse-panic-policy-data");
    let first = run(&home, &data, BUILD, None, &["--emit-rows"]);
    assert!(first.status.success(), "--emit-rows index failed");
    assert!(!data.join(".source_snapshot.bin").exists());
    let _ = fs::remove_file(data.join(".harness_prefixes.snapshot"));

    for round in 1..=2 {
        let churn = format!("codex churn {round}");
        append_codex_turn(&codex, &churn);
        append_claude_turn(&claude, &format!("claude churn {round}"));
        // Its consumer deletes the changed-session delta once it has applied it.
        let _ = fs::remove_file(data.join(".changed_sessions"));
        let stderr = assert_published(&index(&home, &data, Some(&claude)), &churn);
        assert!(
            messages(&data).contains(&churn),
            "{churn} was not published"
        );
        assert_disclosed(&data, &stderr, "claude", &claude);
        assert!(!data.join(".source_snapshot.bin").exists());
        let changed = fs::read_to_string(data.join(".changed_sessions")).unwrap();
        if round > 1 {
            assert_ne!(changed, "*\n", "pass {round} marked every session changed");
        }
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}

/// A crash between the renames of a publication leaves its proof stale, never its files
/// illegible: a never-indexed store that keeps panicking still costs the other agents nothing.
#[test]
fn a_torn_publication_still_admits_a_never_indexed_panicking_store() {
    let (home, session, target, codex) = whole_store_home("parse-panic-torn-home", "kimi", false);
    let data = temp_dir("parse-panic-torn-data");
    assert_published(&run(&home, &data, BUILD, Some(&target), &[]), "first index");
    for (round, extra) in [&[][..], &["--full"][..]].into_iter().enumerate() {
        // As if killed between renames: messages moved on, the generation proof did not.
        let published = data.join("messages.jsonl");
        let body = fs::read(&published).unwrap();
        fs::remove_file(&published).unwrap();
        fs::write(&published, body).unwrap();
        let churn = format!("codex churn {round}");
        append_codex_turn(&codex, &churn);
        let output = run(&home, &data, BUILD, Some(&target), extra);
        let stderr = assert_published(&output, &format!("torn {extra:?} pass"));
        assert!(messages(&data).contains(&churn), "{churn} froze");
        assert!(!messages(&data).contains("\"agent\":\"kimi\""));
        assert_disclosed(&data, &stderr, "kimi", &session);
    }
    let _ = fs::remove_dir_all(&home);
    let _ = fs::remove_dir_all(&data);
}
