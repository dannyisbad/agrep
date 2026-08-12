//! Native storage and retrieval primitives for agrep.
//!
//! The crate parses supported agent stores into normalized [`Message`] and event rows, maintains
//! incremental ingest state, publishes corpus artifacts, and implements boundary, substring, and
//! quantized-vector scoring. Python owns command orchestration and the user-facing search policy.

pub mod boundary_rank;
pub mod boundary_stats;
pub mod cache;
pub mod emit;
pub mod fallback_scan;
pub mod ingest;
pub mod ingest_cache;
pub mod intake;
pub mod model;
pub mod row_class;
pub mod semantic_q8;

#[cfg(any(unix, windows))]
mod mapped_file;
mod unicode_v16;

pub use model::Message;
