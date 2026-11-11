//! Codex CLI/Desktop adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, which Codex
//! later compresses in place to rollout-*.jsonl.zst and moves to ~/.codex/archived_sessions/
//! when the user archives the chat.
//! One JSONL file per session. The first line is `session_meta` (gives cwd -> project,
//! and the session id). Real human turns are `response_item` lines with
//! payload.type=="message" and payload.role=="user", text in `input_text`/`text` blocks.
//!
//! Much of what arrives under `role:user` is codex's own composition: AGENTS.md and the
//! environment block, plugin and skill catalogs, delegation payloads, system notifications.
//! Their shape is indistinguishable from typed prose, so eligibility comes from the rollout's
//! `event_msg` submission log (`Submissions`), in legacy and Desktop schemas.
//! `is_wrapper`/`is_codex_injected` still veto client echoes the log does carry,
//! such as slash-command markup. Prefer under-including over mislabeling.
//!
//! While Codex compresses or archives a rollout two copies may exist; each is a source of
//! the same session, and the store's canonical replay shows one copy of each turn.
//!
//! NOTE: `~/.codex/.tmp/**` (plugin test fixtures) and `~/.codex/history.jsonl` are
//! intentionally not read.

mod discover;
mod holdings;
mod parser;
mod prefilter;
mod records;
mod review;
mod state;
mod subagent;
mod submissions;
#[cfg(test)]
mod tests;
mod tools;
mod voice;

use std::path::Path;

use crate::model::{Event, Message};

pub use discover::{collect, file_id, Codex};
pub use holdings::{holdings, Holdings};
pub(crate) use subagent::{delegated_input, envelope_anchor, is_uuid_v7_after};
pub use subagent::{fork_origin, fork_time};

/// Control-event name for a native compaction moment; it is never a tool call.
pub const COMPACTED_MARKER: &str = "compacted";

/// The label opening a guardian's verdict row. The meaning lane leaves rows with it out
/// (`semantic_store`), as it does an omp advisor's notes.
pub const GUARDIAN_VERDICT: &str = "[guardian verdict] ";

/// Codex-specific preambles and system-injected `role:user` notifications that are NOT
/// something the user typed. Checked in addition to the shared `is_wrapper`; the full audit
/// excludes the same text from what it expects of a rollout.
pub fn is_codex_injected(text: &str) -> bool {
    let t = text.trim_start();
    // Injected first-turn preamble (AGENTS.md / environment / instructions blocks).
    t.starts_with("# AGENTS.md")
        || t.starts_with("<environment_context")
        || t.starts_with("<INSTRUCTIONS")
        || t.starts_with("<permissions")
        || t.starts_with("<user_instructions")
        || t.contains("<user_instructions>")
        // System-authored notifications that arrive as role:user.
        || t.starts_with("<turn_aborted")
        || t.starts_with("<subagent_notification")
        || t.starts_with("<goal_context")
        || t.starts_with("<codex_internal_context")
        || t.starts_with("<codex_delegation")
        || t.starts_with("<realtime_delegation")
        || t.starts_with("<user_action")
        // Image-paste marker; the typed text is entangled with it, so skip rather than mislabel.
        || t.starts_with("<image")
}

/// Parse one whole rollout file.
fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    if discover::is_compressed(path) {
        return parse_compressed(path);
    }
    let tally = crate::intake::tally();
    match crate::ingest::read_lossy(path) {
        Ok(data) => parser::parse_data(path, tally, &data, None).0,
        Err(e) => unreadable(path, &tally, &e, crate::ingest::read_failure_outcome(&e)),
    }
}

/// Parse a compressed rollout whole: Codex rewrites rather than appends it, so a change under
/// the read fails it. Its counts join the audit only once it decoded to the end.
fn parse_compressed(path: &Path) -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    let tally = std::sync::Arc::default();
    let read = crate::ingest::registry::read_regular_snapshot(path, |file| {
        let text = crate::ingest::jsonl::ZstdText::new(file);
        let parsed = parser::parse_data(path, std::sync::Arc::clone(&tally), &text, None).0;
        text.check().map(|()| parsed)
    })
    .and_then(|read| {
        read.ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::NotFound, "source does not exist")
        })
    });
    match read {
        Ok(Ok(parsed)) => {
            crate::intake::adopt(tally);
            parsed
        }
        // A decoding failure is the file's content, not a race.
        Ok(Err(e)) => {
            let parsed = unreadable(
                path,
                &crate::intake::tally(),
                &e,
                crate::intake_batch::ReadOutcome::Skipped,
            );
            crate::ingest::note_read_failure(path, format!("cannot decompress: {e}"));
            parsed
        }
        Err(e) => unreadable(
            path,
            &crate::intake::tally(),
            &e,
            crate::ingest::read_failure_outcome(&e),
        ),
    }
}

/// An unreadable file counts as one seen, errored record.
fn unreadable(
    path: &Path,
    tally: &crate::intake::Tally,
    error: &std::io::Error,
    outcome: crate::intake_batch::ReadOutcome,
) -> (Vec<Message>, Vec<Event>, crate::intake_batch::ReadOutcome) {
    crate::intake::diagnostic(format_args!(
        "  ! codex: cannot read {}: {}",
        crate::ingest::terminal_safe(path.display()),
        crate::ingest::terminal_safe(error)
    ));
    tally.seen();
    tally.error(&format!("cannot read: {error}"));
    crate::ingest::note_io_failure(path, error);
    (Vec::new(), Vec::new(), outcome)
}

/// Parse `data` resuming from the serialized state of the previous parse; returns the
/// new rows/events and the state to resume the next append from.
pub(crate) fn resume_parse(
    path: &Path,
    data: &(impl crate::ingest::jsonl::Text + ?Sized),
    previous: Option<&str>,
) -> anyhow::Result<(
    Vec<Message>,
    Vec<Event>,
    crate::intake_batch::ReadOutcome,
    String,
)> {
    let state = previous.map(serde_json::from_str).transpose()?;
    let ((m, e, o), next) = parser::parse_data(path, crate::intake::tally(), data, state);
    Ok((m, e, o, serde_json::to_string(&next)?))
}
