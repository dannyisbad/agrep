//! Codex CLI/Desktop adapter: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
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
//! NOTE: only `~/.codex/sessions/` is walked. `~/.codex/.tmp/**` (plugin test fixtures),
//! `~/.codex/history.jsonl`, and `~/.codex/archived_sessions/` are intentionally not read.

mod discover;
mod parser;
mod prefilter;
mod records;
mod state;
mod subagent;
mod submissions;
mod tools;

use std::path::Path;

use crate::model::{Event, Message};

pub use discover::{collect, Codex};

/// Control-event name for a native compaction moment; it is never a tool call.
pub const COMPACTED_MARKER: &str = "compacted";

/// Codex-specific preambles and system-injected `role:user` notifications that are NOT
/// something the user typed. Checked in addition to the shared `is_wrapper`.
pub(crate) fn is_codex_injected(text: &str) -> bool {
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
    let tally = crate::intake::tally();
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            crate::intake::diagnostic(format_args!(
                "  ! codex: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            ));
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest::read_failure_outcome(&e),
            );
        }
    };

    parser::parse_data(path, tally, &data, None).0
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
