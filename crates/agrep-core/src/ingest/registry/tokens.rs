//! Per-conversation staleness tokens of `Fingerprint::Token` stores, and what a census of them
//! could and could not read.

use std::path::PathBuf;

/// Issue kind of an attribution source (cursor's workspace metadata) the census could not read
/// while it still listed every conversation: nothing is hidden or lost, some projects are
/// provisional. The publication guard admits it without a material verdict for its path.
pub const ATTRIBUTION_ISSUE_KIND: &str = "attribution-unreadable";

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TokenAvailability {
    Data(Vec<(String, String)>),
    Empty,
    /// Every database the census opened lists its conversations; `issues` name each it could not.
    Partial {
        tokens: Vec<(String, String)>,
        issues: Vec<TokenReadIssue>,
    },
    Unreadable(Vec<TokenReadIssue>),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TokenReadIssue {
    pub path: PathBuf,
    pub kind: &'static str,
    pub reason: String,
    /// Conversations listed under a fallback project because `path`, an attribution source,
    /// could not be read. Empty when the issue hides conversations instead.
    pub provisional: Vec<String>,
    /// Conversations of a listed store this issue leaves unlisted; empty when `path` itself is
    /// the store (or a registry) that stayed unlisted whole.
    pub hidden: Vec<String>,
}

impl TokenReadIssue {
    /// A census failure a retry may clear; the publication guard keeps its fail-closed retry.
    pub fn new(path: impl Into<PathBuf>, reason: impl Into<String>) -> Self {
        Self::with_kind(path, "source-unreadable", reason)
    }

    /// `kind` is the snapshot issue kind, so an adapter can name a durable defect as one.
    pub fn with_kind(
        path: impl Into<PathBuf>,
        kind: &'static str,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            path: path.into(),
            kind,
            reason: reason.into(),
            provisional: Vec::new(),
            hidden: Vec::new(),
        }
    }

    pub fn provisional(mut self, sessions: Vec<String>) -> Self {
        self.provisional = sessions;
        self
    }

    pub fn hiding(mut self, sessions: Vec<String>) -> Self {
        self.hidden = sessions;
        self
    }
}

impl TokenAvailability {
    pub fn from_tokens(tokens: Vec<(String, String)>) -> Self {
        if tokens.is_empty() {
            Self::Empty
        } else {
            Self::Data(tokens)
        }
    }

    /// A census over several databases: the conversations of each it opened beside the issue
    /// naming each it could not. Nothing listed is plain `Unreadable`.
    pub fn from_observation(tokens: Vec<(String, String)>, issues: Vec<TokenReadIssue>) -> Self {
        if issues.is_empty() {
            Self::from_tokens(tokens)
        } else if tokens.is_empty() {
            Self::Unreadable(issues)
        } else {
            Self::Partial { tokens, issues }
        }
    }
}

/// A `Fingerprint::Token` conversation's staleness token: the store's own
/// `updatedAt` when it records one, else an FNV-1a hash over the conversation's serialized
/// JSON value. Count plus last-id is invariant under an in-place edit or a
/// delete-one-add-one, which strands the conversation as permanently "unchanged" (silent
/// staleness); `--full` is the documented recovery. crush keys its per-conversation cache
/// on this; cursor hashes raw row bytes via `fnv_token` directly.
pub fn token_fingerprint(value: &serde_json::Value) -> String {
    if let Some(u) = value.get("updatedAt") {
        match u {
            serde_json::Value::String(s) if !s.is_empty() => return format!("u:{s}"),
            serde_json::Value::Number(n) => return format!("u:{n}"),
            _ => {}
        }
    }
    fnv_token(value.to_string().as_bytes())
}

/// The FNV-1a half of `token_fingerprint`, for adapters whose store records no usable
/// updatedAt at all (cursor hashes raw row bytes directly instead of re-serializing JSON).
pub fn fnv_token(bytes: &[u8]) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("h:{h:016x}")
}
