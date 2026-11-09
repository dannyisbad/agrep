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
