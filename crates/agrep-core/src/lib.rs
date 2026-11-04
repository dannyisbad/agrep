//! agrep 0.5: local agent history in one transactional store.
pub mod archive;
pub mod boundary_rank;
pub mod boundary_stats;
mod compression;
pub mod data;
pub mod ingest;
pub mod intake;
pub mod intake_batch;
pub mod integrations;
pub mod model;
mod prose_index;
pub mod query;
pub mod render;
pub mod safe_fs;
pub mod semantic_q8;
pub mod store;
mod unicode_v16;
pub mod window;
pub use model::Message;

pub mod row_class;

pub mod freshness;

pub mod incremental;

mod row_stats;

mod tool_index;
mod tool_store;

mod fts_span;

pub mod briefing;
pub mod caller;
pub mod caller_process;
pub mod coverage;
pub mod diagnostics;
pub mod evidence;
pub mod explorer;
pub mod handles;
pub mod history;
mod history_meta;
pub mod hybrid;
pub mod json_output;
pub mod launch;
pub mod live;
pub mod logging;
pub mod pages;
pub mod process;
pub mod recovery;
pub mod semantic;
#[cfg(target_os = "macos")]
mod semantic_arena;
mod semantic_attention;
mod semantic_attention_metal;
mod semantic_bert;
pub mod semantic_device;
mod semantic_metal;
pub mod semantic_model;
mod semantic_norm;
mod semantic_policy;
#[doc(hidden)]
pub mod semantic_profile;
mod semantic_store;
pub mod surface;
pub mod terminal;

pub mod tool_text;
