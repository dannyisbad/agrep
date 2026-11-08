//! Per-agent transcript adapters. Each yields normalized `Message`s (the user's words only).

pub mod antigravity;
pub mod claude;
pub mod cline;
pub mod codex;
pub mod crush;
pub mod cursor;
pub mod gemini;
pub(crate) mod jsonl;
pub mod kimi;
pub mod opencode;
pub(crate) mod parse_guard;
pub mod parse_timestamp;
pub mod pi;
pub mod registry;

mod project;
mod read_failure;
mod reply;
mod sqlite;
mod task_state;
mod text;

pub use project::{home, home_leaf, project_name};
pub(crate) use read_failure::{
    note_read_failure, read_failure_outcome, take_read_failure, SourceMoved,
};
pub use reply::{append_reply, ReplyOrder, AFTER_REPLY_META, REPLY_CAP, REPLY_TAIL_CAP};
pub use sqlite::JOURNAL_PRESENT;
pub(crate) use sqlite::{
    open_sqlite_ro, sqlite_code, sqlite_corrupt, sqlite_generation_token, sqlite_open_defect,
    sqlite_open_moved, ReadOnlyConnection,
};
pub use task_state::{is_bookkeeping_tool, is_task_tool, is_work_event, task_listing, task_state};
pub use text::{
    cap_event_output, cap_str, cap_str_with_chars, is_wrapper, read_lossy, summarize_tool_input,
    summarize_tool_input_with_chars, terminal_safe, warn_source_skip, EVENT_CAP,
};
