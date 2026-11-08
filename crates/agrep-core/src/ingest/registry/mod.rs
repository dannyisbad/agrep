//! The one registration point for adapters. Adding an agent is a trait impl in its module
//! plus a single line in `ADAPTERS` here.
//!
//! The driver runs cache-driven adapters sequentially sharing the per-file parse cache; the
//! full-parse (Always) adapters run in a sibling rayon task; results are concatenated and
//! deduped by message content/position or event call id. Sessions with colliding source turns
//! receive deterministic unique turn numbers before publication.

use std::path::{Path, PathBuf};

use crate::intake_batch::IntakeBatch;
use crate::model::{Event, Message};

mod change_token;
mod paths;
mod plain_file;
mod secure_read;
mod tokens;
#[cfg(windows)]
mod windows_handle;
#[cfg(windows)]
mod windows_path;

#[cfg(not(windows))]
pub use change_token::metadata_change_token;
pub use change_token::ChangeToken;
pub use paths::{classify_store_link, StoreLink};
#[cfg(not(windows))]
pub use paths::{path_eq, relative_path};
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub use plain_file::nofollow_read_options;
pub(crate) use plain_file::secure_read_options;
pub use plain_file::{metadata_is_link, plain_entry_metadata, plain_metadata, stat_proves_absent};
#[cfg(not(windows))]
pub(crate) use secure_read::open_regular_snapshot;
pub use secure_read::{
    read_bounded_regular_file, read_growing_regular_file, regular_file_edge_snapshot,
    BoundedRegularFileSnapshot, RegularFileEdgeSnapshot,
};
pub use tokens::{
    fnv_token, token_fingerprint, TokenAvailability, TokenReadIssue, ATTRIBUTION_ISSUE_KIND,
};
#[cfg(windows)]
pub(crate) use windows_handle::opened_file_identity;
#[cfg(windows)]
pub use windows_handle::{metadata_change_token, WindowsFileIdentity};
#[cfg(windows)]
pub use windows_path::{logical_path_key, path_eq, relative_path};

/// How an adapter decides a source is unchanged since the last index. Defined here so each
/// adapter states its strategy; the driver only uses it to route to the matching parse path
/// (Stat/Token -> the per-file cache; Always -> full reparse).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Fingerprint {
    /// Reparse when a file's (mtime, filesystem-change-id, size) moves
    /// (claude/codex/opencode/gemini).
    Stat,
    /// Reparse when a per-conversation token changes (updatedAt / value hash) - one sqlite
    /// file, many conversations, so staleness is per-session (crush, cursor).
    Token,
    /// Always full-parse; no per-file cache (the small stores: antigravity/kimi/cline).
    Always,
}

/// One agent's transcript adapter: discover its store under `home()`, parse sessions, and
/// emit the user's messages + tool/subagent events. `collect` takes the shared per-file parse
/// cache; full-parse adapters ignore it. Impls live in each adapter module (byte-identical
/// wrappers over the existing `collect` free fns).
pub trait Adapter: Sync {
    fn name(&self) -> &'static str;
    fn fingerprint(&self) -> Fingerprint;
    fn collect(&self, cache: &mut IntakeBatch) -> (Vec<Message>, Vec<Event>);
    fn collect_checked(
        &self,
        cache: &mut IntakeBatch,
    ) -> (
        Vec<Message>,
        Vec<Event>,
        crate::intake_batch::ReadOutcome,
        Vec<crate::intake_batch::SourceReadIssue>,
    ) {
        let (messages, events) = self.collect(cache);
        (
            messages,
            events,
            crate::intake_batch::ReadOutcome::Complete,
            Vec::new(),
        )
    }
    /// Whether a read of one of this adapter's sources can come back `ReadOutcome::Partial`.
    /// Cache generations older than the current one published such reads without caching them.
    fn may_read_partially(&self) -> bool {
        false
    }
    /// Whether a failed read of `path` costs a whole-store read exactly the rows of the units
    /// beneath it. A path every unit draws on (an index or registry) may change what the rest
    /// yield, so the default claims nothing.
    fn unit_scoped_read_issue(&self, _path: &std::path::Path) -> bool {
        false
    }
    /// Where this adapter's store lives (dirs or files; absent candidates are fine).
    /// The doctor freshness canary stats these: store activity the parser never turns
    /// into messages is adapter drift, and this is what makes it visible.
    fn store_roots(&self) -> Vec<std::path::PathBuf>;
    /// Native recovery inputs. This belongs to the adapter registry so archive
    /// and intake never maintain competing source-location tables.
    fn archive_roots(&self) -> Vec<PathBuf> {
        self.freshness_roots()
    }
    fn archive_content(&self, path: &Path) -> bool {
        self.freshness_content(path)
    }
    /// Is this file conversation CONTENT the adapter would parse? The canary counts
    /// only these - state/config files update on app boots without any new sessions,
    /// and a canary that fires on those trains everyone to ignore it.
    fn store_content(&self, _path: &std::path::Path) -> bool {
        true
    }
    /// The id the store's own filename gives `session`'s transcript at `path`, when it differs
    /// from the header id the rows carry; publication resolves both to one family.
    fn session_alias(&self, _path: &Path, _session: &str) -> Option<String> {
        None
    }
    /// Inputs whose unchanged state permits skipping this adapter altogether. Usually these
    /// are the same as the doctor/audit roots, but an adapter may add attribution metadata
    /// which affects emitted rows without itself being conversation content.
    fn freshness_roots(&self) -> Vec<PathBuf> {
        self.store_roots()
    }
    fn freshness_content(&self, path: &Path) -> bool {
        self.store_content(path)
    }
    /// Exact per-conversation tokens for `Fingerprint::Token` stores.
    fn freshness_tokens(&self) -> TokenAvailability {
        TokenAvailability::Unreadable(vec![TokenReadIssue::new(
            self.runtime_issue_root(),
            "conversation token snapshot unavailable",
        )])
    }
    /// Exact live tokens keyed the same way as `intake_stats.json` entries.
    fn intake_tokens(&self) -> TokenAvailability {
        self.freshness_tokens()
    }
    /// [`Self::intake_tokens`] beside the parse cache the diagnostic decoded (None when it could
    /// not): an adapter whose tokens depend on what the last generation published (cursor's
    /// retained attribution) reads it rather than the live sources alone.
    fn intake_tokens_with(
        &self,
        _cache: Option<&crate::intake_batch::IntakeBatch>,
    ) -> TokenAvailability {
        self.intake_tokens()
    }
    /// For `Fingerprint::Token` stores: the prefix every freshness token id and parse-cache id of
    /// `database`'s conversations starts with. None keeps a listed database counted as material.
    fn token_prefix(&self, _database: &Path) -> Option<String> {
        None
    }
    /// The inverse of [`Self::token_prefix`]: the database part of a parse-cache id, equal to
    /// `token_prefix` of the database that conversation came from. "" spans every database.
    fn token_namespace<'a>(&self, _cache_id: &'a str) -> &'a str {
        ""
    }
    /// Actionable adapter root used only when a guarded collector cannot name a finer path.
    fn runtime_issue_root(&self) -> PathBuf {
        self.freshness_roots()
            .into_iter()
            .next()
            .unwrap_or_else(crate::ingest::home)
    }
}

/// Every registered adapter. This slice is the single source of truth the driver, the CLI
/// help, and the event delete-sweep all read - add an adapter here and it is wired everywhere.
pub static ADAPTERS: &[&dyn Adapter] = &[
    &crate::ingest::claude::Claude,
    &crate::ingest::codex::Codex,
    &crate::ingest::opencode::Opencode,
    &crate::ingest::antigravity::Antigravity,
    &crate::ingest::kimi::Kimi,
    &crate::ingest::cline::Cline,
    &crate::ingest::gemini::Gemini,
    &crate::ingest::crush::Crush,
    &crate::ingest::cursor::Cursor,
    &crate::ingest::pi::Pi,
];

/// [`Adapter::session_alias`] for the adapter that owns `agent`'s rows.
pub fn session_alias(agent: &str, path: &Path, session: &str) -> Option<String> {
    ADAPTERS
        .iter()
        .find(|adapter| adapter.name() == agent)
        .and_then(|adapter| adapter.session_alias(path, session))
}

/// [`Adapter::token_prefix`] of `agent`'s store, when it is a `Fingerprint::Token` adapter.
pub fn token_prefix(agent: &str, database: &Path) -> Option<String> {
    ADAPTERS
        .iter()
        .find(|adapter| adapter.name() == agent && adapter.fingerprint() == Fingerprint::Token)
        .and_then(|adapter| adapter.token_prefix(database))
}

/// [`Adapter::token_namespace`] of a cache id of `agent`; "" (every database) when no token
/// adapter claims the agent.
pub fn token_namespace(agent: &str, cache_id: &str) -> String {
    ADAPTERS
        .iter()
        .find(|adapter| adapter.name() == agent && adapter.fingerprint() == Fingerprint::Token)
        .map(|adapter| adapter.token_namespace(cache_id).to_owned())
        .unwrap_or_default()
}
