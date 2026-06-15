//! agrep-core - transcript ingest. Parses every agent's chat logs (claude, codex, opencode,
//! antigravity) into normalized [`Message`]s and writes the cache the Python sidecar builds
//! the search/affect/topic index from. The semantic + serving layers live in the sidecar; this
//! crate is the fast, parallel front door that turns ~10k scattered log files into one stream.

pub mod cache;
pub mod ingest;
pub mod ingest_cache;
pub mod model;

pub use model::Message;
