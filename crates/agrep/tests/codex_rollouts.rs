//! Codex rollouts as their files hold them: every rollout's file id resolves with `--chat`
//! and `why`, a guardian's verdicts are keyword rows, and `audit --full` reads each rollout
//! itself, so a row the index lacks is a gap whatever the parser decided.
mod support;
use rusqlite::Connection;
use serde_json::{json, Value};
use std::{fs, path::PathBuf, process::Output};
use tempfile::TempDir;

struct Home {
    _t: TempDir,
    h: PathBuf,
    d: PathBuf,
}
impl Home {
    fn new() -> Self {
        let t = TempDir::new().unwrap();
        let (h, d) = (t.path().join("h"), t.path().join("d"));
        Self { _t: t, h, d }
    }
    /// Writes the rollout of `id` (`<chat>_<id>` for a file Codex started when it resumed a
    /// chat) and returns its path.
    fn rollout(&self, name: &str, records: &[Value]) -> String {
        let path = self
            .h
            .join(".codex/sessions/2026/11/06")
            .join(format!("rollout-2026-11-06T10-00-00-{name}.jsonl"));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let body: String = records.iter().map(|r| format!("{r}\n")).collect();
        fs::write(&path, body).unwrap();
        path.to_string_lossy().into_owned()
    }
    fn run(&self, args: &[&str]) -> Output {
        std::process::Command::new(support::binary())
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", &self.h)
            .env("AGREP_HOME", &self.h)
            .env("AGREP_DATA_DIR", &self.d)
            .env("AGREP_MODEL_DIR", self.h.join("no-model"))
            .env("AGREP_CALLER_PUBLICATION_DIR", self.h.join("empty-callers"))
            .env("AGREP_PROFILE", "compact")
            .args(args)
            .output()
            .unwrap()
    }
    fn json(&self, args: &[&str]) -> (Value, Option<i32>) {
        let out = self.run(args);
        let value = serde_json::from_slice(&out.stdout)
            .unwrap_or_else(|e| panic!("{args:?}: {e}: {}", text(&out.stdout)));
        (value, out.status.code())
    }
}
fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

const PARENT: &str = "01a30000-0001-7000-8000-000000000001";
const FORK: &str = "01a30000-0002-7000-8000-000000000002";
const TEAM: &str = "01a30000-0003-7000-8000-000000000003";
const TEAM2: &str = "01a30000-0004-7000-8000-000000000004";
const REVIEWED: &str = "01a30000-0005-7000-8000-000000000005";
const INTERRUPTED: &str = "01a30000-0006-7000-8000-000000000006";
const REVIEW_THREAD: &str = "01a30000-0007-7000-8000-000000000007";
const EMPTY_VOICE: &str = "01a30000-0008-7000-8000-000000000008";
const RESUMED: &str = "01a30000-0009-7000-8000-000000000009";
const GUARDIAN: &str = "01a30000-000a-7000-8000-00000000000a";
const VOICE: &str = "01a30000-000b-7000-8000-00000000000b";
const REVIEWER: &str = "01a30000-000c-7000-8000-00000000000c";

fn record(at: &str, ty: &str, payload: Value) -> Value {
    json!({"timestamp":at,"type":ty,"payload":payload})
}
fn message(at: &str, role: &str, body: &str, turn: Option<&str>) -> Value {
    let kind = if role == "assistant" {
        "output_text"
    } else {
        "input_text"
    };
    let mut payload = json!({"type":"message","role":role,"content":[{"type":kind,"text":body}]});
    if let Some(turn) = turn {
        payload["internal_chat_message_metadata_passthrough"] = json!({"turn_id":turn});
    }
    record(at, "response_item", payload)
}
/// A prompt the user typed, with the submission Codex logs for it.
fn typed(at: &str, body: &str) -> [Value; 2] {
    [
        message(at, "user", body, None),
        record(
            at,
            "event_msg",
            json!({"type":"user_message","message":body}),
        ),
    ]
}
/// The Desktop app's record that `body` was submitted in `thread`'s `turn`.
fn submitted(at: &str, thread: &str, turn: &str, body: &str) -> Value {
    record(
        at,
        "event_msg",
        json!({"type":"item_completed","thread_id":thread,"turn_id":turn,"item":{"type":"UserMessage","content":[{"type":"text","text":body}]}}),
    )
}
fn call(at: &str, id: &str, input: &str) -> [Value; 2] {
    [
        record(
            at,
            "response_item",
            json!({"type":"custom_tool_call","call_id":id,"name":"apply_patch","input":input,"status":"completed"}),
        ),
        record(
            at,
            "response_item",
            json!({"type":"custom_tool_call_output","call_id":id,"output":"Exit code: 0"}),
        ),
    ]
}
fn started(at: &str, turn: &str) -> Value {
    record(
        at,
        "event_msg",
        json!({"type":"task_started","turn_id":turn}),
    )
}
/// Team mode's inbound message: an assistant message holding a JSON envelope.
fn envelope(at: &str, author: &str, recipient: &str, body: &str) -> Value {
    let envelope = json!({"author":author,"recipient":recipient,"other_recipients":[],"content":"","encrypted_content":body,"trigger_turn":true});
    message(at, "assistant", &envelope.to_string(), None)
}
fn spawned(id: &str, parent: &str, agent_path: &str, depth: u32, at: &str) -> Value {
    record(
        at,
        "session_meta",
        json!({"id":id,"timestamp":at,"forked_from_id":parent,"parent_thread_id":parent,"cwd":"/work/cache","thread_source":"subagent","agent_path":agent_path,"source":{"subagent":{"thread_spawn":{"parent_thread_id":parent,"depth":depth,"agent_path":agent_path}}}}),
    )
}
/// A copy of `records` as a fork child replays them: the same payloads, written anew.
fn replayed(at: &str, records: &[Value]) -> Vec<Value> {
    records
        .iter()
        .map(|r| json!({"timestamp":at,"type":r["type"],"payload":r["payload"]}))
        .collect()
}

/// The rollout paths of a home holding every kind of Codex rollout whose rows live
/// somewhere other than under its own file id, or nowhere.
struct Rollouts {
    parent: String,
    team2: String,
    voice: String,
}

fn codex_home(home: &Home) -> Rollouts {
    let parent_meta = record(
        "2026-11-06T10:00:00.000Z",
        "session_meta",
        json!({"id":PARENT,"timestamp":"2026-11-06T10:00:00.000Z","cwd":"/work/cache","source":"cli"}),
    );
    // Before the fork: what the children copy.
    let mut early = vec![
        message(
            "2026-11-06T10:00:01.000Z",
            "user",
            "# AGENTS.md instructions\n<INSTRUCTIONS>keep it small</INSTRUCTIONS>",
            None,
        ),
        message(
            "2026-11-06T10:00:02.000Z",
            "user",
            "<environment_context>\n  <cwd>/work/cache</cwd>\n</environment_context>",
            None,
        ),
    ];
    early.extend(typed(
        "2026-11-06T10:00:05.000Z",
        "Build the cache layer for the fetch helper",
    ));
    early.extend(call(
        "2026-11-06T10:00:06.000Z",
        "call_cache_1",
        "*** Add File: cache.rs",
    ));
    // A reply past the reply cap, then text past it in the same turn.
    early.push(message(
        "2026-11-06T10:00:07.000Z",
        "assistant",
        &format!(
            "The cache layer is built behind the fetch helper. {}",
            "y".repeat(64_100)
        ),
        None,
    ));
    early.push(message(
        "2026-11-06T10:00:08.000Z",
        "assistant",
        "Also flushed the stale entries.",
        None,
    ));
    let mut parent = vec![parent_meta.clone()];
    parent.extend(early.clone());
    parent.extend(typed(
        "2026-11-06T10:05:00.000Z",
        "after the fork only the parent saw this",
    ));
    parent.push(message("2026-11-06T10:05:01.000Z", "assistant", "ok", None));
    let parent_path = home.rollout(PARENT, &parent);

    // A file Codex started when it resumed the chat: its rows are the chat's.
    let mut resumed = vec![record(
        "2026-11-06T12:00:00.000Z",
        "session_meta",
        json!({"id":PARENT,"timestamp":"2026-11-06T12:00:00.000Z","cwd":"/work/cache","source":"vscode"}),
    )];
    resumed.extend(typed(
        "2026-11-06T12:00:05.000Z",
        "resumed after lunch: add the cache size flag",
    ));
    resumed.push(message(
        "2026-11-06T12:00:06.000Z",
        "assistant",
        "Added --cache-size.",
        None,
    ));
    home.rollout(&format!("{PARENT}_{RESUMED}"), &resumed);

    // A fork child that holds nothing but the history it copied.
    let fork_at = "2026-11-06T10:01:00.000Z";
    let mut fork = vec![
        spawned(FORK, PARENT, "/root/idle", 1, fork_at),
        replayed(fork_at, std::slice::from_ref(&parent_meta)).remove(0),
    ];
    fork.extend(replayed(fork_at, &early));
    home.rollout(FORK, &fork);

    // A team-mode child and its own child: each copies its ancestors' history, then its
    // task arrives as an envelope in its own first turn.
    let mut team_own = vec![
        message(fork_at, "developer", "You are an agent in a team", None),
        started(
            "2026-11-06T10:01:01.000Z",
            "01a30000-0003-7001-8000-000000000031",
        ),
        envelope(
            "2026-11-06T10:01:02.000Z",
            "/root",
            "/root/sub_alpha",
            "Write sub_alpha.txt containing exactly alpha.",
        ),
    ];
    team_own.extend(call(
        "2026-11-06T10:01:03.000Z",
        "call_team_1",
        "*** Add File: sub_alpha.txt",
    ));
    team_own.push(message(
        "2026-11-06T10:01:04.000Z",
        "assistant",
        "sub_alpha.txt is written.",
        None,
    ));
    let team_meta = spawned(TEAM, PARENT, "/root/sub_alpha", 1, fork_at);
    let mut team = vec![
        team_meta.clone(),
        replayed(fork_at, std::slice::from_ref(&parent_meta)).remove(0),
    ];
    team.extend(replayed(fork_at, &early));
    team.extend(team_own.clone());
    home.rollout(TEAM, &team);

    let deep_at = "2026-11-06T10:02:00.000Z";
    let mut team2 = vec![
        spawned(TEAM2, TEAM, "/root/sub_alpha/sub_alpha", 2, deep_at),
        replayed(deep_at, &[team_meta]).remove(0),
        replayed(deep_at, &[parent_meta]).remove(0),
    ];
    team2.extend(replayed(deep_at, &early));
    team2.extend(replayed(deep_at, &team_own));
    team2.push(started(
        "2026-11-06T10:02:01.000Z",
        "01a30000-0004-7001-8000-000000000041",
    ));
    team2.push(envelope(
        "2026-11-06T10:02:02.000Z",
        "/root/sub_alpha",
        "/root/sub_alpha/sub_alpha",
        "Create the file in the active project root.",
    ));
    team2.push(message(
        "2026-11-06T10:02:03.000Z",
        "assistant",
        "The platform blocked that tool invocation.",
        None,
    ));
    team2.extend(call(
        "2026-11-06T10:02:04.000Z",
        "call_team2_1",
        "*** Update File: main_beta.txt",
    ));
    let team2_path = home.rollout(TEAM2, &team2);

    // A review with a result, written as newer Codex builds write records (an ordinal after
    // each timestamp): review mode opens, the review thread receives its request, Codex
    // composes a notice of the result, review mode closes and the result arrives. Its review
    // thread holds the review's work.
    let (review_turn, reviewer_turn) = (
        "01a30000-0005-7001-8000-000000000051",
        "01a30000-000c-7001-8000-0000000000c1",
    );
    let review_request =
        "Review the cache layer changes against 'main'.\nProvide prioritized findings.";
    let result = "No actionable regressions found in the cache layer.";
    let reviewed: Vec<Value> = [
        record(
            "2026-11-06T11:00:00.000Z",
            "session_meta",
            json!({"id":REVIEWED,"cwd":"/work/review","thread_source":"user","source":"exec"}),
        ),
        record(
            "2026-11-06T11:00:00.000Z",
            "event_msg",
            json!({"type":"item_completed","thread_id":REVIEWED,"turn_id":review_turn,"item":{"type":"EnteredReviewMode","target":{"type":"baseBranch","branch":"main"},"user_facing_hint":"changes against 'main'"}}),
        ),
        started("2026-11-06T11:00:01.000Z", reviewer_turn),
        submitted(
            "2026-11-06T11:00:02.000Z",
            REVIEWER,
            reviewer_turn,
            review_request,
        ),
        message(
            "2026-11-06T11:00:30.000Z",
            "user",
            &format!("<user_action>\n  <action>review</action>\n  <results>\n  {result}\n  </results>\n</user_action>"),
            Some(review_turn),
        ),
        record(
            "2026-11-06T11:00:30.000Z",
            "event_msg",
            json!({"type":"item_completed","thread_id":REVIEWED,"turn_id":review_turn,"item":{"type":"ExitedReviewMode","review_output":{"overall_explanation":result}}}),
        ),
        message("2026-11-06T11:00:31.000Z", "assistant", result, Some(review_turn)),
    ]
    .into_iter()
    .enumerate()
    .map(|(ordinal, r)| json!({"timestamp":r["timestamp"],"ordinal":ordinal,"type":r["type"],"payload":r["payload"]}))
    .collect();
    home.rollout(REVIEWED, &reviewed);
    let mut reviewer = vec![
        record(
            "2026-11-06T11:00:01.000Z",
            "session_meta",
            json!({"id":REVIEWER,"cwd":"/work/review","thread_source":"subagent","parent_thread_id":REVIEWED,"source":{"subagent":"review"}}),
        ),
        started("2026-11-06T11:00:01.000Z", reviewer_turn),
        message(
            "2026-11-06T11:00:02.000Z",
            "user",
            review_request,
            Some(reviewer_turn),
        ),
        submitted(
            "2026-11-06T11:00:02.000Z",
            REVIEWER,
            reviewer_turn,
            review_request,
        ),
    ];
    reviewer.extend(call(
        "2026-11-06T11:00:10.000Z",
        "call_reviewer_1",
        "git diff main -- cache.rs",
    ));
    reviewer.push(message(
        "2026-11-06T11:00:29.000Z",
        "assistant",
        "The cache layer keeps its eviction order.",
        Some(reviewer_turn),
    ));
    home.rollout(REVIEWER, &reviewer);
    let (entered, thread_turn) = (
        "01a30000-0006-7001-8000-000000000061",
        "01a30000-0007-7001-8000-000000000071",
    );
    let request = "Review the cache diff against main.";
    home.rollout(
        INTERRUPTED,
        &[
            record(
                "2026-11-06T11:10:00.000Z",
                "session_meta",
                json!({"id":INTERRUPTED,"cwd":"/work/cache","thread_source":"user","source":"exec"}),
            ),
            record(
                "2026-11-06T11:10:00.000Z",
                "event_msg",
                json!({"type":"item_completed","thread_id":INTERRUPTED,"turn_id":entered,"item":{"type":"EnteredReviewMode"}}),
            ),
            started("2026-11-06T11:10:01.000Z", thread_turn),
            submitted("2026-11-06T11:10:02.000Z", REVIEW_THREAD, thread_turn, request),
        ],
    );
    let mut thread = vec![
        record(
            "2026-11-06T11:10:01.000Z",
            "session_meta",
            json!({"id":REVIEW_THREAD,"cwd":"/work/cache","thread_source":"subagent","parent_thread_id":INTERRUPTED,"source":{"subagent":"review"}}),
        ),
        started("2026-11-06T11:10:01.000Z", thread_turn),
        message(
            "2026-11-06T11:10:02.000Z",
            "user",
            request,
            Some(thread_turn),
        ),
        submitted(
            "2026-11-06T11:10:02.000Z",
            REVIEW_THREAD,
            thread_turn,
            request,
        ),
        message(
            "2026-11-06T11:10:03.000Z",
            "assistant",
            "The diff drops the retry ceiling.",
            Some(thread_turn),
        ),
    ];
    thread.extend(call(
        "2026-11-06T11:10:04.000Z",
        "call_review_1",
        "git diff main",
    ));
    home.rollout(REVIEW_THREAD, &thread);

    // A voice chat in which nothing was said, and one with a request and its closing flush.
    home.rollout(
        EMPTY_VOICE,
        &[
            record(
                "2026-11-06T11:20:00.000Z",
                "session_meta",
                json!({"id":EMPTY_VOICE,"cwd":"/work/cache","thread_source":"voice_chat","source":"vscode"}),
            ),
            message(
                "2026-11-06T11:20:00.000Z",
                "developer",
                "Realtime voice mode",
                None,
            ),
            message(
                "2026-11-06T11:20:00.000Z",
                "user",
                "# AGENTS.md instructions\n<INSTRUCTIONS>keep it small</INSTRUCTIONS>",
                None,
            ),
        ],
    );
    // The request took only part of what was said, and the flush carries the transcript's
    // tail: the words only the transcripts hold are searchable too.
    let (asked, ended) = (
        "01a30000-000b-7001-8000-0000000000b1",
        "01a30000-000b-7002-8000-0000000000b2",
    );
    let ask = "<realtime_delegation>\n  <input>is a donut too much sugar</input>\n  <transcript_delta>user: honestly though is a donut too much sugar\nassistant: Let me look that up.</transcript_delta>\n</realtime_delegation>";
    let flush = "<realtime_delegation>\n  <source>transcript_tail_flush</source>\n  <input>The user just ended their realtime session.</input>\n  <transcript_delta>assistant: Anything else today?\nuser: no thanks, goodbye</transcript_delta>\n</realtime_delegation>";
    let voice_path = home.rollout(
        VOICE,
        &[
            record(
                "2026-11-06T11:30:00.000Z",
                "session_meta",
                json!({"id":VOICE,"cwd":"/work/cache","thread_source":"voice_chat","source":"vscode"}),
            ),
            message("2026-11-06T11:30:01.000Z", "user", ask, Some(asked)),
            submitted("2026-11-06T11:30:01.000Z", VOICE, asked, ask),
            message(
                "2026-11-06T11:30:02.000Z",
                "assistant",
                "[COMPLETE] A donut is fine.",
                Some(asked),
            ),
            message("2026-11-06T11:30:03.000Z", "user", flush, Some(ended)),
            submitted("2026-11-06T11:30:03.000Z", VOICE, ended, flush),
            message(
                "2026-11-06T11:30:04.000Z",
                "assistant",
                "Voice session ended.",
                Some(ended),
            ),
        ],
    );

    // A guardian's review of the parent's next action.
    home.rollout(
        GUARDIAN,
        &[
            record(
                "2026-11-06T10:30:00.000Z",
                "session_meta",
                json!({"id":GUARDIAN,"cwd":"/work/cache","parent_thread_id":PARENT,"thread_source":"guardian_review","source":{"subagent":{"other":"guardian"}}}),
            ),
            record(
                "2026-11-06T10:30:00.000Z",
                "turn_context",
                json!({"model":"codex-auto-review"}),
            ),
            message(
                "2026-11-06T10:30:01.000Z",
                "user",
                "The following is the Codex agent history whose request action you are assessing.",
                None,
            ),
            message(
                "2026-11-06T10:30:02.000Z",
                "assistant",
                r#"{"risk_level":"low","outcome":"allow","rationale":"A read-only listing of the cache directory."}"#,
                None,
            ),
        ],
    );
    Rollouts {
        parent: parent_path,
        team2: team2_path,
        voice: voice_path,
    }
}

/// A search's count, exit code and stderr within `--chat chat`.
fn count(home: &Home, pattern: &str, chat: &str) -> (String, Option<i32>, String) {
    let out = home.run(&["search", pattern, "-c", "--chat", chat]);
    (
        text(&out.stdout).trim().to_owned(),
        out.status.code(),
        text(&out.stderr),
    )
}

#[test]
fn every_codex_rollout_id_resolves_with_chat_and_why() {
    let home = Home::new();
    codex_home(&home);
    let out = home.run(&["index"]);
    assert!(out.status.success(), "{}", text(&out.stderr));
    let prefix = |id: &str| id[..13].to_owned();

    // Rollouts that are chats of their own, among them the review with a result, the
    // team-mode children and the guardian.
    for (chat, phrase) in [
        (TEAM, "sub_alpha.txt is written"),
        (TEAM2, "platform blocked that tool invocation"),
        (REVIEWED, "No actionable regressions"),
        (GUARDIAN, "read-only listing of the cache directory"),
        (VOICE, "is a donut too much sugar"),
    ] {
        let (n, code, err) = count(&home, phrase, &prefix(chat));
        assert_eq!((n.as_str(), code), ("1", Some(0)), "{chat}: {err}");
        assert!(!err.contains("names rollout"), "{chat}: {err}");
        let (why, code) = home.json(&["why", &prefix(chat), "--json"]);
        assert_eq!(
            (why["verdict"].as_str(), code),
            (Some("indexed"), Some(0)),
            "{why}"
        );
    }
    let (why, _) = home.json(&["why", TEAM2, "--json"]);
    assert_eq!(why["candidates"][0]["parent"], TEAM, "{why}");

    // A file Codex started when it resumed a chat searches that chat.
    let (n, code, err) = count(&home, "add the cache size flag", &prefix(RESUMED));
    assert_eq!((n.as_str(), code), ("1", Some(0)), "{err}");
    assert!(
        err.contains(&format!(
            "--chat {} names rollout {RESUMED}, a file of chat {PARENT}: searching that chat",
            prefix(RESUMED)
        )),
        "{err}"
    );
    let (why, code) = home.json(&["why", &prefix(RESUMED), "--json"]);
    assert_eq!((why["verdict"].as_str(), code), (Some("indexed"), Some(0)));
    assert_eq!(why["candidates"][0]["session"], PARENT);

    // A fork child holding only copied history searches it, up to the fork.
    let (n, code, err) = count(&home, "Build the cache layer", &prefix(FORK));
    assert_eq!((n.as_str(), code), ("1", Some(0)), "{err}");
    assert!(
        err.contains(&format!(
            "a fork holding only history it copied from {PARENT}"
        )),
        "{err}"
    );
    let (n, code, _) = count(&home, "after the fork only the parent", &prefix(FORK));
    assert_eq!((n.as_str(), code), ("0", Some(1)));
    let (why, code) = home.json(&["why", &prefix(FORK), "--json"]);
    assert_eq!((why["verdict"].as_str(), code), (Some("indexed"), Some(0)));

    // An interrupted review holds no rows; its review thread holds the review's work.
    let (n, code, err) = count(&home, "drops the retry ceiling", &prefix(INTERRUPTED));
    assert_eq!((n.as_str(), code), ("1", Some(0)), "{err}");
    assert!(
        err.contains(&format!(
            "a review interrupted before its result: searching its review thread {REVIEW_THREAD}"
        )),
        "{err}"
    );
    let (why, code) = home.json(&["why", &prefix(INTERRUPTED), "--json"]);
    assert_eq!(
        (why["verdict"].as_str(), code),
        (Some("not-indexed"), Some(1))
    );
    assert!(
        why["summary"]
            .as_str()
            .unwrap()
            .contains("holds no searchable rows: a review interrupted before its result"),
        "{why}"
    );

    // An empty voice chat resolves, finds nothing and says why.
    let (n, code, err) = count(&home, "donut", &prefix(EMPTY_VOICE));
    assert_eq!((n.as_str(), code), ("0", Some(1)), "{err}");
    assert!(
        err.contains("which holds no searchable rows: nothing in it was typed, answered or run"),
        "{err}"
    );
    let (why, code) = home.json(&["why", &prefix(EMPTY_VOICE), "--json"]);
    assert_eq!(
        (why["verdict"].as_str(), code),
        (Some("not-indexed"), Some(1))
    );
    assert!(why["summary"]
        .as_str()
        .unwrap()
        .contains("nothing in it was typed, answered or run"));

    // A genuine unknown id is still refused.
    let (_, code, err) = count(&home, "cache", "01a3ffff-0000");
    assert_eq!(code, Some(2), "{err}");
    assert!(err.contains("no session matches '01a3ffff-0000'"), "{err}");
    let (why, code) = home.json(&["why", "01a3ffff-0000", "--json"]);
    assert_eq!(
        (why["verdict"].as_str(), code),
        (Some("not-indexed"), Some(1))
    );
}

#[test]
fn guardian_verdicts_are_keyword_rows_of_the_reviewed_chat() {
    let home = Home::new();
    codex_home(&home);
    let out = home.run(&["index"]);
    assert!(out.status.success(), "{}", text(&out.stderr));
    let out = home.run(&[
        "search",
        "read-only listing of the cache directory",
        "--json",
    ]);
    assert!(out.status.success(), "{}", text(&out.stderr));
    let rows: Vec<Value> = text(&out.stdout)
        .lines()
        .skip(1)
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 1, "{}", text(&out.stdout));
    assert_eq!(
        (&rows[0]["session"], &rows[0]["who"]),
        (&json!(GUARDIAN), &json!("subagent"))
    );
    let c = Connection::open(home.d.join("index.db")).unwrap();
    let verdicts: Vec<String> = c
        .prepare("SELECT CAST(text AS TEXT) FROM prose WHERE session=?1")
        .unwrap()
        .query_map([GUARDIAN], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(
        verdicts,
        [
            r#"[guardian verdict] {"risk_level":"low","outcome":"allow","rationale":"A read-only listing of the cache directory."}"#
        ]
    );
    let (why, _) = home.json(&["why", GUARDIAN, "--json"]);
    assert_eq!(why["candidates"][0]["parent"], PARENT);
    // The request it answers quotes the reviewed chat and is not a row.
    let (n, code, _) = count(&home, "whose request action you are assessing", GUARDIAN);
    assert_eq!((n.as_str(), code), ("0", Some(1)));
}

/// JSON lines of `args`' stdout.
fn json_lines(home: &Home, args: &[&str]) -> Vec<Value> {
    let out = home.run(args);
    assert!(out.status.success(), "{args:?}: {}", text(&out.stderr));
    text(&out.stdout)
        .lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect()
}

/// A chat that holds only a review is titled with the review's request and counts the review
/// as its turn, with a handle `around` opens. Its review thread folds into it, and the
/// project counts the review once, as it counted the thread's turn while the chat had no row.
#[test]
fn a_review_chat_is_titled_and_counts_its_review() {
    let home = Home::new();
    codex_home(&home);
    let out = home.run(&["index"]);
    assert!(out.status.success(), "{}", text(&out.stderr));
    let title = "[review] Review the cache layer changes against 'main'.";

    let chats = json_lines(&home, &["chats", "--json", "-n", "50"]);
    let chat = chats
        .iter()
        .find(|c| c["session"] == REVIEWED)
        .unwrap_or_else(|| panic!("{chats:?}"));
    let rows = json_lines(
        &home,
        &["summary", "--since", "2026-11-01", "--json", "-n", "0"],
    );
    let project = rows
        .iter()
        .find(|r| r["kind"] == "project" && r["project"] == "review")
        .unwrap_or_else(|| panic!("{rows:?}"));
    let worked = &project["worked_on"][0];
    // chats: title and turns; summary: the project's chats and turns, then the review
    // chat's title, turns and side chats.
    assert_eq!(
        (
            (chat["first_text"].as_str(), chat["turns"].as_u64()),
            (project["chats"].as_u64(), project["turns"].as_u64()),
            (
                worked["session"].as_str(),
                worked["first_text"].as_str(),
                worked["turns"].as_u64(),
                worked["side_chats"].as_u64()
            )
        ),
        (
            (Some(title), Some(1)),
            (Some(1), Some(1)),
            (Some(REVIEWED), Some(title), Some(1), Some(1))
        ),
        "{chat}\n{project}"
    );
    let handle = worked["latest_handle"]
        .as_str()
        .unwrap_or_else(|| panic!("{worked}"));
    let out = home.run(&["around", handle]);
    assert!(out.status.success(), "{}", text(&out.stderr));
    assert!(
        text(&out.stdout).contains("No actionable regressions found in the cache layer"),
        "{}",
        text(&out.stdout)
    );
}

/// What only a voice session's transcripts record, the words its request left out and its
/// closing flush's tail, is searchable in the voice chat, and `audit --full` finds it lost.
#[test]
fn a_voice_transcript_is_searchable_and_audited() {
    let home = Home::new();
    let rollouts = codex_home(&home);
    let out = home.run(&["index"]);
    assert!(out.status.success(), "{}", text(&out.stderr));
    for phrase in [
        "honestly though",
        "no thanks, goodbye",
        "Let me look that up",
        "Anything else today",
    ] {
        let (n, code, err) = count(&home, phrase, &VOICE[..13]);
        assert_eq!((n.as_str(), code), ("1", Some(0)), "{phrase}: {err}");
    }
    let (audit, code) = home.json(&["audit", "--full", "--strict", "--json"]);
    assert_eq!(
        (audit["gaps"].as_u64(), code),
        (Some(0), Some(0)),
        "{audit}"
    );

    let c = Connection::open(home.d.join("index.db")).unwrap();
    c.execute(
        "DELETE FROM prose WHERE session=?1 AND turn=1 AND who='user'",
        [VOICE],
    )
    .unwrap();
    drop(c);
    let (audit, code) = home.json(&["audit", "--full", "--strict", "--json"]);
    assert_eq!(code, Some(2), "{audit}");
    let gaps: Vec<_> = audit["gap_details"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|gap| gap["reason"] == "rollout row missing from index")
        .map(|gap| {
            (
                gap["path"].as_str().unwrap().to_owned(),
                gap["who"].as_str().unwrap().to_owned(),
                gap["rows"].as_u64().unwrap(),
            )
        })
        .collect();
    assert_eq!(gaps, [(rollouts.voice, "user".to_owned(), 2)], "{audit}");
}

#[test]
fn audit_full_reads_each_codex_rollout_itself() {
    let home = Home::new();
    let rollouts = codex_home(&home);
    let out = home.run(&["index"]);
    assert!(out.status.success(), "{}", text(&out.stderr));
    // Every exclusion holds: text Codex composed, copied history, messages between agents,
    // a guardian's quoted request, the note a voice session's flush adds, text past the reply
    // cap.
    let (audit, code) = home.json(&["audit", "--full", "--strict", "--json"]);
    assert_eq!(
        (audit["gaps"].as_u64(), code),
        (Some(0), Some(0)),
        "{audit}"
    );

    // A reply and a call the rollouts hold but the index lacks are gaps, named by file.
    let c = Connection::open(home.d.join("index.db")).unwrap();
    c.execute(
        "DELETE FROM prose WHERE session=?1 AND who='agent'",
        [TEAM2],
    )
    .unwrap();
    c.execute("DELETE FROM tools WHERE extra='call_cache_1'", [])
        .unwrap();
    drop(c);
    let (audit, code) = home.json(&["audit", "--full", "--strict", "--json"]);
    assert_eq!(code, Some(2), "{audit}");
    let gaps: Vec<_> = audit["gap_details"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|gap| gap["reason"] == "rollout row missing from index")
        .map(|gap| {
            (
                gap["path"].as_str().unwrap().to_owned(),
                gap["who"].as_str().unwrap().to_owned(),
                gap["rows"].as_u64().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        gaps,
        [
            (rollouts.parent, "tool".to_owned(), 1),
            (rollouts.team2, "agent".to_owned(), 1),
        ],
        "{audit}"
    );
}
