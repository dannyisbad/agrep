//! Claude Code adapter: ~/.claude/projects/<cwd-slug>/*.jsonl
//! Session files plus `subagents/` side transcripts; structurally identified
//! throwaway-worker records are accounted as named skips.
//! Keeps real human turns: type=user, not meta/sidechain, userType external|absent,
//! string or text-block content, not a command/system wrapper.

mod discovery;
pub mod launch;
mod notifications;
mod parse;
mod project;
mod records;
mod resume;
mod throwaway;
mod tool_calls;

use crate::model::{Event, Message};

pub(crate) use resume::resume_parse;

/// Control-event names for structural moments; they are never tool calls.
pub const API_ERROR_MARKER: &str = "api_error";
pub const TASK_NOTIFICATION_MARKER: &str = "task_notification";

/// Walk all real project dirs and collect the user's Claude messages + tool events.
pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    let root = discovery::projects_root();
    let files = cache
        .preflight_source_paths(&root)
        .unwrap_or_else(|| discovery::transcript_files(&root));
    let pass = crate::intake_batch::collect_cached_for(cache, "claude", &files, parse::parse_file);
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). Byte-identical wrapper over `collect`.
pub struct Claude;
impl crate::ingest::registry::Adapter for Claude {
    fn name(&self) -> &'static str {
        "claude"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![discovery::projects_root()]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        path.extension().and_then(|x| x.to_str()) == Some("jsonl")
    }
    fn freshness_content(&self, path: &std::path::Path) -> bool {
        discovery::is_discovered_transcript(path)
    }
}
