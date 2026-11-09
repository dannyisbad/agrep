//! Reading one session file: a JSONL record stream folded as upstream's recorder replays it, or a
//! legacy whole-session JSON document.

use std::path::Path;

use serde_json::Value;

use super::discover::{place_of, Place, Projects};
use super::emit::emit_messages;
use super::fold::Fold;
use crate::intake::Tally;
use crate::intake_batch::ReadOutcome;
use crate::model::{Event, Message};

pub(super) fn parse_file(
    path: &Path,
    projects: &Projects,
) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    parse_with_tally(path, projects, &crate::intake::tally())
}

fn parse_with_tally(
    path: &Path,
    projects: &Projects,
    tally: &Tally,
) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            crate::intake::diagnostic(format_args!(
                "  ! gemini: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            ));
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            crate::ingest::note_io_failure(path, &e);
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest::read_failure_outcome(&e),
            );
        }
    };
    let place = place_of(path, projects);
    if path.extension().and_then(|x| x.to_str()) == Some("jsonl") {
        return parse_jsonl(&data, path, &place, tally);
    }
    parse_json(&data, path, &place, tally)
}

/// A legacy `.json` file, parsed whole.
fn parse_json(
    data: &str,
    path: &Path,
    place: &Place,
    tally: &Tally,
) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    match serde_json::from_str::<Value>(data) {
        Ok(root) => {
            let (messages, events) = parse_document(&root, path, place, tally);
            (messages, events, ReadOutcome::Complete)
        }
        Err(e) => {
            // whole-file failure: the file itself is the one seen-and-errored record. Text that
            // does not parse, an empty file included, is its content: a rewrite caught mid-way
            // changes the file's identity, so only a file that stays so persists.
            tally.seen();
            tally.error(&format!("{e}: {}", crate::intake::clip(data, 80)));
            if data.trim().is_empty() {
                crate::ingest::note_read_failure(path, "file is empty".to_owned());
            }
            (Vec::new(), Vec::new(), ReadOutcome::Invalid)
        }
    }
}

/// `Undone` when the read is empty because the file itself undid what it held: rolled back or
/// rewound in records the read saw whole. Upstream only appends complete lines, so an error-free
/// read ending on one is the session as it stood; a torn or foreign read never qualifies.
fn parse_jsonl(
    data: &str,
    path: &Path,
    place: &Place,
    tally: &Tally,
) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    let fold = Fold::read(data);
    if !fold.complete() {
        if let Ok(root) = serde_json::from_str::<Value>(data) {
            if root.get("sessionId").is_some() {
                let (messages, events) = parse_document(&root, path, place, tally);
                return (messages, events, ReadOutcome::Complete);
            }
        }
    }
    fold.record(tally);
    let session = fold.session().unwrap_or_else(|| file_stem(path));
    let (messages, events) = emit_messages(&session, place, fold.messages(), tally);
    let undone = messages.is_empty()
        && events.is_empty()
        && fold.complete()
        && fold.error_free()
        && data.ends_with('\n')
        && fold.undid_any();
    let outcome = if undone {
        ReadOutcome::Undone
    } else {
        ReadOutcome::Complete
    };
    (messages, events, outcome)
}

/// A legacy whole-session document; seen = elements of its messages array.
fn parse_document(
    root: &Value,
    path: &Path,
    place: &Place,
    tally: &Tally,
) -> (Vec<Message>, Vec<Event>) {
    let session = root
        .get("sessionId")
        .and_then(|s| s.as_str())
        .map(str::to_string)
        .unwrap_or_else(|| file_stem(path));
    let Some(messages) = root.get("messages").and_then(|m| m.as_array()) else {
        return (Vec::new(), Vec::new());
    };
    tally.seen_n(messages.len() as u64);
    emit_messages(
        &session,
        place,
        messages.iter().map(|m| (m, false, false)),
        tally,
    )
}

fn file_stem(path: &Path) -> String {
    path.file_stem()
        .map(|stem| stem.to_string_lossy().to_string())
        .unwrap_or_default()
}
