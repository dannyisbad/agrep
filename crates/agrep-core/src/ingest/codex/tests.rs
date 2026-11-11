use std::path::{Path, PathBuf};

use super::discover::rollout_stem;
use super::parse_file;
use crate::intake_batch::ReadOutcome;

const NAME: &str = "rollout-2026-06-09T10-00-00-01a00000-0000-7000-8000-000000000001.jsonl";

/// A rollout whose session id and one call id come from its filename, so a compressed copy
/// keeps them only if it derives them from the same stem.
fn rollout() -> String {
    [
        serde_json::json!({"timestamp":"2026-06-09T10:00:00Z","type":"session_meta","payload":{"cwd":"/work/zstd"}}),
        serde_json::json!({"timestamp":"2026-06-09T10:00:01Z","type":"event_msg","payload":{"type":"user_message","message":"compressed prompt"}}),
        serde_json::json!({"timestamp":"2026-06-09T10:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"compressed prompt"}]}}),
        serde_json::json!({"timestamp":"2026-06-09T10:00:02Z","type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{\"command\":[\"ls\"]}"}}),
        serde_json::json!({"timestamp":"2026-06-09T10:00:03Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"compressed reply"}]}}),
    ]
    .map(|line| line.to_string() + "\n")
    .concat()
}

fn write(dir: &Path, name: &str, bytes: &[u8]) -> PathBuf {
    let path = dir.join(name);
    std::fs::write(&path, bytes).unwrap();
    path
}

fn compressed(text: &str) -> Vec<u8> {
    zstd::encode_all(text.as_bytes(), 3).unwrap()
}

fn parsed(path: &Path) -> (String, String, ReadOutcome, bool) {
    let ((messages, events, outcome), tallies) = crate::intake::scoped(|| parse_file(path));
    (
        format!("{messages:?}"),
        format!("{events:?}"),
        outcome,
        crate::intake::errors(&tallies),
    )
}

#[test]
fn compressed_rollout_parses_like_its_plain_copy() {
    let plain_dir = tempfile::tempdir().unwrap();
    let compressed_dir = tempfile::tempdir().unwrap();
    let text = rollout();
    let plain = parsed(&write(plain_dir.path(), NAME, text.as_bytes()));
    let packed = parsed(&write(
        compressed_dir.path(),
        &format!("{NAME}.zst"),
        &compressed(&text),
    ));
    assert_eq!(plain.2, ReadOutcome::Complete);
    assert!(plain.0.contains("compressed prompt") && plain.0.contains("compressed reply"));
    assert!(plain
        .1
        .contains("codex:rollout-2026-06-09T10-00-00-01a00000"));
    assert_eq!(packed, plain);
}

#[test]
fn compressed_rollout_reads_every_concatenated_frame() {
    let dir = tempfile::tempdir().unwrap();
    let text = rollout();
    let (head, tail) = text.split_at(text.len() / 2);
    let frames = [compressed(head), compressed(tail)].concat();
    let path = write(dir.path(), &format!("{NAME}.zst"), &frames);
    let plain = write(dir.path(), NAME, text.as_bytes());
    assert_eq!(parsed(&path), parsed(&plain));
}

#[test]
fn damaged_compressed_rollouts_fail_without_rows() {
    let dir = tempfile::tempdir().unwrap();
    let whole = compressed(&rollout());
    let mut corrupt = whole.clone();
    let middle = corrupt.len() / 2;
    corrupt[middle..].iter_mut().for_each(|b| *b = !*b);
    for (name, bytes) in [
        ("truncated", &whole[..whole.len() - 4]),
        ("corrupt", &corrupt[..]),
        ("empty", &[][..]),
        ("plain-text", rollout().as_bytes()),
    ] {
        let path = write(dir.path(), &format!("rollout-{name}.jsonl.zst"), bytes);
        let (messages, events, outcome, errors) = parsed(&path);
        assert_eq!((messages.as_str(), events.as_str()), ("[]", "[]"), "{name}");
        assert_ne!(outcome, ReadOutcome::Complete, "{name}");
        assert!(errors, "{name}");
    }
}

#[test]
fn compressed_rollouts_share_their_plain_stem() {
    for name in [NAME.to_owned(), format!("{NAME}.zst")] {
        assert_eq!(
            rollout_stem(Path::new(&name)),
            Some("rollout-2026-06-09T10-00-00-01a00000-0000-7000-8000-000000000001")
        );
    }
    assert_eq!(rollout_stem(Path::new("rollout-a.zst")), Some("rollout-a"));
}

#[test]
fn list_shaped_call_output_indexes_its_text_items() {
    let long = "y".repeat(900);
    let lines = [
        serde_json::json!({"timestamp":"2026-06-09T10:00:00Z","type":"session_meta","payload":{"cwd":"/work/list"}}),
        serde_json::json!({"timestamp":"2026-06-09T10:00:01Z","type":"response_item","payload":{"type":"function_call","name":"js","call_id":"list-call","arguments":"{}"}}),
        serde_json::json!({"timestamp":"2026-06-09T10:00:02Z","type":"response_item","payload":{"type":"function_call_output","call_id":"list-call","output":[{"type":"input_text","text":"Script completed\nOutput:\n"},{"type":"input_image","image_url":"data:image/png;base64,AAAA"},{"type":"input_text","text":""},{"type":"input_text","text":format!("Process exited with code 0 \"quoted\" {long}")}]}}),
    ]
    .map(|line| line.to_string() + "\n")
    .concat();
    let dir = tempfile::tempdir().unwrap();
    let path = write(dir.path(), NAME, lines.as_bytes());
    let ((_, events, outcome), _) = crate::intake::scoped(|| parse_file(&path));
    assert_eq!(outcome, ReadOutcome::Complete);
    let event = events.iter().find(|e| e.call_id == "list-call").unwrap();
    let full = format!("Script completed\nOutput:\n\nProcess exited with code 0 \"quoted\" {long}");
    assert_eq!(
        event.output,
        crate::ingest::cap_str(&full, crate::ingest::EVENT_CAP)
    );
    assert_eq!(
        (event.output_chars, event.output_bytes, event.ok),
        (full.chars().count(), full.len(), Some(true))
    );
}

fn record(ty: &str, payload: serde_json::Value) -> serde_json::Value {
    serde_json::json!({"timestamp":"2026-06-10T21:33:07.000Z","type":ty,"payload":payload})
}

fn started(turn: &str) -> serde_json::Value {
    record(
        "event_msg",
        serde_json::json!({"type":"task_started","turn_id":turn}),
    )
}

fn said(role: &str, text: &str, turn: Option<&str>) -> serde_json::Value {
    let kind = if role == "assistant" {
        "output_text"
    } else {
        "input_text"
    };
    let mut payload =
        serde_json::json!({"type":"message","role":role,"content":[{"type":kind,"text":text}]});
    if let Some(turn) = turn {
        payload["internal_chat_message_metadata_passthrough"] = serde_json::json!({"turn_id":turn});
    }
    record("response_item", payload)
}

/// The Desktop app's record that the user submitted `text` in `thread`'s `turn`.
fn submitted(thread: &str, turn: &str, text: &str) -> serde_json::Value {
    record(
        "event_msg",
        serde_json::json!({"type":"item_completed","thread_id":thread,"turn_id":turn,"item":{"type":"UserMessage","content":[{"type":"text","text":text}]}}),
    )
}

fn call(id: &str, input: &str) -> [serde_json::Value; 2] {
    [
        record(
            "response_item",
            serde_json::json!({"type":"custom_tool_call","call_id":id,"name":"apply_patch","input":input,"status":"completed"}),
        ),
        record(
            "response_item",
            serde_json::json!({"type":"custom_tool_call_output","call_id":id,"output":"Exit code: 0"}),
        ),
    ]
}

/// Team mode's inbound message: an assistant message carrying a JSON envelope.
fn envelope(author: &str, recipient: &str, body: &str) -> serde_json::Value {
    let text = serde_json::json!({"author":author,"recipient":recipient,"other_recipients":[],"content":"","encrypted_content":body,"trigger_turn":true});
    said("assistant", &text.to_string(), None)
}

/// Each row of the rollout of `id` as (session, parent, text, reply), and each call as
/// (session, call id).
type Parse = (Vec<[String; 4]>, Vec<[String; 2]>);

fn parse_rollout(id: &str, records: &[serde_json::Value]) -> Parse {
    let dir = tempfile::tempdir().unwrap();
    let body: String = records.iter().map(|r| r.to_string() + "\n").collect();
    let name = format!("rollout-2026-06-10T14-33-07-{id}.jsonl");
    let path = write(dir.path(), &name, body.as_bytes());
    let ((messages, events, outcome), _) = crate::intake::scoped(|| parse_file(&path));
    assert_eq!(outcome, ReadOutcome::Complete);
    let rows = messages
        .iter()
        .map(|m| [&m.session, &m.parent, &m.text, &m.reply].map(|s| s.to_string()))
        .collect();
    let calls = events
        .iter()
        .map(|e| [e.session.clone(), e.call_id.clone()])
        .collect();
    (rows, calls)
}

fn row(session: &str, parent: &str, text: &str, reply: &str) -> [String; 4] {
    [session, parent, text, reply].map(str::to_string)
}

const ROOT: &str = "01a20000-0000-7000-8000-000000000001";
const CHILD: &str = "01a20000-0001-7000-8000-000000000002";
const GRANDCHILD: &str = "01a20000-0002-7000-8000-000000000003";
const ROOT_PROMPT: &str = "Two independent parts: spawn sub_alpha, write main_beta yourself";

fn team_meta(id: &str, parent: Option<&str>, agent_path: &str, depth: u32) -> serde_json::Value {
    let payload = match parent {
        None => {
            serde_json::json!({"id":id,"cwd":"/work/team","thread_source":"user","source":"exec"})
        }
        Some(parent) => {
            serde_json::json!({"id":id,"forked_from_id":parent,"parent_thread_id":parent,"cwd":"/work/team","thread_source":"subagent","agent_path":agent_path,"source":{"subagent":{"thread_spawn":{"parent_thread_id":parent,"depth":depth,"agent_path":agent_path}}}})
        }
    };
    record("session_meta", payload)
}

/// What a team-mode child replays of the root before its own turn: the root's turn, its
/// prompt and the prompt's submission.
fn root_replay() -> Vec<serde_json::Value> {
    vec![
        started("01a20000-0000-7001-8000-000000000004"),
        said("user", ROOT_PROMPT, None),
        record(
            "event_msg",
            serde_json::json!({"type":"user_message","message":ROOT_PROMPT}),
        ),
    ]
}

/// A depth-1 child whose team messages arrive as envelopes: every own row is kept, under
/// the child, and nothing of the root's replayed history.
#[test]
fn a_team_child_keeps_every_row_after_its_own_turn_starts() {
    let mut records = vec![
        team_meta(CHILD, Some(ROOT), "/root/sub_alpha", 1),
        team_meta(ROOT, None, "", 0),
    ];
    records.extend(root_replay());
    records.push(said("developer", "You are an agent in a team", None));
    records.push(started("01a20000-0001-7001-8000-000000000005"));
    records.push(envelope(
        "/root",
        "/root/sub_alpha",
        "Create sub_alpha.txt containing exactly alpha.",
    ));
    records.extend(call("child-call", "*** Add File: main_beta.txt"));
    records.push(envelope(
        "/root",
        "/root/sub_alpha",
        "Please complete the original task now.",
    ));
    records.push(said("assistant", "Both files are in place.", None));
    let (rows, calls) = parse_rollout(CHILD, &records);
    assert_eq!(
        rows,
        [
            row(
                CHILD,
                ROOT,
                "[subagent task] Create sub_alpha.txt containing exactly alpha.",
                ""
            ),
            row(
                CHILD,
                ROOT,
                "[subagent message] Please complete the original task now.",
                "Both files are in place."
            ),
        ]
    );
    assert_eq!(calls, [[CHILD.to_string(), "child-call".to_string()]]);
}

/// A depth-2 child replays its parent's and grandparent's metadata: its parent is the
/// first replayed one, and its own rows are kept.
#[test]
fn a_depth_two_team_child_keeps_its_rows_under_its_direct_parent() {
    let mut records = vec![
        team_meta(GRANDCHILD, Some(CHILD), "/root/sub_alpha/sub_alpha", 2),
        team_meta(CHILD, Some(ROOT), "/root/sub_alpha", 1),
        team_meta(ROOT, None, "", 0),
    ];
    records.extend(root_replay());
    records.push(started("01a20000-0001-7001-8000-000000000005"));
    records.push(said("developer", "You are an agent in a team", None));
    records.push(started("01a20000-0002-7001-8000-000000000006"));
    records.push(envelope(
        "/root/sub_alpha",
        "/root/sub_alpha/sub_alpha",
        "Create the file in the active project root.",
    ));
    records.push(said(
        "assistant",
        "The platform blocked that tool invocation.",
        None,
    ));
    records.extend(call("grandchild-call", "*** Update File: main_beta.txt"));
    let (rows, calls) = parse_rollout(GRANDCHILD, &records);
    assert_eq!(
        rows,
        [row(
            GRANDCHILD,
            CHILD,
            "[subagent task] Create the file in the active project root.",
            "The platform blocked that tool invocation."
        )]
    );
    assert_eq!(
        calls,
        [[GRANDCHILD.to_string(), "grandchild-call".to_string()]]
    );
}

/// An envelope whose body is encrypted anchors the child's turn with its routing.
#[test]
fn an_encrypted_team_envelope_anchors_its_routing() {
    let records = [
        team_meta(CHILD, Some(ROOT), "/root/sub_alpha", 1),
        envelope(
            "/root",
            "/root/sub_alpha",
            "gAAAAABq7mhLgQvR1xRHogzpkRtNJri",
        ),
        said("assistant", "Done.", None),
    ];
    let (rows, _) = parse_rollout(CHILD, &records);
    assert_eq!(
        rows,
        [row(
            CHILD,
            ROOT,
            "[subagent task] /root/sub_alpha (from /root)",
            "Done."
        )]
    );
}

const REVIEWED: &str = "01a20000-0010-7000-8000-000000000010";
const REVIEW_THREAD: &str = "01a20000-0011-7000-8000-000000000011";

/// `records` as newer Codex builds write them, an ordinal after each timestamp, which the byte
/// prefilter reads apart from the compact layout.
fn with_ordinals(records: &[serde_json::Value]) -> Vec<serde_json::Value> {
    records
        .iter()
        .enumerate()
        .map(|(i, r)| serde_json::json!({"timestamp":r["timestamp"],"ordinal":i,"type":r["type"],"payload":r["payload"]}))
        .collect()
}

/// What a chat that ran a review holds, as Codex writes it: review mode opens, the review
/// thread receives its request, and once it has a result, Codex composes a notice of it,
/// review mode closes and the result arrives as the chat's reply.
fn review_records(
    hint: Option<&str>,
    request: Option<&str>,
    result: Option<&str>,
) -> Vec<serde_json::Value> {
    let turn = "01a20000-0010-7001-8000-000000000012";
    let thread_turn = "01a20000-0011-7001-8000-000000000013";
    let mut entered = serde_json::json!({"type":"EnteredReviewMode","target":{"type":"baseBranch","branch":"main"}});
    if let Some(hint) = hint {
        entered["user_facing_hint"] = hint.into();
    }
    let mut records = vec![
        record(
            "session_meta",
            serde_json::json!({"id":REVIEWED,"cwd":"/work/review","thread_source":"user","source":"exec"}),
        ),
        record(
            "event_msg",
            serde_json::json!({"type":"item_completed","thread_id":REVIEWED,"turn_id":turn,"item":entered}),
        ),
        started(thread_turn),
    ];
    records.extend(request.map(|request| submitted(REVIEW_THREAD, thread_turn, request)));
    if let Some(result) = result {
        records.push(said(
            "user",
            &format!("<user_action>\n  <action>review</action>\n  <results>\n  {result}\n  </results>\n</user_action>"),
            Some(turn),
        ));
        records.push(record(
            "event_msg",
            serde_json::json!({"type":"item_completed","thread_id":REVIEWED,"turn_id":turn,"item":{"type":"ExitedReviewMode","review_output":{"overall_explanation":result}}}),
        ));
        records.push(said("assistant", result, Some(turn)));
    }
    records
}

/// A review's result is a row of the chat that ran it, labeled `[review]` with the first line
/// of the request its review thread received, else of the hint Codex showed, so a chat that
/// holds only the review has a title and a turn. The review thread keeps its own task and work.
#[test]
fn a_review_keeps_its_result_in_the_chat_and_its_work_in_its_thread() {
    let request =
        "Review the code changes against the base branch 'main'.\nProvide prioritized findings.";
    let result = "No actionable regressions found.";
    let hint = Some("changes against 'main'");
    for (hint, request, label) in [
        (
            hint,
            Some(request),
            "[review] Review the code changes against the base branch 'main'.",
        ),
        (hint, None, "[review] changes against 'main'"),
        (None, None, "[review]"),
    ] {
        let parent = review_records(hint, request, Some(result));
        for records in [parent.clone(), with_ordinals(&parent)] {
            let (rows, _) = parse_rollout(REVIEWED, &records);
            assert_eq!(rows, [row(REVIEWED, "", label, result)], "{label}");
        }
    }

    // A review interrupted before its result leaves no row, and a prompt typed after it is a
    // turn of its own.
    let mut interrupted = review_records(hint, Some(request), None);
    assert_eq!(
        parse_rollout(REVIEWED, &interrupted).0,
        Vec::<[String; 4]>::new()
    );
    interrupted.extend([
        said("user", "now fix the flaky test", None),
        record(
            "event_msg",
            serde_json::json!({"type":"user_message","message":"now fix the flaky test"}),
        ),
        said("assistant", "Fixed.", None),
    ]);
    assert_eq!(
        parse_rollout(REVIEWED, &interrupted).0,
        [row(REVIEWED, "", "now fix the flaky test", "Fixed.")]
    );

    let thread_turn = "01a20000-0011-7001-8000-000000000013";
    let request = "Review the code changes against the base branch.";
    let mut thread = vec![
        record(
            "session_meta",
            serde_json::json!({"id":REVIEW_THREAD,"cwd":"/work/review","thread_source":"subagent","parent_thread_id":REVIEWED,"source":{"subagent":"review"}}),
        ),
        started(thread_turn),
        said(
            "user",
            "# AGENTS.md instructions\n<INSTRUCTIONS>x</INSTRUCTIONS>",
            Some(thread_turn),
        ),
        said("user", request, Some(thread_turn)),
        submitted(REVIEW_THREAD, thread_turn, request),
        said("assistant", "Inspecting the diff.", Some(thread_turn)),
    ];
    thread.extend(call("review-call", "git diff"));
    let (rows, calls) = parse_rollout(REVIEW_THREAD, &thread);
    assert_eq!(
        rows,
        [row(
            REVIEW_THREAD,
            REVIEWED,
            &format!("[subagent task] {request}"),
            "Inspecting the diff."
        )]
    );
    assert_eq!(
        calls,
        [[REVIEW_THREAD.to_string(), "review-call".to_string()]]
    );
}

const VOICE: &str = "01a20000-0020-7000-8000-000000000020";

/// A voice chat with one request and the flush ending it, each carrying these transcript
/// lines.
fn voice_records(asked_said: &str, flush_said: &str) -> Vec<serde_json::Value> {
    let (first, last) = (
        "01a20000-0020-7001-8000-000000000021",
        "01a20000-0020-7002-8000-000000000022",
    );
    let request = format!("<realtime_delegation>\n  <input>is a donut too much sugar</input>\n  <transcript_delta>{asked_said}</transcript_delta>\n</realtime_delegation>");
    let flush = format!("<realtime_delegation>\n  <source>transcript_tail_flush</source>\n  <input>The user just ended their realtime session.</input>\n  <transcript_delta>{flush_said}</transcript_delta>\n</realtime_delegation>");
    vec![
        record(
            "session_meta",
            serde_json::json!({"id":VOICE,"cwd":"/work/voice","thread_source":"voice_chat","source":"vscode"}),
        ),
        said("user", &request, Some(first)),
        submitted(VOICE, first, &request),
        said("assistant", "[COMPLETE] A donut is fine.", Some(first)),
        said("user", &flush, Some(last)),
        submitted(VOICE, last, &flush),
        said("assistant", "Voice session ended.", Some(last)),
    ]
}

/// A voice session hands Codex the user's spoken request as a turn. When its flush ends it,
/// what its transcript holds that no request took is a turn of its own, the user's words as
/// its text and the voice assistant's as its reply, which Codex's acknowledgement continues;
/// the flush's own note is Codex's.
#[test]
fn a_voice_request_is_the_users_turn_and_its_transcript_the_rest() {
    let asked_said = "user:  so um, is a donut too much sugar\nassistant:  Let me check.";
    let flush_said = "assistant:  Glad to help.\nuser:  thanks";
    let records = voice_records(asked_said, flush_said);
    assert_eq!(
        parse_rollout(VOICE, &records).0,
        [
            row(
                VOICE,
                "",
                "is a donut too much sugar",
                "[COMPLETE] A donut is fine."
            ),
            row(
                VOICE,
                "",
                "so um\nthanks",
                "Let me check. Glad to help. Voice session ended."
            )
        ]
    );

    // A transcript the request already holds adds nothing, and the acknowledgement
    // continues the request's turn.
    let said = "user:  is a donut too much sugar";
    assert_eq!(
        parse_rollout(VOICE, &voice_records(said, said)).0,
        [row(
            VOICE,
            "",
            "is a donut too much sugar",
            "[COMPLETE] A donut is fine. Voice session ended."
        )]
    );

    // The session's transcript survives a parse that stops before the flush.
    let body = |records: &[serde_json::Value]| -> String {
        records.iter().map(|r| r.to_string() + "\n").collect()
    };
    let path = PathBuf::from(format!("rollout-2026-06-10T14-33-07-{VOICE}.jsonl"));
    let (whole, _) = crate::intake::scoped(|| {
        super::resume_parse(&path, body(&records).as_str(), None)
            .unwrap()
            .0
    });
    let (head, tail) = records.split_at(4);
    let ((first, second), _) = crate::intake::scoped(|| {
        let (first, _, _, cursor) = super::resume_parse(&path, body(head).as_str(), None).unwrap();
        let second = super::resume_parse(&path, body(tail).as_str(), Some(&cursor))
            .unwrap()
            .0;
        (first, second)
    });
    let turns = |messages: &[crate::model::Message]| {
        messages
            .iter()
            .map(|m| (m.turn, m.text.to_string(), m.reply.to_string()))
            .collect::<std::collections::BTreeSet<_>>()
    };
    let mut resumed = turns(&first);
    resumed.retain(|(turn, ..)| second.iter().all(|m| m.turn != *turn));
    resumed.extend(turns(&second));
    assert_eq!(resumed, turns(&whole));
}

/// A Desktop prompt with a pasted image carries the image's markers after the submitted text.
#[test]
fn a_desktop_prompt_with_an_image_is_the_users_turn() {
    let chat = "01a20000-0030-7000-8000-000000000030";
    let turn = "01a20000-0030-7001-8000-000000000031";
    let prompt = record(
        "response_item",
        serde_json::json!({"type":"message","role":"user","content":[{"type":"input_text","text":"fix the login bug"},{"type":"input_text","text":"<image name=[Image #1]>"},{"type":"input_image","image_url":"data:image/png;base64,AAAA"},{"type":"input_text","text":"</image>"}],"internal_chat_message_metadata_passthrough":{"turn_id":turn}}),
    );
    let records = [
        record(
            "session_meta",
            serde_json::json!({"id":chat,"cwd":"/work/desktop","source":"vscode"}),
        ),
        prompt,
        submitted(chat, turn, "fix the login bug"),
        said("assistant", "Fixed.", Some(turn)),
    ];
    let (rows, _) = parse_rollout(chat, &records);
    assert_eq!(
        rows,
        [row(
            chat,
            "",
            "fix the login bug\n<image name=[Image #1]>\n</image>",
            "Fixed."
        )]
    );
}

/// A guardian's review of another chat's actions, in both of its schemas: each verdict is a
/// labeled row of its own under the reviewed chat, the request quoting that chat is not, and
/// a call the guardian made is its own.
#[test]
fn guardian_verdicts_are_labeled_rows_of_the_reviewed_chat() {
    let guardian = "01a20000-0040-7000-8000-000000000040";
    let verdict = |outcome: &str| {
        format!("{{\"risk_level\":\"low\",\"outcome\":\"{outcome}\",\"rationale\":\"read-only\"}}")
    };
    for thread_source in ["guardian_review", "subagent"] {
        let mut records = vec![
            record(
                "session_meta",
                serde_json::json!({"id":guardian,"cwd":"/work/guard","thread_source":thread_source,"parent_thread_id":REVIEWED,"source":{"subagent":{"other":"guardian"}}}),
            ),
            said("user", "The following is the Codex agent history", None),
        ];
        records.extend(call("guardian-call", "inspect approval target"));
        records.push(said("assistant", &verdict("allow"), None));
        records.push(said(
            "user",
            "The following is the Codex agent history",
            None,
        ));
        records.push(said("assistant", &verdict("deny"), None));
        let (rows, calls) = parse_rollout(guardian, &records);
        assert_eq!(
            rows,
            [
                row(
                    guardian,
                    REVIEWED,
                    &format!("[guardian verdict] {}", verdict("allow")),
                    ""
                ),
                row(
                    guardian,
                    REVIEWED,
                    &format!("[guardian verdict] {}", verdict("deny")),
                    ""
                ),
            ],
            "{thread_source}"
        );
        assert_eq!(
            calls,
            [[guardian.to_string(), "guardian-call".to_string()]],
            "{thread_source}"
        );
    }
}
