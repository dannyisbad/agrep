//! Per-source-file parse cache: skip re-parsing files whose `(mtime, change-id, size)` are unchanged
//! since the last index. Reparsing every source file on every run is expensive even when almost
//! nothing changed; this caches each file's parsed messages so a typical run re-parses only
//! the handful of files that actually moved.
//!
//! Only messages are cached. Events are not: their per-session files already persist
//! on disk and are skipped by content-hash in `cache::write_events`, so re-deriving them for
//! unchanged sessions adds cache I/O without avoiding publication work. Events are returned only
//! for sessions touched in the current run.
//!
//! Downstream message dedupe includes text and repairs turn collisions; events use call id,
//! where a repeated tuple is byte-identical - so the result is order-independent and the cache
//! is transparent. Claude and Codex are one-file-per-session; for any session that does
//! span files, its unchanged sibling files are re-parsed too so its event file stays complete.
//! Supported schema bumps retain last-good rows during reparse. Missing, corrupt, or
//! unsupported caches and `--full` fall back to a clean full parse.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use rayon::prelude::*;
use serde::{Deserialize, Serialize};

use crate::model::{Event, Message};

/// Increment when entry layout or parse semantics change.
/// Supported prior generations retain last-good entries until their source reparse completes.
pub const CACHE_VERSION: u32 = 38;

const CACHE_BASE_MAGIC: &[u8; 8] = b"AGRPCB01";
const CACHE_JOURNAL_MAGIC: &[u8; 8] = b"AGRPCJ01";
const CACHE_FRAME_MAGIC: &[u8; 8] = b"AGRPCF01";
const CACHE_COMMIT_MAGIC: &[u8; 8] = b"AGRPCOM1";
const CACHE_STORAGE_VERSION: u32 = 2;
const CACHE_BASE_STORAGE_VERSION: u32 = 4;
const CACHE_BASE_STORAGE_VERSION_V3: u32 = 3;
const CACHE_BASE_STORAGE_VERSION_V2: u32 = 2;
const CACHE_CODEC_NONE: u32 = 0;
const CACHE_CODEC_XPRESS: u32 = 1;
const MAX_COMPRESSED_CACHE_RAW_BYTES: u64 = 8 * 1024 * 1024 * 1024;
const MAX_COMPRESSED_CACHE_EXPANSION: u64 = 64;
const MAX_COMPRESSED_CACHE_SLACK: u64 = 16 * 1024 * 1024;
const CACHE_DIGEST_CHUNK: usize = 1024 * 1024;
const WRITER_BUILD_ID_LEN: usize = 20;
const BASE_HEADER_V2_LEN: usize = 4 + 8 + 8 + 4 + 16 + 8 + 16;
const BASE_HEADER_V3_LEN: usize = BASE_HEADER_V2_LEN + 4 + 4 + 8;
const BASE_OWNER_OFFSET: usize = 24;
const BASE_V4_INSTANCE_OFFSET: usize = BASE_OWNER_OFFSET + WRITER_BUILD_ID_LEN;
const BASE_V4_RAW_LEN_OFFSET: usize = BASE_V4_INSTANCE_OFFSET + 16;
const BASE_V4_DIGEST_OFFSET: usize = BASE_V4_RAW_LEN_OFFSET + 8;
const BASE_V4_CODEC_OFFSET: usize = BASE_V4_DIGEST_OFFSET + 16;
const BASE_V4_RESERVED_OFFSET: usize = BASE_V4_CODEC_OFFSET + 4;
const BASE_V4_STORED_LEN_OFFSET: usize = BASE_V4_RESERVED_OFFSET + 4;
const BASE_HEADER_LEN: usize = BASE_V4_STORED_LEN_OFFSET + 8;
const JOURNAL_HEADER_LEN: usize = 8 + 4 + 16;
const FRAME_HEADER_LEN: usize = 8 + 8 + 8 + 8 + 16;
const FRAME_FOOTER_LEN: usize = 8 + 8 + 8 + 16;
const MAX_FRAME_BYTES: u64 = 512 * 1024 * 1024;
const JOURNAL_COMPACT_AT: u64 = 16 * 1024 * 1024;
const JOURNAL_COMPACT_FRAMES: u64 = 1024;
const JOURNAL_COMPACT_BUDGET: usize = 4 * 1024 * 1024;
const STAGED_WRITE_CHUNK: usize = 256 * 1024;
const PYTHON_RUNTIME_BUILD_ID_ENV: &str = "AGREP_PYTHON_RUNTIME_BUILD_ID";
static CACHE_COMMIT_LOCK: Mutex<()> = Mutex::new(());

#[derive(Serialize, Deserialize, Clone, PartialEq)]
struct CMsg {
    agent: String,
    project: std::sync::Arc<str>,
    session: std::sync::Arc<str>,
    ts: i64,
    turn: u32,
    text: std::sync::Arc<str>,
    who: std::sync::Arc<str>,
    model: std::sync::Arc<str>,
    model_source: std::sync::Arc<str>,
    reply: std::sync::Arc<str>,
    reply_chars: usize,
    reply_shape: std::sync::Arc<crate::model::ReplyShape>,
    side: bool,
    parent: std::sync::Arc<str>,
}
#[derive(Serialize, Deserialize, Clone, Eq, Hash, Ord, PartialEq, PartialOrd)]
struct CEventKey {
    agent: String,
    session: String,
}

#[derive(Clone, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
enum CacheIdentity {
    Source(Vec<crate::ingest::registry::ChangeToken>),
    Token(String),
    #[cfg(windows)]
    HardlinkedSource {
        change: Vec<crate::ingest::registry::ChangeToken>,
        content_sha256: [u8; 32],
    },
    /// The packed rows a released-row record carries; see [`pack_released`].
    Released(Vec<u8>),
}

#[cfg(windows)]
fn refreshable_hardlink_identity(cached: &CacheIdentity, observed: &CacheIdentity) -> bool {
    match (cached, observed) {
        (CacheIdentity::Source(cached), CacheIdentity::HardlinkedSource { change, .. }) => {
            cached == change
        }
        (
            CacheIdentity::HardlinkedSource {
                content_sha256: cached,
                ..
            },
            CacheIdentity::HardlinkedSource {
                content_sha256: observed,
                ..
            },
        ) => cached == observed,
        _ => false,
    }
}

#[cfg(windows)]
enum LogicalPathIndex<T> {
    Single(PathBuf, T),
    Multiple(HashMap<Vec<u16>, Vec<(PathBuf, T)>>),
}

#[cfg(windows)]
impl<T: Clone> LogicalPathIndex<T> {
    fn new(path: PathBuf, value: T) -> Self {
        Self::Single(path, value)
    }

    fn get(&self, path: &Path) -> Option<&T> {
        match self {
            Self::Single(prior, value) => source_path_eq(prior, path).then_some(value),
            Self::Multiple(buckets) => buckets
                .get(&crate::ingest::registry::logical_path_key(path))?
                .iter()
                .find_map(|(prior, value)| source_path_eq(prior, path).then_some(value)),
        }
    }

    fn observe(&mut self, path: PathBuf, value: T) -> Option<T> {
        match self {
            Self::Single(prior, prior_value) if source_path_eq(prior, &path) => {
                Some(prior_value.clone())
            }
            Self::Single(..) => {
                let old = std::mem::replace(self, Self::Multiple(HashMap::new()));
                let Self::Single(prior, prior_value) = old else {
                    unreachable!()
                };
                let Self::Multiple(buckets) = self else {
                    unreachable!()
                };
                buckets
                    .entry(crate::ingest::registry::logical_path_key(&prior))
                    .or_default()
                    .push((prior, prior_value));
                buckets
                    .entry(crate::ingest::registry::logical_path_key(&path))
                    .or_default()
                    .push((path, value));
                None
            }
            Self::Multiple(buckets) => {
                let aliases = buckets
                    .entry(crate::ingest::registry::logical_path_key(&path))
                    .or_default();
                if let Some((_, prior)) = aliases
                    .iter()
                    .find(|(prior, _)| source_path_eq(prior, &path))
                {
                    Some(prior.clone())
                } else {
                    aliases.push((path, value));
                    None
                }
            }
        }
    }
}

#[derive(Serialize, Deserialize, Clone)]
struct Entry {
    mtime: i64,
    size: u64,
    identity: Option<CacheIdentity>,
    msgs: Vec<CMsg>,
    /// Exact event-file ownership for this source. Events themselves stay out of the cache, but
    /// their identities are small and make incremental keep/prune correct for event-only sources.
    event_keys: Vec<CEventKey>,
    /// v8 migration sentinel: prior intake recorded events but their exact sessions were not
    /// cached. A successful current-version parse replaces this with exact keys; an empty
    /// parse must guard.
    legacy_had_events: bool,
    /// Persists across a partial/single-agent migration; this entry cannot cache-hit until its
    /// owning adapter has produced exact current-version event ownership successfully.
    legacy_needs_reparse: bool,
}

#[derive(Deserialize, Default)]
struct CacheFile {
    version: u32,
    entries: HashMap<String, Entry>,
}
struct CacheFileRef<'a> {
    version: u32,
    entries: &'a HashMap<String, Entry>,
}

/// The derived wire shape, with the released-row record (at most one) serialized before every
/// source entry: a bounded reader of the payload prelude then counts sources exactly.
impl Serialize for CacheFileRef<'_> {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::{SerializeMap, SerializeStruct};

        struct RecordsFirst<'a>(&'a HashMap<String, Entry>);
        impl Serialize for RecordsFirst<'_> {
            fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                let mut map = serializer.serialize_map(Some(self.0.len()))?;
                let records = self.0.iter().filter(|(key, _)| released_key(key));
                let sources = self.0.iter().filter(|(key, _)| !released_key(key));
                for (key, entry) in records.chain(sources) {
                    map.serialize_entry(key, entry)?;
                }
                map.end()
            }
        }

        let mut file = serializer.serialize_struct("CacheFileRef", 2)?;
        file.serialize_field("version", &self.version)?;
        file.serialize_field("entries", &RecordsFirst(self.entries))?;
        file.end()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WriterBuildId([u8; WRITER_BUILD_ID_LEN]);

impl WriterBuildId {
    fn parse(bytes: &[u8]) -> Option<Self> {
        let mut normalized = [0_u8; WRITER_BUILD_ID_LEN];
        if bytes.len() != normalized.len() {
            return None;
        }
        for (output, input) in normalized.iter_mut().zip(bytes) {
            *output = match input {
                b'0'..=b'9' | b'a'..=b'f' => *input,
                b'A'..=b'F' => input.to_ascii_lowercase(),
                _ => return None,
            };
        }
        Some(Self(normalized))
    }

    fn current() -> Self {
        if let Some(id) = std::env::var_os("AGREP_RUNTIME_BUILD_ID")
            .and_then(|value| value.into_string().ok())
            .and_then(|value| Self::parse(value.as_bytes()))
        {
            return id;
        }
        if let Ok(Some(id)) = python_composite_writer_build_id() {
            return id;
        }
        static FALLBACK: std::sync::OnceLock<WriterBuildId> = std::sync::OnceLock::new();
        *FALLBACK.get_or_init(|| {
            let digest = direct_executable_digest().unwrap_or_else(|| {
                let mut seed = Vec::with_capacity(include_bytes!("ingest_cache.rs").len() + 64);
                seed.extend_from_slice(b"agrep-rust-ingest-cache-writer-v2\0");
                seed.extend_from_slice(env!("CARGO_PKG_VERSION").as_bytes());
                seed.extend_from_slice(&CACHE_VERSION.to_le_bytes());
                seed.extend_from_slice(&CACHE_BASE_STORAGE_VERSION.to_le_bytes());
                seed.extend_from_slice(include_bytes!("ingest_cache.rs"));
                md5::compute(seed).0
            });
            Self::from_digest(&digest)
        })
    }

    fn from_digest(digest: &[u8]) -> Self {
        debug_assert!(digest.len() >= WRITER_BUILD_ID_LEN / 2);
        let mut text = [0_u8; WRITER_BUILD_ID_LEN];
        const HEX: &[u8; 16] = b"0123456789abcdef";
        for (index, byte) in digest[..WRITER_BUILD_ID_LEN / 2].iter().enumerate() {
            text[index * 2] = HEX[(byte >> 4) as usize];
            text[index * 2 + 1] = HEX[(byte & 0x0f) as usize];
        }
        Self(text)
    }

    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.0).expect("writer build id is ASCII")
    }
}

fn python_composite_writer_build_id() -> Result<Option<WriterBuildId>, &'static str> {
    let Some(raw) = std::env::var_os(PYTHON_RUNTIME_BUILD_ID_ENV) else {
        return Ok(None);
    };
    let value = raw
        .into_string()
        .map_err(|_| "Python runtime writing-build identity is not Unicode")?;
    let runtime = WriterBuildId::parse(value.as_bytes())
        .ok_or("Python runtime writing-build identity is not exact 20-hex")?;
    let binary = direct_executable_digest()
        .ok_or("the running ingest executable cannot be identified exactly")?;
    let mut seed = Vec::with_capacity(64);
    seed.extend_from_slice(b"agrep-derived-writer-v2\0");
    seed.extend_from_slice(&runtime.0);
    seed.extend_from_slice(b"\0");
    seed.extend_from_slice(&binary);
    Ok(Some(WriterBuildId::from_digest(&md5::compute(seed).0)))
}

fn direct_executable_digest() -> Option<[u8; 16]> {
    static DIGEST: std::sync::OnceLock<Option<[u8; 16]>> = std::sync::OnceLock::new();
    *DIGEST.get_or_init(|| {
        let path = std::env::current_exe().ok()?;
        let (mut file, before) = open_regular_read(&path).ok()?;
        let mut digest = md5::Context::new();
        let mut chunk = [0_u8; 1024 * 1024];
        loop {
            let count = file.read(&mut chunk).ok()?;
            if count == 0 {
                break;
            }
            digest.consume(&chunk[..count]);
        }
        let opened_after = file.metadata().ok()?;
        let path_after = regular_metadata(&path).ok()?;
        if !same_snapshot(&before, &opened_after) || !same_snapshot(&before, &path_after) {
            return None;
        }
        Some(digest.compute().0)
    })
}

/// The writing-build ownership observed in an ingest cache base envelope.
///
/// The probe reads only the fixed-size base header and never mutates the cache.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CacheOwnerProbe {
    Missing,
    LegacyUnowned,
    Current { build_id: String },
    Foreign { build_id: String },
    Unreadable,
    Malformed,
}

/// The exact 20-hex identity embedded in new parse-cache base envelopes.
pub fn current_cache_writer_build_id() -> String {
    WriterBuildId::current().as_str().to_owned()
}

/// Atomically re-own validated entries, preserving any required source reparse. Returns the
/// count of sources adopted: the entries that are not a released-row record.
pub fn adopt_foreign_cache(path: &Path) -> Result<usize, &'static str> {
    let (entries, _generation, _backing) =
        decode_cache_owned(path, WriterBuildId::current(), true, DecodeLane::Writer)
            .map_err(CacheDecodeRefusal::label)?;
    let adopted = entries.keys().filter(|key| !released_key(key)).count();
    let cache = IngestCache::base(entries, CacheBacking::Rewrite);
    cache
        .save(path)
        .map_err(|_| "re-owned cache base could not be published")?;
    Ok(adopted)
}

/// Refuse a write when Python delegated its runtime half of the composite
/// identity but the Rust process cannot bind that value to its own executable.
pub fn current_cache_writer_identity_refusal() -> Option<String> {
    if let Some(raw) = std::env::var_os("AGREP_RUNTIME_BUILD_ID") {
        let explicit = match raw
            .into_string()
            .ok()
            .and_then(|value| WriterBuildId::parse(value.as_bytes()))
        {
            Some(value) => value,
            None => return Some("explicit writing-build identity is not exact 20-hex".to_string()),
        };
        return match python_composite_writer_build_id() {
            Ok(Some(observed)) if observed == explicit => None,
            Ok(Some(_)) => Some(
                "explicit writing-build identity does not match the Python runtime and running ingest executable"
                    .to_string(),
            ),
            Ok(None) => None,
            Err(reason) => Some(reason.to_string()),
        };
    }
    match python_composite_writer_build_id() {
        Ok(_) => None,
        Err(reason) => Some(reason.to_string()),
    }
}

#[derive(Deserialize, Serialize)]
struct JournalDelta {
    upserts: Vec<(String, Entry)>,
    deletes: Vec<String>,
}

#[derive(Clone, Copy, Eq, PartialEq)]
struct BaseHeader {
    storage_version: u32,
    writer_build_id: Option<WriterBuildId>,
    instance: [u8; 16],
    raw_len: u64,
    stored_len: u64,
    codec: u32,
    payload_digest: [u8; 16],
    header_len: usize,
}

#[derive(Clone)]
enum BaseWitness {
    Wrapped {
        len: u64,
        modified: Option<std::time::SystemTime>,
        header: BaseHeader,
    },
    Legacy {
        len: u64,
        modified: Option<std::time::SystemTime>,
    },
}

type DecodedCacheBase<'a> = (Cow<'a, [u8]>, [u8; 16], BaseWitness);

struct JournalCursor {
    sequence: u64,
    observed_len: u64,
    valid_len: u64,
    reset: bool,
    frames: u64,
    upserts: HashSet<String>,
    deletes: HashSet<String>,
}

enum CacheBacking {
    Rewrite,
    Delta {
        instance: [u8; 16],
        witness: BaseWitness,
        cursor: Arc<Mutex<JournalCursor>>,
    },
}

#[derive(Deserialize, Serialize)]
struct LegacyEntryV15 {
    mtime: i64,
    size: u64,
    msgs: Vec<LegacyCMsgV37>,
    event_keys: Vec<CEventKey>,
    legacy_had_events: bool,
    legacy_needs_reparse: bool,
}

#[derive(Deserialize, Serialize)]
struct LegacyCacheFileV15 {
    version: u32,
    entries: HashMap<String, LegacyEntryV15>,
}

/// The exact row layout generations 18..=37 wrote (no reply shape). Bincode is positional, so
/// those payloads and journals decode through this shape; every entry then reparses.
#[derive(Deserialize, Serialize)]
struct LegacyCMsgV37 {
    agent: String,
    project: std::sync::Arc<str>,
    session: std::sync::Arc<str>,
    ts: i64,
    turn: u32,
    text: std::sync::Arc<str>,
    who: std::sync::Arc<str>,
    model: std::sync::Arc<str>,
    model_source: std::sync::Arc<str>,
    reply: std::sync::Arc<str>,
    reply_chars: usize,
    side: bool,
    parent: std::sync::Arc<str>,
}

impl LegacyCMsgV37 {
    #[cfg(test)]
    fn from(m: &Message) -> Self {
        let row = CMsg::from(m);
        LegacyCMsgV37 {
            agent: row.agent,
            project: row.project,
            session: row.session,
            ts: row.ts,
            turn: row.turn,
            text: row.text,
            who: row.who,
            model: row.model,
            model_source: row.model_source,
            reply: row.reply,
            reply_chars: row.reply_chars,
            side: row.side,
            parent: row.parent,
        }
    }

    fn current(row: LegacyCMsgV37) -> CMsg {
        CMsg {
            agent: row.agent,
            project: row.project,
            session: row.session,
            ts: row.ts,
            turn: row.turn,
            text: row.text,
            who: row.who,
            model: row.model,
            model_source: row.model_source,
            reply: row.reply,
            reply_chars: row.reply_chars,
            reply_shape: Default::default(),
            side: row.side,
            parent: row.parent,
        }
    }
}

#[derive(Deserialize, Serialize)]
struct LegacyEntryV37 {
    mtime: i64,
    size: u64,
    identity: Option<CacheIdentity>,
    msgs: Vec<LegacyCMsgV37>,
    event_keys: Vec<CEventKey>,
    legacy_had_events: bool,
    legacy_needs_reparse: bool,
}

impl LegacyEntryV37 {
    fn into_entry(self) -> Entry {
        Entry {
            mtime: self.mtime,
            size: self.size,
            identity: self.identity,
            msgs: self.msgs.into_iter().map(LegacyCMsgV37::current).collect(),
            event_keys: self.event_keys,
            legacy_had_events: self.legacy_had_events,
            legacy_needs_reparse: true,
        }
    }

    /// The v37 wire form of a current entry, for tests that stage an older generation.
    #[cfg(test)]
    fn downgrade(entry: &Entry) -> Self {
        LegacyEntryV37 {
            mtime: entry.mtime,
            size: entry.size,
            identity: entry.identity.clone(),
            msgs: entry
                .msgs
                .iter()
                .map(|row| LegacyCMsgV37::from(&row.to_msg()))
                .collect(),
            event_keys: entry.event_keys.clone(),
            legacy_had_events: entry.legacy_had_events,
            legacy_needs_reparse: entry.legacy_needs_reparse,
        }
    }
}

#[derive(Deserialize, Serialize)]
struct LegacyCacheFileV37 {
    version: u32,
    entries: HashMap<String, LegacyEntryV37>,
}

#[derive(Deserialize, Serialize)]
struct LegacyJournalDeltaV37 {
    upserts: Vec<(String, LegacyEntryV37)>,
    deletes: Vec<String>,
}

/// Message.agent is &'static str; resolve a cached string against the registry so a
/// new adapter can never round-trip through the cache relabeled "unknown".
fn intern_agent(s: &str) -> &'static str {
    crate::ingest::registry::ADAPTERS
        .iter()
        .map(|a| a.name())
        .find(|n| *n == s)
        .unwrap_or("unknown")
}

impl CMsg {
    // both directions are refcount bumps (Arc fields), not string copies - on a warm
    // run every cached message round-trips through here, so this is the hot edge
    fn from(m: &Message) -> Self {
        CMsg {
            agent: m.agent.to_string(),
            project: m.project.clone(),
            session: m.session.clone(),
            ts: m.ts,
            turn: m.turn,
            text: m.text.clone(),
            who: m.who.clone(),
            model: m.model.clone(),
            model_source: m.model_source.clone(),
            reply: m.reply.clone(),
            reply_chars: m.reply_chars,
            reply_shape: m.reply_shape.clone(),
            side: m.side,
            parent: m.parent.clone(),
        }
    }
    fn to_msg(&self) -> Message {
        Message {
            agent: intern_agent(&self.agent),
            project: self.project.clone(),
            session: self.session.clone(),
            ts: self.ts,
            turn: self.turn,
            text: self.text.clone(),
            who: self.who.clone(),
            model: self.model.clone(),
            model_source: self.model_source.clone(),
            reply: self.reply.clone(),
            reply_chars: self.reply_chars,
            reply_shape: self.reply_shape.clone(),
            side: self.side,
            parent: self.parent.clone(),
        }
    }
}

/// `(agent, database namespace, session)` of a token conversation: one session id may name
/// different conversations in different databases of the same agent.
type TokenConversation = (String, String, String);

/// A row's text as a map key: equal only to the same text whole, but hashed by its length and
/// 16 bytes at its start, middle and end alone, so a long text costs no more than a short one.
#[derive(Clone, Copy, Eq, PartialEq)]
struct Text<'a>(&'a str);

impl std::hash::Hash for Text<'_> {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        let bytes = self.0.as_bytes();
        let sample = |at: usize| &bytes[at..bytes.len().min(at + 16)];
        state.write_usize(bytes.len());
        state.write(sample(0));
        state.write(sample(bytes.len() / 2));
        state.write(sample(bytes.len().saturating_sub(16)));
    }
}

/// Rows counted by `(session, timestamp, text)`.
#[derive(Default)]
struct RowCounts<'a>(HashMap<(&'a str, i64, Text<'a>), usize>);

impl<'a> RowCounts<'a> {
    fn add(&mut self, (session, ts, text): (&'a str, i64, &'a str), count: usize) {
        *self.0.entry((session, ts, Text(text))).or_default() += count;
    }

    fn count(&self, (session, ts, text): (&'a str, i64, &'a str)) -> usize {
        self.0.get(&(session, ts, Text(text))).copied().unwrap_or(0)
    }

    /// Takes one row bearing `key`, if one is left.
    fn take(&mut self, (session, ts, text): (&'a str, i64, &'a str)) -> bool {
        let left = self
            .0
            .get_mut(&(session, ts, Text(text)))
            .filter(|count| **count > 0);
        left.map(|count| *count -= 1).is_some()
    }

    fn iter(&self) -> impl Iterator<Item = ((&'a str, i64, &'a str), usize)> + '_ {
        let counts = self.0.iter();
        counts.map(|(&(session, ts, Text(text)), &count)| ((session, ts, text), count))
    }
}

/// A cached row of the agent a published generation is judged for.
struct CachedRow<'a> {
    row: &'a CMsg,
    /// Its source lies outside the scopes the preflight failed to read.
    beside: bool,
    /// Its entry is the base's own.
    unchanged: bool,
}

/// The rows publication keeps of `rows`, counted: of rows sharing a session, raw turn and
/// text, dedupe keeps the canonical one. A tie it may break either way, if the tied rows'
/// timestamps differ, counts for neither.
fn published_counts<'a>(rows: impl IntoIterator<Item = &'a CMsg>) -> RowCounts<'a> {
    use std::collections::hash_map::Entry as Slot;

    let rows = rows.into_iter();
    let mut kept: HashMap<(&str, u32, Text), Option<&CMsg>> =
        HashMap::with_capacity(rows.size_hint().0);
    for row in rows {
        let mut slot = match kept.entry((row.session.as_ref(), row.turn, Text(&row.text))) {
            Slot::Vacant(slot) => {
                slot.insert(Some(row));
                continue;
            }
            Slot::Occupied(slot) => slot,
        };
        let Some(prior) = *slot.get() else {
            continue;
        };
        let order = crate::ingest::registry::canonical_message_cmp(&row.to_msg(), &prior.to_msg());
        if order.is_gt() {
            slot.insert(Some(row));
        } else if order.is_eq() && row.ts != prior.ts {
            slot.insert(None);
        }
    }
    let mut counts = RowCounts(HashMap::with_capacity(kept.len()));
    for ((session, _, Text(text)), row) in kept {
        if let Some(row) = row {
            counts.add((session, row.ts, text), 1);
        }
    }
    counts
}

/// Of one agent, what passes since the published generation saw go beside the scopes they
/// failed to read: rows this pass's base held, rows only an earlier pass's record names, by
/// `(session, timestamp)` and [`crate::cache::text_digest`], and sessions gone whole.
#[derive(Default)]
struct Released<'a> {
    rows: RowCounts<'a>,
    recorded: HashMap<(&'a str, i64), Vec<(crate::cache::TextDigest, usize)>>,
    sessions: HashSet<&'a str>,
}

impl<'a> Released<'a> {
    /// Takes one row bearing `key`, if one is left.
    fn take(&mut self, key: (&'a str, i64, &'a str)) -> bool {
        if self.rows.take(key) {
            return true;
        }
        let (session, ts, text) = key;
        let Some(recorded) = self.recorded.get_mut(&(session, ts)) else {
            return false;
        };
        let digest = crate::cache::text_digest(text);
        let left = recorded
            .iter_mut()
            .find(|(held, count)| *count > 0 && *held == digest);
        left.map(|(_, count)| *count -= 1).is_some()
    }

    fn record(&self) -> ReleasedRows {
        let mut rows = HashMap::new();
        for ((session, ts, text), count) in self.rows.iter() {
            let digest = crate::cache::text_digest(text);
            *rows.entry((session.to_owned(), ts, digest)).or_default() += count;
        }
        for (&(session, ts), recorded) in &self.recorded {
            for &(digest, count) in recorded.iter().filter(|(_, count)| *count > 0) {
                *rows.entry((session.to_owned(), ts, digest)).or_default() += count;
            }
        }
        let sessions = self.sessions.iter().map(|session| (*session).to_owned());
        ReleasedRows {
            rows,
            sessions: sessions.collect(),
        }
    }
}

/// [`Released`] as the cache stages it.
#[derive(Debug, Default, PartialEq, Eq)]
struct ReleasedRows {
    rows: HashMap<(String, i64, crate::cache::TextDigest), usize>,
    sessions: HashSet<String>,
}

/// A 0.4.2 record: every agent's rows as JSON in the key, naming its seal inside.
#[derive(Deserialize)]
struct PackedReleased {
    seal: String,
    agents: std::collections::BTreeMap<String, PackedAgent>,
}

/// One agent's rows as `(session, timestamp, digest, count)`, then its sessions gone whole.
type PackedAgent = (Vec<(String, i64, String, usize)>, Vec<String>);

/// Every agent's [`ReleasedRows`] as the entry packs them: sessions interned once per agent, each
/// row `(session index, timestamp, digest, count)`, then the indexes of sessions gone whole.
#[derive(Deserialize, Serialize)]
struct PackedReleasedRows {
    agents: std::collections::BTreeMap<String, PackedAgentRows>,
}

#[derive(Deserialize, Serialize)]
struct PackedAgentRows {
    sessions: Vec<String>,
    rows: Vec<(u32, i64, [u8; 16], u32)>,
    gone: Vec<u32>,
}

/// One reserved key, naming the sealed generation, packs every record staged with the cache, so
/// the base's entry count stays its count of sources. Every key under `\0released` is a record,
/// and only this pass's survives it.
const RELEASED_KEY: &str = "\0released\0";

fn released_key(key: &str) -> bool {
    key.starts_with("\0released")
}

fn released_entry(packed: Vec<u8>) -> Entry {
    Entry {
        mtime: 0,
        size: 0,
        identity: Some(CacheIdentity::Released(packed)),
        msgs: Vec::new(),
        event_keys: Vec::new(),
        legacy_had_events: false,
        legacy_needs_reparse: false,
    }
}

fn hex16(bytes: &[u8; 16]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn parse_hex16(text: &str) -> Option<[u8; 16]> {
    let mut bytes = [0; 16];
    if text.len() != 32 || !text.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    for (index, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[2 * index..2 * index + 2], 16).ok()?;
    }
    Some(bytes)
}

/// The record of `released` against the generation `seal` names, as `(key, entry)`; None when it
/// records nothing.
fn pack_released(seal: &str, released: &HashMap<String, ReleasedRows>) -> Option<(String, Entry)> {
    let mut agents = std::collections::BTreeMap::new();
    for (agent, records) in released {
        if records.rows.is_empty() && records.sessions.is_empty() {
            continue;
        }
        let mut names: Vec<&str> = records
            .rows
            .keys()
            .map(|(session, _, _)| session.as_str())
            .chain(records.sessions.iter().map(String::as_str))
            .collect();
        names.sort_unstable();
        names.dedup();
        let index = |session: &str| names.binary_search(&session).ok().map(|at| at as u32);
        let mut rows: Vec<(u32, i64, [u8; 16], u32)> = records
            .rows
            .iter()
            .filter_map(|((session, ts, digest), count)| {
                Some((index(session)?, *ts, *digest, u32::try_from(*count).ok()?))
            })
            .collect();
        rows.sort_unstable();
        let mut gone: Vec<u32> = records
            .sessions
            .iter()
            .filter_map(|session| index(session))
            .collect();
        gone.sort_unstable();
        agents.insert(
            agent.clone(),
            PackedAgentRows {
                sessions: names.into_iter().map(str::to_owned).collect(),
                rows,
                gone,
            },
        );
    }
    if agents.is_empty() {
        return None;
    }
    let packed = bincode::serialize(&PackedReleasedRows { agents }).ok()?;
    Some((format!("{RELEASED_KEY}{seal}"), released_entry(packed)))
}

fn unpack_released(entry: &Entry) -> Option<HashMap<String, ReleasedRows>> {
    let Some(CacheIdentity::Released(packed)) = entry.identity.as_ref() else {
        return None;
    };
    let packed: PackedReleasedRows = bincode::deserialize(packed).ok()?;
    let mut released: HashMap<String, ReleasedRows> = HashMap::new();
    for (agent, agent_rows) in packed.agents {
        let records = released.entry(agent).or_default();
        let session = |index: u32| agent_rows.sessions.get(index as usize).cloned();
        for (at, ts, digest, count) in &agent_rows.rows {
            if let Some(session) = session(*at).filter(|_| *count > 0) {
                *records.rows.entry((session, *ts, *digest)).or_default() += *count as usize;
            }
        }
        records
            .sessions
            .extend(agent_rows.gone.iter().filter_map(|at| session(*at)));
    }
    Some(released)
}

fn unpack_legacy_released(json: &str, seal: &str) -> Option<HashMap<String, ReleasedRows>> {
    let packed = serde_json::from_str::<PackedReleased>(json).ok()?;
    if packed.seal != seal {
        return None;
    }
    let mut released: HashMap<String, ReleasedRows> = HashMap::new();
    for (agent, (rows, sessions)) in packed.agents {
        let records = released.entry(agent).or_default();
        for (session, ts, digest, count) in rows {
            if let Some(digest) = parse_hex16(&digest).filter(|_| count > 0) {
                *records.rows.entry((session, ts, digest)).or_default() += count;
            }
        }
        records.sessions.extend(sessions);
    }
    Some(released)
}

/// The [`ReleasedRows`] of each agent `entries` record against the generation `seal` names; none
/// unless exactly one record names it.
fn recorded_released_rows(
    entries: &HashMap<String, Entry>,
    seal: &str,
) -> HashMap<String, ReleasedRows> {
    let mut packs = entries.iter().filter_map(|(key, entry)| {
        let rest = key.strip_prefix(RELEASED_KEY)?;
        if rest == seal {
            unpack_released(entry)
        } else {
            unpack_legacy_released(rest, seal)
        }
    });
    match (packs.next(), packs.next()) {
        (Some(packed), None) => packed,
        _ => HashMap::new(),
    }
}

pub struct IngestCache {
    entries: HashMap<String, Entry>,
    dirty: HashSet<String>,
    deleted: HashSet<String>,
    backing: CacheBacking,
    /// A foreign-owned cache can be inspected for fallback rows only. It must never be staged
    /// over by this build, including after a decode refusal in a newer payload format.
    write_blocked: bool,
    /// true when a valid cache was loaded - i.e. this run is INCREMENTAL (only changed files
    /// re-parsed, so the returned events cover only touched sessions). false on a cold/forced
    /// load means every file is parsed and the event set is complete.
    pub warm: bool,
    /// Sessions whose rows could have changed this run (accumulated across adapters as
    /// collect_cached re-parses changed files). The corpus refresh reads this to re-index only
    /// these sessions instead of rescanning the whole materialized corpus.
    pub touched: HashSet<String>,
    /// Event files owned by changed/deleted sources this run. After all adapters finish, main
    /// subtracts the exact live ownership union and prunes only the remaining scoped files.
    event_touched: HashSet<String>,
    /// A guard served last-known-good rows because a source that should have been
    /// reparsed was temporarily unreadable/empty. This is deliberately per-run (not
    /// serialized): the caller must not publish a source snapshot for this run, so the next
    /// ingest retries the guarded source instead of taking the source-identical shortcut.
    guarded_stale: bool,
    /// Monotonic per-run guard counter used to attribute a new guard to its adapter even when
    /// an earlier adapter already made `guarded_stale` true.
    guard_epoch: u64,
    /// A guard fired without a complete last-good cache fallback. Publishing derived outputs from
    /// this pass would drop prior rows; callers must abort when a prior generation exists.
    output_incomplete: bool,
    /// A read that failed with no last-good copy left output incomplete where a published
    /// generation may hold its rows: unlike `output_incomplete`, an explicit rebuild may publish
    /// without them ([`Self::output_complete_past_unreadable`]).
    unreadable_unserved: bool,
    /// A failed token read with no last-good copy was let through because nothing published
    /// could hold its rows; such a pass may not publish a generation of no rows at all.
    admitted_unpublished_failure: bool,
    /// Failed token conversations with no last-good copy that neither a base nor the namespace
    /// record proves unpublished: output stays incomplete until a read of the verified published
    /// generation does ([`Self::admit_unpublished_token_conversations`]).
    unproven_token_failures: Vec<TokenConversation>,
    /// Malformed token conversations this pass served from a last good copy or proved
    /// unpublished: a durable fact a published snapshot may stand beside. A panic is not one;
    /// the next pass retries it.
    settled_token_failures: HashSet<TokenConversation>,
    /// The staleness token of each failed token conversation no last-good copy served.
    failed_token_identities: HashMap<TokenConversation, String>,
    /// Automatic event repair retains old entries as fallback but forces every Stat/
    /// Token source through its parser so the otherwise-uncached event stream is complete.
    force_reparse: bool,
    repair_mode: bool,
    /// Guarded recovery may accept a Stat/Token deletion after the current preflight and adapter
    /// independently observe that exact source absent. Unrelated live writers cannot veto it.
    allow_stable_deletions: bool,
    /// Agents whose clean ENOENT absence two complete preflights observed; whole-root loss
    /// needs the same absent preflight on two runs, not merely one stable walk.
    repeated_absent_agents: HashSet<String>,
    /// The same two-observation witness for single store roots of a still-present agent.
    repeated_absent_roots: Vec<(String, PathBuf)>,
    /// Per token agent, the project the published generation's rows gave each conversation;
    /// read only for an agent whose attribution source the preflight could not read.
    published_token_projects: HashMap<String, HashMap<String, String>>,
    /// Epoch nanoseconds before this run's first source stat; stamps not safely older are
    /// recorded racy. `None` keeps every stamp trusted for callers that never set it.
    racy_scan_start_ns: Option<i64>,
    /// Recovery started from no last-good rows because the on-disk base was discarded. Absence
    /// of prior material is then a missing witness, never proof, so whole-store deletion still
    /// needs the repeated observation a cached generation would otherwise have supplied.
    discarded_base: bool,
    /// The caller confirmed the repeated stable source observation a discarded base needs
    /// before any stable-deletion grant may bind (see [`Self::stable_deletions_provable`]).
    discarded_base_confirmed: bool,
    #[cfg(test)]
    provisional_deletions: bool,
    /// Source paths the last published generation contained. `None` means no inventory was
    /// supplied, which publication guards must read as unknown rather than as "nothing".
    published_material: Option<HashSet<PathBuf>>,
    /// Scopes the published inventory could not enumerate, so it contributed no path for them
    /// whether or not they held material. Absence under such a scope proves nothing.
    published_blind_scopes: HashSet<(String, PathBuf)>,
    /// The inventory was rebuilt from this cache. Every Stat and token row reaches the cache
    /// before it publishes, but a whole store's need not, nor a partial read of an older build.
    published_from_cache: bool,
    /// Decoded from an older cache generation, whose builds published partial reads uncached.
    legacy_generation: bool,
    /// Agents no publication can drop anything of: the verified published generation holds no
    /// row or event of theirs, or this pass serves every one of them it holds.
    unpublished_agents: HashSet<String>,
    /// Agents whose snapshot this pass seeded from a read that missed part of their store: not
    /// yet published, it vouches for nothing the read missed.
    seeded_snapshots: HashSet<String>,
    /// `(agent, root)` of whole-store reads that failed with no last-good snapshot to serve:
    /// output only once the inventory or a read of the published generation proves it empty.
    unvouched_reads: HashSet<(String, PathBuf)>,
    /// For each per-file agent whose failed read this cache cannot answer for alone, whether the
    /// pass holds every row the published generation does; None where that could not be read.
    published_rows: HashMap<String, Option<bool>>,
    /// The base's own version of each entry changed since [`Self::mark_base_rows`], None where it
    /// held none: the base is the current entries with these put back.
    displaced: HashMap<String, Option<Entry>>,
    /// The scopes the preflight failed to read, which no count of rows seen go looks under.
    failed_scopes: Vec<PathBuf>,
    /// The published generation this pass compares with and stages released rows against.
    published_seal: Option<crate::cache::GenerationSeal>,
    /// What passes since that generation saw go, by agent: [`Self::record_released_rows`].
    released: HashMap<String, ReleasedRows>,
    /// `(agent, database namespace)` of every token conversation the published generation may
    /// hold rows or events of. `None` is unknown: every token database counts as material.
    published_token_namespaces: Option<HashSet<(String, String)>>,
    /// This cache decoded a last-good generation, so "no entry under X" is a positive fact
    /// about what was published rather than a missing witness.
    last_good_base: bool,
    repair_expected_agents: HashSet<String>,
    repair_expected_paths: HashSet<PathBuf>,
    current_snapshot_complete: bool,
    current_source_agents: HashSet<String>,
    current_cleanly_absent_agents: HashSet<String>,
    coverage_expected_paths: HashSet<PathBuf>,
    coverage_expected_tokens: HashMap<String, Vec<(String, String)>>,
    source_stamps: HashMap<PathBuf, crate::ingest::registry::SourceStatStamp>,
    source_read_issues: Vec<SourceReadIssue>,
    /// Every entry's source path, built on first use and dropped when a key is added or removed.
    source_paths: std::sync::OnceLock<HashSet<PathBuf>>,
}

#[cfg(all(windows, test))]
fn source_key(path: &Path) -> String {
    let metadata = fs::symlink_metadata(path).expect("source key metadata");
    let (_, identity) =
        crate::ingest::registry::metadata_change_token_with_file_identity(path, &metadata)
            .expect("source key identity");
    windows_source_key(path, identity)
}

#[cfg(not(windows))]
fn source_key(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

#[cfg(windows)]
fn windows_source_key(
    path: &Path,
    identity: crate::ingest::registry::WindowsFileIdentity,
) -> String {
    let mut key = identity.cache_key();
    append_windows_path(&mut key, path);
    key
}

#[cfg(windows)]
fn append_windows_path(key: &mut String, path: &Path) {
    use std::fmt::Write as _;
    use std::os::windows::ffi::OsStrExt;

    key.push('\0');
    for unit in path.as_os_str().encode_wide() {
        write!(key, "{unit:04x}").unwrap();
    }
}

#[cfg(windows)]
fn source_path_from_key(key: &str) -> Option<PathBuf> {
    use std::ffi::OsString;
    use std::os::windows::ffi::OsStringExt;

    if !key.starts_with("\0file\0") {
        return (!key.starts_with('\0')).then(|| PathBuf::from(key));
    }
    let (_, encoded) = key.rsplit_once('\0')?;
    if encoded.len() % 4 != 0 || !encoded.is_ascii() {
        return None;
    }
    let mut units = Vec::with_capacity(encoded.len() / 4);
    for chunk in encoded.as_bytes().chunks_exact(4) {
        let chunk = std::str::from_utf8(chunk).ok()?;
        units.push(u16::from_str_radix(chunk, 16).ok()?);
    }
    Some(PathBuf::from(OsString::from_wide(&units)))
}

#[cfg(not(windows))]
fn source_path_from_key(key: &str) -> Option<PathBuf> {
    (!key.starts_with('\0')).then(|| PathBuf::from(key))
}

#[cfg(windows)]
fn file_identity_prefix(key: &str) -> Option<&str> {
    key.starts_with("\0file\0")
        .then(|| key.rsplit_once('\0').map(|pair| pair.0))
        .flatten()
}

#[cfg(not(windows))]
fn file_identity_prefix(_key: &str) -> Option<&str> {
    None
}

#[cfg(windows)]
fn source_path_eq(left: &Path, right: &Path) -> bool {
    crate::ingest::registry::path_eq(left, right)
}

#[cfg(not(windows))]
fn source_path_eq(left: &Path, right: &Path) -> bool {
    left == right
}

#[cfg(windows)]
fn source_path_within(path: &Path, root: &Path) -> bool {
    crate::ingest::registry::relative_path(path, root).is_some()
}

#[cfg(not(windows))]
fn source_path_within(path: &Path, root: &Path) -> bool {
    path.starts_with(root)
}

#[cfg(windows)]
fn source_relative(path: &Path, root: &Path) -> Option<PathBuf> {
    crate::ingest::registry::relative_path(path, root)
}

#[cfg(not(windows))]
fn source_relative(path: &Path, root: &Path) -> Option<PathBuf> {
    path.strip_prefix(root).ok().map(Path::to_path_buf)
}

/// Whether `path`, absent beneath `root`, may still exist behind an ancestor no walk follows: a
/// link (a day directory moved onto a drive since unmounted) or anything not proven a directory.
/// Beneath directories proven real, a plain file where one was, or an ancestor a stat proves
/// gone, absence is a deletion, as the preflight walk judges it.
fn absence_unproven(root: &Path, path: &Path) -> bool {
    let Some(relative) = source_relative(path, root) else {
        return false;
    };
    let mut ancestor = root.to_path_buf();
    let mut components = relative.components().peekable();
    while let Some(component) = components.next() {
        if components.peek().is_none() {
            return false;
        }
        ancestor.push(component);
        match fs::symlink_metadata(&ancestor) {
            Ok(metadata) if metadata.is_dir() => {}
            Ok(metadata) if metadata.is_file() => return false,
            Err(error) if crate::ingest::registry::stat_proves_absent(&ancestor, &error) => {
                return false
            }
            _ => return true,
        }
    }
    false
}

pub(crate) fn epoch_ns(time: std::time::SystemTime) -> Option<i64> {
    let since = time.duration_since(std::time::UNIX_EPOCH).ok()?;
    i64::try_from(since.as_nanos()).ok()
}

/// Racy: a same-size rewrite could still receive this stamp, because it is not safely older
/// than the scan start. Far-future stamps are not: a later write stamps the present, and
/// re-verifying a future-dated file would repeat every run. The window is `(scan start - margin,
/// now + max(margin, 2 s)]` on the stamping clock: a remote clock more than 2 s ahead, or behind
/// by more than the margin, can stamp a write made during the scan outside it.
#[cfg(not(windows))]
pub(crate) fn racy_stamp(mtime_ns: i64, scan_start_ns: i64, now_ns: i64) -> bool {
    const FUTURE_SLACK_NS: i64 = 2_000_000_000;
    let margin = racy_margin_ns(mtime_ns);
    mtime_ns > scan_start_ns.saturating_sub(margin)
        && mtime_ns <= now_ns.saturating_add(margin.max(FUTURE_SLACK_NS))
}

/// Windows identity is the change journal's USN or a content digest, never a timestamp.
#[cfg(windows)]
pub(crate) fn racy_stamp(_mtime_ns: i64, _scan_start_ns: i64, _now_ns: i64) -> bool {
    false
}

/// A later write can share a stamp only within one timestamp granule plus however far the
/// stamping clock lags the scan clock. A stamp ending in k zeros may come from a filesystem
/// that coarse (10 ms exFAT, 1 s HFS+/ext3); whole seconds may be FAT's 2 s granules.
#[cfg(not(windows))]
fn racy_margin_ns(mtime_ns: i64) -> i64 {
    const SECOND: i64 = 1_000_000_000;
    let mut granule = 1_i64;
    while granule < SECOND && mtime_ns % (granule * 10) == 0 {
        granule *= 10;
    }
    if granule == SECOND {
        granule = 2 * SECOND;
    }
    granule.saturating_add(timestamp_clock_lag_ns())
}

/// Linux stamps files from the coarse clock, which trails CLOCK_REALTIME by under one tick
/// and never leads it: a write after the scan start stamps later than `start - tick`.
#[cfg(target_os = "linux")]
fn timestamp_clock_lag_ns() -> i64 {
    static TICK: std::sync::LazyLock<i64> =
        std::sync::LazyLock::new(|| coarse_clock_tick_ns().unwrap_or(10_000_000));
    *TICK
}

#[cfg(target_os = "linux")]
fn coarse_clock_tick_ns() -> Option<i64> {
    #[repr(C)]
    struct Timespec {
        tv_sec: std::ffi::c_long,
        tv_nsec: std::ffi::c_long,
    }
    unsafe extern "C" {
        fn clock_getres(clock: std::ffi::c_int, resolution: *mut Timespec) -> std::ffi::c_int;
    }
    const CLOCK_REALTIME_COARSE: std::ffi::c_int = 5;
    let mut resolution = Timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock_getres writes one timespec through a valid, exclusively borrowed pointer.
    if unsafe { clock_getres(CLOCK_REALTIME_COARSE, &mut resolution) } != 0 {
        return None;
    }
    // c_long is i64 on 64-bit Linux, where clippy calls this a no-op, but i32 on 32-bit targets.
    #[allow(clippy::useless_conversion)]
    let tick = i64::from(resolution.tv_sec)
        .saturating_mul(1_000_000_000)
        .saturating_add(i64::from(resolution.tv_nsec));
    (1..=1_000_000_000).contains(&tick).then_some(tick)
}

/// Apple filesystems stamp from the precise clock: a later write always gets a later stamp.
#[cfg(target_vendor = "apple")]
fn timestamp_clock_lag_ns() -> i64 {
    0
}

/// Other kernels: assume a coarse clock with a tick up to 20 ms (HZ=50).
#[cfg(not(any(target_os = "linux", target_vendor = "apple", windows)))]
fn timestamp_clock_lag_ns() -> i64 {
    20_000_000
}

fn normalized_source_entries(entries: HashMap<String, Entry>) -> HashMap<String, Entry> {
    entries
}

fn cached_event_keys(events: &[Event]) -> Vec<CEventKey> {
    let mut keys: Vec<CEventKey> = events
        .iter()
        .filter(|event| !event.session.is_empty())
        .map(|event| CEventKey {
            agent: event.agent.to_string(),
            session: event.session.clone(),
        })
        .collect();
    keys.sort_unstable();
    keys.dedup();
    keys
}

/// Whether a partial read kept everything a cached entry holds: each of its rows, whose reply may
/// only have grown, and each session it owns events of. Replacing the entry then loses nothing.
fn partial_read_extends(prev: &Entry, fresh: &[Message], fresh_events: &[Event]) -> bool {
    if prev.legacy_had_events {
        return false;
    }
    let rows: HashMap<(&str, u32, &str), &str> = fresh
        .iter()
        .map(|row| {
            (
                (row.session.as_ref(), row.turn, row.text.as_ref()),
                row.reply.as_ref(),
            )
        })
        .collect();
    let sessions: HashSet<&str> = fresh_events
        .iter()
        .map(|event| event.session.as_str())
        .collect();
    prev.msgs.iter().all(|row| {
        rows.get(&(row.session.as_ref(), row.turn, row.text.as_ref()))
            .is_some_and(|reply| reply.starts_with(row.reply.as_ref()))
    }) && prev
        .event_keys
        .iter()
        .all(|key| sessions.contains(key.session.as_str()))
}

#[cfg(not(windows))]
fn legacy_eventful_keys(cache_path: &Path) -> HashSet<String> {
    let Some(data) = cache_path.parent() else {
        return HashSet::new();
    };
    let Some(files) = fs::read(data.join("intake_stats.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<serde_json::Value>(&bytes).ok())
        .and_then(|book| {
            book.get("files")
                .and_then(|files| files.as_object())
                .cloned()
        })
    else {
        return HashSet::new();
    };
    let mut eventful = HashSet::new();
    for (id, entry) in files {
        if entry
            .get("events")
            .and_then(|value| value.as_u64())
            .unwrap_or(0)
            == 0
        {
            continue;
        }
        eventful.insert(id.clone());
        let Some(agent) = entry.get("agent").and_then(|value| value.as_str()) else {
            continue;
        };
        let fingerprint = crate::ingest::registry::ADAPTERS
            .iter()
            .find(|adapter| adapter.name() == agent)
            .map(|adapter| adapter.fingerprint());
        match fingerprint {
            Some(crate::ingest::registry::Fingerprint::Always) => {
                eventful.insert(format!("\0snapshot\0{agent}"));
            }
            Some(crate::ingest::registry::Fingerprint::Token) => {
                if let Some((_, session)) = crate::intake::parse_token_id(&id) {
                    eventful.insert(format!("\0tok\0{agent}\0{session}"));
                }
            }
            _ => {}
        }
    }
    eventful
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum CacheGeneration {
    Current,
    LegacyReparse,
}

/// Which decode check refused the on-disk parse cache. Surfaced through
/// [`IngestCache::repair`]/[`IngestCache::guarded_retry`] so recovery refusals can name the
/// failed check instead of an anonymous "no valid parse cache".
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CacheDecodeRefusal {
    MissingFile,
    UnreadableFile,
    ForeignOwner,
    BaseHeader,
    BaseLengthMismatch,
    PayloadDigestMismatch,
    StorageVersion,
    PayloadShape,
    StaleJournal,
}

impl CacheDecodeRefusal {
    pub fn label(self) -> &'static str {
        match self {
            Self::MissingFile => "missing cache file",
            Self::UnreadableFile => "unreadable cache file",
            Self::ForeignOwner => "cache owned by another build",
            Self::BaseHeader => "malformed cache header",
            Self::BaseLengthMismatch => "cache length mismatch",
            Self::PayloadDigestMismatch => "cache digest mismatch",
            Self::StorageVersion => "cache version mismatch",
            Self::PayloadShape => "undecodable cache payload",
            Self::StaleJournal => "cache journal behind its last commit",
        }
    }

    /// The bytes were read and this build conclusively cannot decode them, so no retry changes
    /// the verdict. Such a base is a broken derived artifact agrep owns: recovery discards and
    /// reparses it. Missing/unreadable/foreign/stale bases are not this - see the callers' refusals.
    pub fn is_undecodable(self) -> bool {
        matches!(
            self,
            Self::BaseHeader
                | Self::BaseLengthMismatch
                | Self::PayloadDigestMismatch
                | Self::StorageVersion
                | Self::PayloadShape
        )
    }
}

impl std::fmt::Display for CacheDecodeRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.label())
    }
}

/// These generations share the current entry layout but require current parser semantics.
fn reparse_compatible_cache_version(version: u32) -> bool {
    matches!(version, 18..=19 | 21..=37)
}

/// Decode current/reparse-compatible entries or migrate the exact v8 wire shape.
fn decode_cache_payload(
    bytes: &[u8],
    _path: &Path,
) -> Result<(HashMap<String, Entry>, CacheGeneration, bool), CacheDecodeRefusal> {
    let mut refusal = CacheDecodeRefusal::PayloadShape;
    // generations 18..=37 wrote rows without a reply shape: their own layout decodes them
    if read_u32(bytes, 0).is_some_and(reparse_compatible_cache_version) {
        if let Ok(cache) = bincode::deserialize::<LegacyCacheFileV37>(bytes) {
            let entries = cache
                .entries
                .into_iter()
                .map(|(key, entry)| (key, entry.into_entry()))
                .collect();
            return Ok((entries, CacheGeneration::LegacyReparse, false));
        }
    }
    if let Ok(cache) = bincode::deserialize::<CacheFile>(bytes) {
        if cache.version == CACHE_VERSION {
            #[cfg(windows)]
            if cache.entries.keys().any(|key| {
                key.starts_with("\0file\0") && source_path_from_key(key).is_none()
                    || !key.starts_with('\0')
            }) {
                return Err(CacheDecodeRefusal::PayloadShape);
            }
            // Takeover re-encodes an older generation at this version with every entry flagged:
            // it reparses whole. Once a pass has made any entry current, the entries it could not
            // reparse keep their flag and rows and reparse one by one on warm passes.
            let mut held = cache
                .entries
                .iter()
                .filter(|(key, _)| !released_key(key))
                .peekable();
            let generation =
                if held.peek().is_some() && held.all(|(_, entry)| entry.legacy_needs_reparse) {
                    CacheGeneration::LegacyReparse
                } else {
                    CacheGeneration::Current
                };
            return Ok((cache.entries, generation, true));
        }
        // a payload carrying an older version in the current layout (a generation staged for
        // an upgrade test) still retains its rows for reparse
        if reparse_compatible_cache_version(cache.version) {
            let mut entries = cache.entries;
            for entry in entries.values_mut() {
                entry.legacy_needs_reparse = true;
            }
            return Ok((entries, CacheGeneration::LegacyReparse, true));
        }
        if matches!(cache.version, 13..=16) {
            #[cfg(windows)]
            {
                return Err(CacheDecodeRefusal::StorageVersion);
            }
            #[cfg(not(windows))]
            {
                let mut entries = cache.entries;
                for entry in entries.values_mut() {
                    entry.identity = None;
                    entry.legacy_needs_reparse = true;
                }
                return Ok((entries, CacheGeneration::LegacyReparse, false));
            }
        }
        refusal = CacheDecodeRefusal::StorageVersion;
    }
    if let Ok(cache) = bincode::deserialize::<LegacyCacheFileV15>(bytes) {
        if matches!(cache.version, 13..=16) {
            #[cfg(windows)]
            {
                return Err(CacheDecodeRefusal::StorageVersion);
            }
            #[cfg(not(windows))]
            {
                let entries = cache
                    .entries
                    .into_iter()
                    .map(|(key, entry)| {
                        (
                            key,
                            Entry {
                                mtime: entry.mtime,
                                size: entry.size,
                                identity: None,
                                msgs: entry.msgs.into_iter().map(LegacyCMsgV37::current).collect(),
                                event_keys: entry.event_keys,
                                legacy_had_events: entry.legacy_had_events,
                                legacy_needs_reparse: true,
                            },
                        )
                    })
                    .collect();
                return Ok((entries, CacheGeneration::LegacyReparse, false));
            }
        }
    }
    #[cfg(windows)]
    return Err(refusal);
    #[cfg(not(windows))]
    let legacy = bincode::deserialize::<LegacyCacheFileV8>(bytes).map_err(|_| refusal)?;
    #[cfg(not(windows))]
    if legacy.version != 8 {
        return Err(refusal);
    }
    #[cfg(not(windows))]
    let eventful = legacy_eventful_keys(_path);
    #[cfg(not(windows))]
    let mut entries: HashMap<String, Entry> = legacy
        .entries
        .into_iter()
        .map(|(key, entry)| {
            let legacy_had_events = eventful.contains(&key);
            (
                key,
                Entry {
                    mtime: entry.mtime,
                    size: entry.size,
                    identity: None,
                    msgs: entry.msgs.into_iter().map(LegacyCMsgV37::current).collect(),
                    event_keys: Vec::new(),
                    legacy_had_events,
                    legacy_needs_reparse: true,
                },
            )
        })
        .collect();
    // Always adapters with event-only history had no v8 message snapshot Entry. Intake still
    // provides an agent-level sentinel so an unavailable migration cannot erase those events.
    #[cfg(not(windows))]
    for key in eventful {
        if key.starts_with("\0snapshot\0") {
            entries.entry(key).or_insert(Entry {
                mtime: 0,
                size: 0,
                identity: None,
                msgs: Vec::new(),
                event_keys: Vec::new(),
                legacy_had_events: true,
                legacy_needs_reparse: true,
            });
        }
    }
    #[cfg(not(windows))]
    Ok((entries, CacheGeneration::LegacyReparse, false))
}

/// Exact v8 wire layout for one-time migration. Bincode is positional, so serde defaults cannot
/// safely add fields; deserialize the old shape explicitly and force every entry through parse.
#[derive(Deserialize, Serialize)]
#[cfg(not(windows))]
struct LegacyEntryV8 {
    mtime: i64,
    size: u64,
    msgs: Vec<LegacyCMsgV37>,
}

#[derive(Deserialize, Serialize)]
#[cfg(not(windows))]
struct LegacyCacheFileV8 {
    version: u32,
    entries: HashMap<String, LegacyEntryV8>,
}

fn read_u32(bytes: &[u8], offset: usize) -> Option<u32> {
    Some(u32::from_le_bytes(
        bytes.get(offset..offset + 4)?.try_into().ok()?,
    ))
}

fn read_u64(bytes: &[u8], offset: usize) -> Option<u64> {
    Some(u64::from_le_bytes(
        bytes.get(offset..offset + 8)?.try_into().ok()?,
    ))
}

fn read_digest(bytes: &[u8], offset: usize) -> Option<[u8; 16]> {
    bytes.get(offset..offset + 16)?.try_into().ok()
}

fn cache_digest(bytes: &[u8]) -> [u8; 16] {
    let leaves: Vec<[u8; 16]> = if bytes.len() <= CACHE_DIGEST_CHUNK {
        vec![md5::compute(bytes).0]
    } else {
        bytes
            .par_chunks(CACHE_DIGEST_CHUNK)
            .map(|chunk| md5::compute(chunk).0)
            .collect()
    };
    let mut root = Vec::with_capacity(32 + leaves.len() * 16);
    root.extend_from_slice(b"agrep-cache-digest-v2\0");
    // Total length plus the fixed partition makes the final leaf length implicit.
    root.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
    root.extend_from_slice(&(leaves.len() as u64).to_le_bytes());
    for leaf in leaves {
        root.extend_from_slice(&leaf);
    }
    md5::compute(root).0
}

fn new_cache_instance() -> [u8; 16] {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let mut seed = [0_u8; 28];
    seed[..16].copy_from_slice(&nanos.to_le_bytes());
    seed[16..20].copy_from_slice(&std::process::id().to_le_bytes());
    seed[20..].copy_from_slice(
        &NEXT
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            .to_le_bytes(),
    );
    md5::compute(seed).0
}

fn base_writer_build_id(bytes: &[u8]) -> Result<Option<WriterBuildId>, CacheDecodeRefusal> {
    if bytes.len() < 24
        || read_u64(bytes, 4) != Some(0)
        || bytes.get(12..20) != Some(CACHE_BASE_MAGIC)
    {
        return Err(CacheDecodeRefusal::BaseHeader);
    }
    let storage_version = read_u32(bytes, 20).ok_or(CacheDecodeRefusal::BaseHeader)?;
    match storage_version {
        CACHE_BASE_STORAGE_VERSION_V2 | CACHE_BASE_STORAGE_VERSION_V3 => Ok(None),
        CACHE_BASE_STORAGE_VERSION.. => {
            let id = bytes
                .get(BASE_OWNER_OFFSET..BASE_OWNER_OFFSET + WRITER_BUILD_ID_LEN)
                .and_then(WriterBuildId::parse)
                .ok_or(CacheDecodeRefusal::BaseHeader)?;
            Ok(Some(id))
        }
        _ => Ok(None),
    }
}

fn parse_base_header(bytes: &[u8]) -> Result<BaseHeader, CacheDecodeRefusal> {
    let semantic_version = read_u32(bytes, 0).ok_or(CacheDecodeRefusal::BaseHeader)?;
    let writer_build_id = base_writer_build_id(bytes)?;
    if semantic_version != CACHE_VERSION && !reparse_compatible_cache_version(semantic_version) {
        return Err(CacheDecodeRefusal::StorageVersion);
    }
    let storage_version = read_u32(bytes, 20).ok_or(CacheDecodeRefusal::BaseHeader)?;
    let (instance, raw_len, payload_digest, header_len, codec, stored_len) = match storage_version {
        CACHE_BASE_STORAGE_VERSION_V2 => {
            if bytes.len() < BASE_HEADER_V2_LEN {
                return Err(CacheDecodeRefusal::BaseHeader);
            }
            let instance = read_digest(bytes, 24).ok_or(CacheDecodeRefusal::BaseHeader)?;
            let raw_len = read_u64(bytes, 40).ok_or(CacheDecodeRefusal::BaseHeader)?;
            let payload_digest = read_digest(bytes, 48).ok_or(CacheDecodeRefusal::BaseHeader)?;
            (
                instance,
                raw_len,
                payload_digest,
                BASE_HEADER_V2_LEN,
                CACHE_CODEC_NONE,
                raw_len,
            )
        }
        CACHE_BASE_STORAGE_VERSION_V3 => {
            if bytes.len() < BASE_HEADER_V3_LEN || read_u32(bytes, 68) != Some(0) {
                return Err(CacheDecodeRefusal::BaseHeader);
            }
            (
                read_digest(bytes, 24).ok_or(CacheDecodeRefusal::BaseHeader)?,
                read_u64(bytes, 40).ok_or(CacheDecodeRefusal::BaseHeader)?,
                read_digest(bytes, 48).ok_or(CacheDecodeRefusal::BaseHeader)?,
                BASE_HEADER_V3_LEN,
                read_u32(bytes, 64).ok_or(CacheDecodeRefusal::BaseHeader)?,
                read_u64(bytes, 72).ok_or(CacheDecodeRefusal::BaseHeader)?,
            )
        }
        CACHE_BASE_STORAGE_VERSION => {
            if bytes.len() < BASE_HEADER_LEN || read_u32(bytes, BASE_V4_RESERVED_OFFSET) != Some(0)
            {
                return Err(CacheDecodeRefusal::BaseHeader);
            }
            (
                read_digest(bytes, BASE_V4_INSTANCE_OFFSET)
                    .ok_or(CacheDecodeRefusal::BaseHeader)?,
                read_u64(bytes, BASE_V4_RAW_LEN_OFFSET).ok_or(CacheDecodeRefusal::BaseHeader)?,
                read_digest(bytes, BASE_V4_DIGEST_OFFSET).ok_or(CacheDecodeRefusal::BaseHeader)?,
                BASE_HEADER_LEN,
                read_u32(bytes, BASE_V4_CODEC_OFFSET).ok_or(CacheDecodeRefusal::BaseHeader)?,
                read_u64(bytes, BASE_V4_STORED_LEN_OFFSET).ok_or(CacheDecodeRefusal::BaseHeader)?,
            )
        }
        _ => return Err(CacheDecodeRefusal::StorageVersion),
    };
    if raw_len == 0
        || !matches!(codec, CACHE_CODEC_NONE | CACHE_CODEC_XPRESS)
        || (codec == CACHE_CODEC_NONE && raw_len != stored_len)
        || (codec == CACHE_CODEC_XPRESS
            && (stored_len == 0
                || stored_len >= raw_len
                || raw_len > MAX_COMPRESSED_CACHE_RAW_BYTES
                || raw_len
                    > stored_len
                        .saturating_mul(MAX_COMPRESSED_CACHE_EXPANSION)
                        .saturating_add(MAX_COMPRESSED_CACHE_SLACK)))
    {
        return Err(CacheDecodeRefusal::BaseHeader);
    }
    Ok(BaseHeader {
        storage_version,
        writer_build_id,
        instance,
        raw_len,
        stored_len,
        codec,
        payload_digest,
        header_len,
    })
}

#[cfg(windows)]
fn compress_cache_payload(payload: &[u8]) -> Option<Vec<u8>> {
    use windows_sys::Win32::Storage::Compression::{
        CloseCompressor, Compress, CreateCompressor, ResetCompressor, COMPRESS_ALGORITHM_XPRESS,
    };

    let mut handle = std::ptr::null_mut();
    if unsafe { CreateCompressor(COMPRESS_ALGORITHM_XPRESS, std::ptr::null(), &mut handle) } == 0 {
        return None;
    }
    let result = (|| {
        let mut needed = 0usize;
        unsafe {
            Compress(
                handle,
                payload.as_ptr().cast(),
                payload.len(),
                std::ptr::null_mut(),
                0,
                &mut needed,
            );
        }
        if needed == 0 || unsafe { ResetCompressor(handle) } == 0 {
            return None;
        }
        let mut compressed = vec![0_u8; needed];
        if unsafe {
            Compress(
                handle,
                payload.as_ptr().cast(),
                payload.len(),
                compressed.as_mut_ptr().cast(),
                compressed.len(),
                &mut needed,
            )
        } == 0
        {
            return None;
        }
        compressed.truncate(needed);
        (compressed.len() < payload.len()).then_some(compressed)
    })();
    unsafe { CloseCompressor(handle) };
    result
}

#[cfg(windows)]
fn decompress_cache_payload(payload: &[u8], raw_len: usize) -> Option<Vec<u8>> {
    use windows_sys::Win32::Storage::Compression::{
        CloseDecompressor, CreateDecompressor, Decompress, COMPRESS_ALGORITHM_XPRESS,
    };

    let mut handle = std::ptr::null_mut();
    if unsafe { CreateDecompressor(COMPRESS_ALGORITHM_XPRESS, std::ptr::null(), &mut handle) } == 0
    {
        return None;
    }
    let result = (|| {
        let mut restored = Vec::new();
        restored.try_reserve_exact(raw_len).ok()?;
        restored.resize(raw_len, 0);
        let mut actual = 0usize;
        if unsafe {
            Decompress(
                handle,
                payload.as_ptr().cast(),
                payload.len(),
                restored.as_mut_ptr().cast(),
                restored.len(),
                &mut actual,
            )
        } == 0
            || actual != raw_len
        {
            return None;
        }
        Some(restored)
    })();
    unsafe { CloseDecompressor(handle) };
    result
}

fn stored_cache_payload(payload: Vec<u8>) -> (u32, Vec<u8>) {
    #[cfg(windows)]
    if let Some(compressed) = compress_cache_payload(&payload) {
        let raw_len = payload.len() as u64;
        let stored_len = compressed.len() as u64;
        if raw_len <= MAX_COMPRESSED_CACHE_RAW_BYTES
            && raw_len
                <= stored_len
                    .saturating_mul(MAX_COMPRESSED_CACHE_EXPANSION)
                    .saturating_add(MAX_COMPRESSED_CACHE_SLACK)
        {
            return (CACHE_CODEC_XPRESS, compressed);
        }
    }
    (CACHE_CODEC_NONE, payload)
}

fn encode_cache_base(entries: &HashMap<String, Entry>) -> anyhow::Result<Vec<u8>> {
    encode_cache_base_for(entries, WriterBuildId::current())
}

fn encode_cache_base_for(
    entries: &HashMap<String, Entry>,
    writer_build_id: WriterBuildId,
) -> anyhow::Result<Vec<u8>> {
    let started = std::time::Instant::now();
    let payload = bincode::serialize(&CacheFileRef {
        version: CACHE_VERSION,
        entries,
    })?;
    let serialized = started.elapsed();
    let digest = cache_digest(&payload);
    let digested = started.elapsed();
    let raw_len = payload.len();
    let (codec, stored) = stored_cache_payload(payload);
    let stored_done = started.elapsed();
    let mut bytes = Vec::with_capacity(BASE_HEADER_LEN + stored.len());
    bytes.extend_from_slice(&CACHE_VERSION.to_le_bytes());
    bytes.extend_from_slice(&0_u64.to_le_bytes());
    bytes.extend_from_slice(CACHE_BASE_MAGIC);
    bytes.extend_from_slice(&CACHE_BASE_STORAGE_VERSION.to_le_bytes());
    bytes.extend_from_slice(&writer_build_id.0);
    bytes.extend_from_slice(&new_cache_instance());
    bytes.extend_from_slice(&(raw_len as u64).to_le_bytes());
    bytes.extend_from_slice(&digest);
    bytes.extend_from_slice(&codec.to_le_bytes());
    bytes.extend_from_slice(&0_u32.to_le_bytes());
    bytes.extend_from_slice(&(stored.len() as u64).to_le_bytes());
    bytes.extend_from_slice(&stored);
    if std::env::var_os("AGREP_DEBUG").is_some() {
        eprintln!(
            "* [agrep cache] base serialize {:.1}ms · digest {:.1}ms · store {:.1}ms · assemble {:.1}ms · {:.1}/{:.1}MiB",
            serialized.as_secs_f64() * 1000.0,
            (digested - serialized).as_secs_f64() * 1000.0,
            (stored_done - digested).as_secs_f64() * 1000.0,
            (started.elapsed() - stored_done).as_secs_f64() * 1000.0,
            bytes.len() as f64 / (1024.0 * 1024.0),
            raw_len as f64 / (1024.0 * 1024.0),
        );
    }
    Ok(bytes)
}

fn decode_cache_base<'a>(
    path: &Path,
    bytes: &'a [u8],
) -> Result<DecodedCacheBase<'a>, CacheDecodeRefusal> {
    if bytes.get(12..20) == Some(CACHE_BASE_MAGIC) {
        let header = parse_base_header(bytes)?;
        let stored_len =
            usize::try_from(header.stored_len).map_err(|_| CacheDecodeRefusal::BaseHeader)?;
        let payload_end = header
            .header_len
            .checked_add(stored_len)
            .ok_or(CacheDecodeRefusal::BaseHeader)?;
        if payload_end != bytes.len() {
            return Err(CacheDecodeRefusal::BaseLengthMismatch);
        }
        let stored = &bytes[header.header_len..payload_end];
        let payload = match header.codec {
            CACHE_CODEC_NONE => Cow::Borrowed(stored),
            #[cfg(windows)]
            CACHE_CODEC_XPRESS => Cow::Owned(
                decompress_cache_payload(
                    stored,
                    usize::try_from(header.raw_len).map_err(|_| CacheDecodeRefusal::BaseHeader)?,
                )
                .ok_or(CacheDecodeRefusal::PayloadShape)?,
            ),
            _ => return Err(CacheDecodeRefusal::BaseHeader),
        };
        if payload.len() as u64 != header.raw_len
            || cache_digest(payload.as_ref()) != header.payload_digest
        {
            return Err(CacheDecodeRefusal::PayloadDigestMismatch);
        }
        let metadata = regular_metadata(path).map_err(|_| CacheDecodeRefusal::UnreadableFile)?;
        return Ok((
            payload,
            header.instance,
            BaseWitness::Wrapped {
                len: metadata.len(),
                modified: metadata.modified().ok(),
                header,
            },
        ));
    }
    let metadata = regular_metadata(path).map_err(|_| CacheDecodeRefusal::UnreadableFile)?;
    Ok((
        Cow::Borrowed(bytes),
        md5::compute(bytes).0,
        BaseWitness::Legacy {
            len: metadata.len(),
            modified: metadata.modified().ok(),
        },
    ))
}

fn cache_journal_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| "ingest-cache".into());
    path.with_file_name(format!("{name}.journal"))
}

#[cfg(test)]
thread_local! {
    /// Runs once between a decode's head read and its journal read, where a lock-free reader
    /// can see a commit land; tests stand a commit there.
    static BETWEEN_HEAD_AND_JOURNAL: std::cell::RefCell<Option<Box<dyn FnOnce()>>> =
        const { std::cell::RefCell::new(None) };
}

/// The journal position a cache commit established: its base instance and the frame sequence.
/// Recorded beside the cache; a decode that replays to an earlier position has lost frames.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CacheHead {
    instance: [u8; 16],
    sequence: u64,
}

impl CacheHead {
    /// The journal frame sequence this commit established; 0 after a base rewrite.
    pub fn sequence(self) -> u64 {
        self.sequence
    }

    fn encode(self) -> String {
        format!("{}:{}\n", hex16(&self.instance), self.sequence)
    }

    fn parse(text: &[u8]) -> Option<Self> {
        let text = std::str::from_utf8(text).ok()?;
        let (instance, sequence) = text.trim().split_once(':')?;
        Some(CacheHead {
            instance: parse_hex16(instance)?,
            sequence: sequence.parse().ok()?,
        })
    }
}

const CACHE_HEAD_MAX_BYTES: usize = 64;

/// The sidecar naming the head of the last commit; a lost journal leaves the base behind it.
pub fn cache_head_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| "ingest-cache".into());
    path.with_file_name(format!("{name}.head"))
}

fn read_cache_head(path: &Path) -> Option<CacheHead> {
    read_regular_prefix(&cache_head_path(path), CACHE_HEAD_MAX_BYTES)
        .ok()
        .flatten()
        .and_then(|bytes| CacheHead::parse(&bytes))
}

/// Record the head a commit established. The head is the evidence that dates the base, so a
/// write that fails after the cache commit fails the pass naming the path: a base without its
/// head would be adopted as whole by the next decode, exactly what the head exists to prevent.
fn write_cache_head(path: &Path, head: CacheHead) -> anyhow::Result<()> {
    let sidecar = cache_head_path(path);
    displace_sidecar_directory(&sidecar, "head")?;
    crate::cache::write_bytes_atomic(&sidecar, head.encode().as_bytes())
        .map_err(|error| anyhow::anyhow!("cannot write {}: {error}", sidecar.display()))
}

fn encode_journal_header(instance: [u8; 16]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(JOURNAL_HEADER_LEN);
    bytes.extend_from_slice(CACHE_JOURNAL_MAGIC);
    bytes.extend_from_slice(&CACHE_STORAGE_VERSION.to_le_bytes());
    bytes.extend_from_slice(&instance);
    bytes
}

#[cfg(test)]
thread_local! {
    /// 0 = use `MAX_FRAME_BYTES`; tests set a small limit on their own thread to
    /// exercise the oversized-delta base-rewrite fallback without materializing
    /// a real half-gigabyte delta.
    static TEST_MAX_FRAME_BYTES: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

fn max_frame_bytes() -> u64 {
    #[cfg(test)]
    {
        let limit = TEST_MAX_FRAME_BYTES.with(std::cell::Cell::get);
        if limit != 0 {
            return limit;
        }
    }
    MAX_FRAME_BYTES
}

/// Encode a journal frame, or `None` when the serialized delta exceeds the frame
/// limit. A delta that large (e.g. the first publication after a derived-store
/// adoption, which re-upserts the whole corpus against a near-empty base) can
/// never fit one frame no matter how often the writer retries, so the caller
/// must rewrite the cache base instead of erroring forever.
fn encode_bounded_journal_frame(
    from: u64,
    to: u64,
    delta: &JournalDelta,
) -> anyhow::Result<Option<Vec<u8>>> {
    let payload = bincode::serialize(delta)?;
    if payload.len() as u64 > max_frame_bytes() {
        return Ok(None);
    }
    let digest = cache_digest(&payload);
    let mut bytes = Vec::with_capacity(FRAME_HEADER_LEN + payload.len() + FRAME_FOOTER_LEN);
    bytes.extend_from_slice(CACHE_FRAME_MAGIC);
    bytes.extend_from_slice(&from.to_le_bytes());
    bytes.extend_from_slice(&to.to_le_bytes());
    bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
    bytes.extend_from_slice(&digest);
    bytes.extend_from_slice(&payload);
    bytes.extend_from_slice(CACHE_COMMIT_MAGIC);
    bytes.extend_from_slice(&to.to_le_bytes());
    bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
    bytes.extend_from_slice(&digest);
    Ok(Some(bytes))
}

/// Test-only frame builder keeping the size invariant as a hard error. Production
/// append paths go through [`encode_bounded_journal_frame`] instead, whose `None`
/// routes an oversized delta to a base rewrite rather than failing the save.
#[cfg(test)]
fn encode_journal_frame(from: u64, to: u64, delta: &JournalDelta) -> anyhow::Result<Vec<u8>> {
    encode_bounded_journal_frame(from, to, delta)?
        .ok_or_else(|| anyhow::anyhow!("ingest cache delta is too large"))
}

fn compact_size_bound(
    entries: &HashMap<String, Entry>,
    upserts: &HashSet<String>,
    deletes: &HashSet<String>,
) -> usize {
    let mut size = 64_usize;
    for key in deletes {
        size = size.saturating_add(key.len() + 16);
    }
    for key in upserts {
        size = size.saturating_add(key.len() + 96);
        let Some(entry) = entries.get(key) else {
            continue;
        };
        for message in &entry.msgs {
            size = size
                .saturating_add(128)
                .saturating_add(message.agent.len())
                .saturating_add(message.project.len())
                .saturating_add(message.session.len())
                .saturating_add(message.text.len())
                .saturating_add(message.who.len())
                .saturating_add(message.model.len())
                .saturating_add(message.model_source.len())
                .saturating_add(message.reply.len())
                .saturating_add(message.parent.len());
            if size > JOURNAL_COMPACT_BUDGET {
                return size;
            }
        }
        for event in &entry.event_keys {
            size = size
                .saturating_add(48)
                .saturating_add(event.agent.len())
                .saturating_add(event.session.len());
        }
        if size > JOURNAL_COMPACT_BUDGET {
            return size;
        }
    }
    size
}

fn regular_metadata(path: &Path) -> std::io::Result<fs::Metadata> {
    let metadata = fs::symlink_metadata(path)?;
    if crate::ingest::registry::metadata_is_link(&metadata) || !metadata.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "ingest cache path is not a regular file",
        ));
    }
    Ok(metadata)
}

#[cfg(unix)]
fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(windows)]
fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    left.creation_time() == right.creation_time()
        && left.last_write_time() == right.last_write_time()
        && left.file_size() == right.file_size()
        && left.file_attributes() == right.file_attributes()
}

#[cfg(not(any(unix, windows)))]
fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.len() == right.len() && left.modified().ok() == right.modified().ok()
}

fn same_snapshot(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    same_file(left, right)
        && left.len() == right.len()
        && left.modified().ok() == right.modified().ok()
}

#[cfg(not(windows))]
fn open_cache_read(path: &Path) -> std::io::Result<File> {
    File::open(path)
}

#[cfg(windows)]
fn open_cache_read(path: &Path) -> std::io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
}

#[cfg(not(windows))]
fn open_cache_read_write(path: &Path) -> std::io::Result<File> {
    OpenOptions::new().read(true).write(true).open(path)
}

#[cfg(windows)]
fn open_cache_read_write(path: &Path) -> std::io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    OpenOptions::new()
        .read(true)
        .write(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
}

fn open_regular_read(path: &Path) -> std::io::Result<(File, fs::Metadata)> {
    let before = regular_metadata(path)?;
    let file = open_cache_read(path)?;
    let opened = file.metadata()?;
    let after = regular_metadata(path)?;
    if crate::ingest::registry::metadata_is_link(&opened)
        || !opened.is_file()
        || !same_file(&before, &after)
        || !same_file(&opened, &after)
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "ingest cache path changed while opening",
        ));
    }
    Ok((file, after))
}

fn read_regular_file(path: &Path) -> std::io::Result<Option<Vec<u8>>> {
    let (mut file, before) = match open_regular_read(path) {
        Ok(opened) => opened,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut bytes = Vec::with_capacity(before.len().min(usize::MAX as u64) as usize);
    file.read_to_end(&mut bytes)?;
    let after_read = regular_metadata(path)?;
    if !same_snapshot(&before, &after_read) || bytes.len() as u64 != after_read.len() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "ingest cache path changed while reading",
        ));
    }
    Ok(Some(bytes))
}

fn read_regular_prefix(path: &Path, limit: usize) -> std::io::Result<Option<Vec<u8>>> {
    let (mut file, before) = match open_regular_read(path) {
        Ok(opened) => opened,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut bytes = Vec::with_capacity(limit.min(before.len().min(usize::MAX as u64) as usize));
    (&mut file).take(limit as u64).read_to_end(&mut bytes)?;
    let after_read = regular_metadata(path)?;
    if !same_snapshot(&before, &after_read) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "ingest cache path changed while probing",
        ));
    }
    Ok(Some(bytes))
}

fn probe_cache_owner_bytes(bytes: &[u8], current: WriterBuildId) -> CacheOwnerProbe {
    if bytes.get(12..20) != Some(CACHE_BASE_MAGIC) {
        return CacheOwnerProbe::LegacyUnowned;
    }
    let Some(storage_version) = read_u32(bytes, 20) else {
        return CacheOwnerProbe::Malformed;
    };
    if matches!(
        storage_version,
        CACHE_BASE_STORAGE_VERSION_V2 | CACHE_BASE_STORAGE_VERSION_V3
    ) {
        return CacheOwnerProbe::LegacyUnowned;
    }
    if storage_version < CACHE_BASE_STORAGE_VERSION {
        return CacheOwnerProbe::Malformed;
    }
    let Some(owner) = bytes
        .get(BASE_OWNER_OFFSET..BASE_OWNER_OFFSET + WRITER_BUILD_ID_LEN)
        .and_then(WriterBuildId::parse)
    else {
        return CacheOwnerProbe::Malformed;
    };
    if owner == current {
        CacheOwnerProbe::Current {
            build_id: owner.as_str().to_owned(),
        }
    } else {
        CacheOwnerProbe::Foreign {
            build_id: owner.as_str().to_owned(),
        }
    }
}

fn probe_cache_owner_for(path: &Path, current: WriterBuildId) -> CacheOwnerProbe {
    match read_regular_prefix(path, BASE_HEADER_LEN) {
        Ok(Some(bytes)) => probe_cache_owner_bytes(&bytes, current),
        Ok(None) => CacheOwnerProbe::Missing,
        Err(_) => CacheOwnerProbe::Unreadable,
    }
}

/// Probe a parse-cache owner without decoding its format or reading its payload.
pub fn probe_cache_owner(path: &Path) -> CacheOwnerProbe {
    probe_cache_owner_for(path, WriterBuildId::current())
}

fn open_regular_journal(path: &Path) -> std::io::Result<File> {
    let before = regular_metadata(path)?;
    let file = open_cache_read_write(path)?;
    let opened = file.metadata()?;
    let after = regular_metadata(path)?;
    if crate::ingest::registry::metadata_is_link(&opened)
        || !opened.is_file()
        || !same_file(&before, &after)
        || !same_file(&opened, &after)
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "ingest cache journal changed while opening",
        ));
    }
    Ok(file)
}

/// What replaying the journal found: its frames, or no readable journal at all.
enum JournalReplay {
    Replayed(JournalCursor),
    /// The journal could not be read (denied, a link or directory in its place, torn mid-read):
    /// persistent under the index lock, a race without it.
    Unreadable(JournalCursor),
}

fn replay_journal(
    path: &Path,
    instance: [u8; 16],
    entries: &mut HashMap<String, Entry>,
) -> JournalReplay {
    replay_journal_frames(path, instance, |payload, cursor| {
        let delta = bincode::deserialize::<JournalDelta>(payload).ok()?;
        apply_delta(entries, cursor, delta.upserts, delta.deletes);
        Some(())
    })
}

fn apply_delta(
    entries: &mut HashMap<String, Entry>,
    cursor: &mut JournalCursor,
    upserts: Vec<(String, Entry)>,
    deletes: Vec<String>,
) {
    for (key, entry) in upserts {
        entries.insert(key.clone(), entry);
        cursor.upserts.insert(key.clone());
        cursor.deletes.remove(&key);
    }
    for key in deletes {
        entries.remove(&key);
        cursor.upserts.remove(&key);
        cursor.deletes.insert(key);
    }
}

/// Walk the journal's verified frames, handing each payload to `apply` (None: undecodable, the
/// walk stops there) with the cursor it advances; an unreadable journal yields no frame.
fn replay_journal_frames(
    path: &Path,
    instance: [u8; 16],
    mut apply: impl FnMut(&[u8], &mut JournalCursor) -> Option<()>,
) -> JournalReplay {
    let bytes = match read_regular_file(path) {
        Ok(Some(bytes)) => bytes,
        Ok(None) => Vec::new(),
        Err(_) => {
            let observed_len = fs::symlink_metadata(path)
                .map(|metadata| metadata.len())
                .unwrap_or(0);
            return JournalReplay::Unreadable(JournalCursor {
                sequence: 0,
                observed_len,
                valid_len: 0,
                reset: true,
                frames: 0,
                upserts: HashSet::new(),
                deletes: HashSet::new(),
            });
        }
    };
    if bytes.is_empty() {
        return JournalReplay::Replayed(JournalCursor {
            sequence: 0,
            observed_len: 0,
            valid_len: 0,
            reset: true,
            frames: 0,
            upserts: HashSet::new(),
            deletes: HashSet::new(),
        });
    };
    let observed_len = bytes.len() as u64;
    if bytes.len() < JOURNAL_HEADER_LEN
        || !bytes.starts_with(CACHE_JOURNAL_MAGIC)
        || read_u32(&bytes, 8) != Some(CACHE_STORAGE_VERSION)
        || read_digest(&bytes, 12) != Some(instance)
    {
        return JournalReplay::Replayed(JournalCursor {
            sequence: 0,
            observed_len,
            valid_len: 0,
            reset: true,
            frames: 0,
            upserts: HashSet::new(),
            deletes: HashSet::new(),
        });
    }

    let mut cursor = JournalCursor {
        sequence: 0,
        observed_len,
        valid_len: JOURNAL_HEADER_LEN as u64,
        reset: false,
        frames: 0,
        upserts: HashSet::new(),
        deletes: HashSet::new(),
    };
    let mut offset = JOURNAL_HEADER_LEN;
    while offset < bytes.len() {
        let start = offset;
        let Some(header_end) = start.checked_add(FRAME_HEADER_LEN) else {
            break;
        };
        if header_end > bytes.len() || bytes.get(start..start + 8) != Some(CACHE_FRAME_MAGIC) {
            break;
        }
        let Some(from) = read_u64(&bytes, start + 8) else {
            break;
        };
        let Some(to) = read_u64(&bytes, start + 16) else {
            break;
        };
        let Some(payload_len_u64) = read_u64(&bytes, start + 24) else {
            break;
        };
        let Some(digest) = read_digest(&bytes, start + 32) else {
            break;
        };
        // A compacted frame carries the sequence it folded forward, so `to` may lead by more than one.
        if payload_len_u64 > MAX_FRAME_BYTES || from != cursor.sequence || to <= from {
            break;
        }
        let Ok(payload_len) = usize::try_from(payload_len_u64) else {
            break;
        };
        let Some(payload_end) = header_end.checked_add(payload_len) else {
            break;
        };
        let Some(frame_end) = payload_end.checked_add(FRAME_FOOTER_LEN) else {
            break;
        };
        if frame_end > bytes.len()
            || bytes.get(payload_end..payload_end + 8) != Some(CACHE_COMMIT_MAGIC)
            || read_u64(&bytes, payload_end + 8) != Some(to)
            || read_u64(&bytes, payload_end + 16) != Some(payload_len_u64)
            || read_digest(&bytes, payload_end + 24) != Some(digest)
        {
            break;
        }
        let payload = &bytes[header_end..payload_end];
        if cache_digest(payload) != digest {
            break;
        }
        if apply(payload, &mut cursor).is_none() {
            break;
        }
        offset = frame_end;
        cursor.sequence = to;
        cursor.valid_len = offset as u64;
        cursor.frames += 1;
    }
    JournalReplay::Replayed(cursor)
}

/// Who decodes: a writer holds the index lock, so nothing it fails to read is a race, and a
/// journal it cannot read is as lost as one deleted; a lock-free reader may be mid-commit.
#[derive(Clone, Copy, Eq, PartialEq)]
enum DecodeLane {
    Writer,
    LockFree,
}

fn decode_cache_for(
    path: &Path,
    current: WriterBuildId,
) -> Result<(HashMap<String, Entry>, CacheGeneration, CacheBacking), CacheDecodeRefusal> {
    decode_cache_owned(path, current, false, DecodeLane::Writer)
}

fn decode_cache_owned(
    path: &Path,
    current: WriterBuildId,
    adopt_foreign: bool,
    lane: DecodeLane,
) -> Result<(HashMap<String, Entry>, CacheGeneration, CacheBacking), CacheDecodeRefusal> {
    let bytes = match read_regular_file(path) {
        Ok(Some(bytes)) => bytes,
        Ok(None) => return Err(CacheDecodeRefusal::MissingFile),
        Err(_) => return Err(CacheDecodeRefusal::UnreadableFile),
    };
    if bytes.get(12..20) == Some(CACHE_BASE_MAGIC) {
        if let Some(owner) = base_writer_build_id(&bytes)? {
            if owner != current && !adopt_foreign {
                return Err(CacheDecodeRefusal::ForeignOwner);
            }
        }
    }
    let (payload, instance, witness) = decode_cache_base(path, &bytes)?;
    let (mut entries, generation, current_wire) = decode_cache_payload(payload.as_ref(), path)?;
    if !current_wire {
        // the generation's own journal, in its own layout: its last-good rows retain too
        if read_u32(payload.as_ref(), 0).is_some_and(reparse_compatible_cache_version) {
            replay_journal_frames(&cache_journal_path(path), instance, |payload, cursor| {
                let delta = bincode::deserialize::<LegacyJournalDeltaV37>(payload).ok()?;
                apply_delta(
                    &mut entries,
                    cursor,
                    delta
                        .upserts
                        .into_iter()
                        .map(|(key, entry)| (key, entry.into_entry()))
                        .collect(),
                    delta.deletes,
                );
                Some(())
            });
        }
        return Ok((entries, generation, CacheBacking::Rewrite));
    }
    // The head first: a commit lands its frames before it records the head, so a head read
    // before the journal never exceeds the frames that journal holds, lock or no lock.
    let head = read_cache_head(path);
    #[cfg(test)]
    if let Some(hook) = BETWEEN_HEAD_AND_JOURNAL.with(|hook| hook.borrow_mut().take()) {
        hook();
    }
    let replay = replay_journal(&cache_journal_path(path), instance, &mut entries);
    // Frames the last commit established are gone: this base is older than what later passes
    // may have published from it, so it vouches for nothing. A writer holds the lock, so a
    // journal it cannot read holds no frame it can trust: lost, exactly as if deleted.
    let (cursor, judged) = match replay {
        JournalReplay::Replayed(cursor) => (cursor, true),
        JournalReplay::Unreadable(cursor) => (cursor, lane == DecodeLane::Writer),
    };
    if judged
        && head.is_some_and(|head| head.instance == instance && cursor.sequence < head.sequence)
    {
        return Err(CacheDecodeRefusal::StaleJournal);
    }
    if read_u32(payload.as_ref(), 0).is_some_and(reparse_compatible_cache_version) {
        for key in &cursor.upserts {
            if let Some(entry) = entries.get_mut(key) {
                entry.legacy_needs_reparse = true;
            }
        }
    }
    let backing = if generation == CacheGeneration::Current {
        CacheBacking::Delta {
            instance,
            witness,
            cursor: Arc::new(Mutex::new(cursor)),
        }
    } else {
        CacheBacking::Rewrite
    };
    Ok((entries, generation, backing))
}

enum JournalWrite {
    Append,
    Replace,
}

struct BaseExpectation {
    instance: [u8; 16],
    witness: BaseWitness,
    cursor: Arc<Mutex<JournalCursor>>,
    sequence: u64,
    observed_len: u64,
    valid_len: u64,
    reset: bool,
    frames: u64,
}

enum StagedCacheKind {
    Noop {
        base: PathBuf,
        head: CacheHead,
    },
    Base {
        tmp: PathBuf,
        path: PathBuf,
        journal: PathBuf,
        instance: [u8; 16],
        expected: Option<BaseExpectation>,
    },
    Journal {
        tmp: PathBuf,
        base: PathBuf,
        journal: PathBuf,
        instance: [u8; 16],
        witness: BaseWitness,
        cursor: Arc<Mutex<JournalCursor>>,
        expected_sequence: u64,
        expected_observed_len: u64,
        expected_valid_len: u64,
        expected_reset: bool,
        expected_frames: u64,
        next_sequence: u64,
        mode: JournalWrite,
        upserts: HashSet<String>,
        deletes: HashSet<String>,
    },
}

/// A staged cache mutation advances only after its event proof has been revoked for recovery.
pub struct StagedCache {
    kind: StagedCacheKind,
    committed: bool,
}

fn staged_path(path: &Path) -> PathBuf {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

    let pid = std::process::id();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| "ingest-cache".into());
    let serial = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    path.with_file_name(format!("{name}.tmp.{pid}.{nanos}.{serial}"))
}

fn write_staged_bytes(writer: &mut impl Write, bytes: &[u8]) -> std::io::Result<()> {
    for chunk in bytes.chunks(STAGED_WRITE_CHUNK) {
        writer.write_all(chunk)?;
    }
    Ok(())
}

fn write_staged_file_with(
    path: &Path,
    write: impl FnOnce(&mut File) -> std::io::Result<()>,
) -> std::io::Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    if let Err(error) = write(&mut file) {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(error);
    }
    Ok(())
}

fn write_staged_file(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    // A single tens-of-MiB WriteFile call can serialize through Windows filter drivers.
    write_staged_file_with(path, |file| write_staged_bytes(file, bytes))?;
    Ok(())
}

fn base_matches(path: &Path, instance: [u8; 16], witness: &BaseWitness) -> bool {
    match witness {
        BaseWitness::Wrapped {
            len,
            modified,
            header: expected_header,
        } => {
            let Ok((mut file, metadata)) = open_regular_read(path) else {
                return false;
            };
            let mut buffer = [0_u8; BASE_HEADER_LEN];
            if file.read_exact(&mut buffer[..BASE_HEADER_V2_LEN]).is_err() {
                return false;
            }
            let header_len = match read_u32(&buffer, 20) {
                Some(CACHE_BASE_STORAGE_VERSION_V2) => BASE_HEADER_V2_LEN,
                Some(CACHE_BASE_STORAGE_VERSION_V3) => BASE_HEADER_V3_LEN,
                Some(CACHE_BASE_STORAGE_VERSION) => BASE_HEADER_LEN,
                _ => return false,
            };
            if file
                .read_exact(&mut buffer[BASE_HEADER_V2_LEN..header_len])
                .is_err()
            {
                return false;
            }
            let Ok(actual_header) = parse_base_header(&buffer[..header_len]) else {
                return false;
            };
            let Ok(after) = regular_metadata(path) else {
                return false;
            };
            same_snapshot(&metadata, &after)
                && metadata.len() == *len
                && metadata.modified().ok() == *modified
                && actual_header == *expected_header
                && actual_header.instance == instance
                && metadata.len() == actual_header.header_len as u64 + actual_header.stored_len
        }
        BaseWitness::Legacy { len, modified } => {
            let Ok(Some(bytes)) = read_regular_file(path) else {
                return false;
            };
            bytes.len() as u64 == *len
                && regular_metadata(path)
                    .is_ok_and(|metadata| metadata.modified().ok() == *modified)
                && md5::compute(bytes).0 == instance
        }
    }
}

fn base_expectation(
    instance: [u8; 16],
    witness: BaseWitness,
    cursor: &Arc<Mutex<JournalCursor>>,
) -> anyhow::Result<BaseExpectation> {
    let state = cursor
        .lock()
        .map_err(|_| anyhow::anyhow!("ingest cache journal state poisoned"))?;
    Ok(BaseExpectation {
        instance,
        witness,
        cursor: cursor.clone(),
        sequence: state.sequence,
        observed_len: state.observed_len,
        valid_len: state.valid_len,
        reset: state.reset,
        frames: state.frames,
    })
}

/// A directory where a cache sidecar belongs (no agrep build writes one) blocks every write
/// there. Move it aside under a name no staging reaper claims, never into it or through it; a
/// move that fails refuses the commit naming the path, so the user can clear it.
pub fn displace_sidecar_directory(sidecar: &Path, kind: &str) -> anyhow::Result<()> {
    let Ok(metadata) = fs::symlink_metadata(sidecar) else {
        return Ok(());
    };
    if !metadata.is_dir() {
        return Ok(());
    }
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let name = sidecar
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| format!("ingest-cache.{kind}").into());
    let aside = sidecar.with_file_name(format!("{name}.displaced-{nanos}"));
    fs::rename(sidecar, &aside).map_err(|error| {
        anyhow::anyhow!(
            "a directory stands where the parse cache {kind} belongs, {}, and could not be moved aside: {error}; move or remove it",
            sidecar.display()
        )
    })?;
    crate::emit::human_line(format_args!(
        "  a directory stood where the parse cache {kind} belongs; moved it aside to {}",
        crate::ingest::terminal_safe(aside.display())
    ));
    Ok(())
}

/// [`displace_sidecar_directory`] for the journal.
pub fn displace_journal_directory(journal: &Path) -> anyhow::Result<()> {
    displace_sidecar_directory(journal, "journal")
}

fn journal_cursor_matches(
    file: &mut File,
    instance: [u8; 16],
    sequence: u64,
    valid_len: u64,
) -> bool {
    let mut header = [0_u8; JOURNAL_HEADER_LEN];
    if file.seek(SeekFrom::Start(0)).is_err()
        || file.read_exact(&mut header).is_err()
        || !header.starts_with(CACHE_JOURNAL_MAGIC)
        || read_u32(&header, 8) != Some(CACHE_STORAGE_VERSION)
        || read_digest(&header, 12) != Some(instance)
    {
        return false;
    }
    if sequence == 0 {
        return valid_len == JOURNAL_HEADER_LEN as u64;
    }
    let Some(footer_at) = valid_len.checked_sub(FRAME_FOOTER_LEN as u64) else {
        return false;
    };
    let mut footer = [0_u8; FRAME_FOOTER_LEN];
    file.seek(SeekFrom::Start(footer_at)).is_ok()
        && file.read_exact(&mut footer).is_ok()
        && footer.starts_with(CACHE_COMMIT_MAGIC)
        && read_u64(&footer, 8) == Some(sequence)
}

impl StagedCache {
    /// Commit, then record the head established beside the cache: a later decode that replays
    /// short of it has lost frames and is refused rather than trusted as last-good. The head's
    /// path is cleared first: a head that cannot be written refuses before the base or journal
    /// moves, so no commit ever stands without the head that dates it.
    pub fn commit(mut self) -> anyhow::Result<CacheHead> {
        let _commit = CACHE_COMMIT_LOCK
            .lock()
            .map_err(|_| anyhow::anyhow!("ingest cache commit lock poisoned"))?;
        let base_path = match &self.kind {
            StagedCacheKind::Noop { base, .. } | StagedCacheKind::Journal { base, .. } => base,
            StagedCacheKind::Base { path, .. } => path,
        };
        displace_sidecar_directory(&cache_head_path(base_path), "head")?;
        let (base, head) = match &self.kind {
            StagedCacheKind::Noop { base, head } => (base.clone(), *head),
            StagedCacheKind::Base {
                tmp,
                path,
                journal,
                instance,
                expected,
            } => {
                let mut cursor_guard = None;
                if let Some(expected) = expected {
                    anyhow::ensure!(
                        base_matches(path, expected.instance, &expected.witness),
                        "ingest cache base changed before replacement"
                    );
                    let state = expected
                        .cursor
                        .lock()
                        .map_err(|_| anyhow::anyhow!("ingest cache journal state poisoned"))?;
                    anyhow::ensure!(
                        state.sequence == expected.sequence
                            && state.observed_len == expected.observed_len
                            && state.valid_len == expected.valid_len
                            && state.reset == expected.reset
                            && state.frames == expected.frames,
                        "stale ingest cache replacement"
                    );
                    let actual_len = fs::symlink_metadata(journal)
                        .map(|metadata| metadata.len())
                        .unwrap_or(0);
                    anyhow::ensure!(
                        actual_len == expected.observed_len,
                        "ingest cache journal changed before replacement"
                    );
                    cursor_guard = Some(state);
                }
                displace_journal_directory(journal)?;
                crate::cache::replace_file(tmp, path)?;
                let _ = fs::remove_file(journal);
                drop(cursor_guard);
                (
                    path.clone(),
                    CacheHead {
                        instance: *instance,
                        sequence: 0,
                    },
                )
            }
            StagedCacheKind::Journal {
                tmp,
                base,
                journal,
                instance,
                witness,
                cursor,
                expected_sequence,
                expected_observed_len,
                expected_valid_len,
                expected_reset,
                expected_frames,
                next_sequence,
                mode,
                upserts,
                deletes,
            } => {
                anyhow::ensure!(
                    base_matches(base, *instance, witness),
                    "ingest cache base changed before delta commit"
                );
                let mut state = cursor
                    .lock()
                    .map_err(|_| anyhow::anyhow!("ingest cache journal state poisoned"))?;
                anyhow::ensure!(
                    state.sequence == *expected_sequence
                        && state.observed_len == *expected_observed_len
                        && state.valid_len == *expected_valid_len
                        && state.reset == *expected_reset
                        && state.frames == *expected_frames,
                    "stale ingest cache delta"
                );
                let actual_len = fs::symlink_metadata(journal)
                    .map(|metadata| metadata.len())
                    .unwrap_or(0);
                anyhow::ensure!(
                    actual_len == *expected_observed_len,
                    "ingest cache journal changed before delta commit"
                );
                let new_len = match mode {
                    JournalWrite::Replace => {
                        displace_journal_directory(journal)?;
                        crate::cache::replace_file(tmp, journal)?;
                        fs::metadata(journal)?.len()
                    }
                    JournalWrite::Append => {
                        let mut live = open_regular_journal(journal)?;
                        anyhow::ensure!(
                            journal_cursor_matches(
                                &mut live,
                                *instance,
                                *expected_sequence,
                                *expected_valid_len,
                            ),
                            "ingest cache journal cursor changed before delta commit"
                        );
                        live.set_len(*expected_valid_len)?;
                        live.seek(SeekFrom::End(0))?;
                        let mut staged = File::open(tmp)?;
                        std::io::copy(&mut staged, &mut live)?;
                        live.metadata()?.len()
                    }
                };
                state.sequence = *next_sequence;
                state.observed_len = new_len;
                state.valid_len = new_len;
                state.reset = false;
                state.frames = match mode {
                    JournalWrite::Replace => 1,
                    JournalWrite::Append => expected_frames + 1,
                };
                match mode {
                    JournalWrite::Replace => {
                        state.upserts = upserts.clone();
                        state.deletes = deletes.clone();
                    }
                    JournalWrite::Append => {
                        for key in upserts {
                            state.upserts.insert(key.clone());
                            state.deletes.remove(key);
                        }
                        for key in deletes {
                            state.upserts.remove(key);
                            state.deletes.insert(key.clone());
                        }
                    }
                }
                let _ = fs::remove_file(tmp);
                (
                    base.clone(),
                    CacheHead {
                        instance: *instance,
                        sequence: *next_sequence,
                    },
                )
            }
        };
        self.committed = true;
        write_cache_head(&base, head)?;
        Ok(head)
    }
}

impl Drop for StagedCache {
    fn drop(&mut self) {
        if self.committed {
            return;
        }
        match &self.kind {
            StagedCacheKind::Base { tmp, .. } | StagedCacheKind::Journal { tmp, .. } => {
                let _ = fs::remove_file(tmp);
            }
            StagedCacheKind::Noop { .. } => {}
        }
    }
}

/// `(source path, agent, session)`: one cached file's claim on an indexed session.
pub type SessionSource = (PathBuf, &'static str, Arc<str>);

fn session_sources_of(entries: &HashMap<String, Entry>) -> Vec<SessionSource> {
    let mut out = Vec::new();
    for (key, entry) in entries {
        let Some(path) = source_path_from_key(key) else {
            continue;
        };
        let first = out.len();
        for msg in &entry.msgs {
            if out[first..]
                .iter()
                .any(|(_, _, session)| session == &msg.session)
            {
                continue;
            }
            out.push((path.clone(), intern_agent(&msg.agent), msg.session.clone()));
        }
    }
    out
}

/// The one read-only decode a diagnostic takes of the cache at `path`, accepting a foreign owner:
/// it describes what was published and never stages over another build's cache. Every view a
/// diagnostic needs comes from this single decode, so one pass can never mix two generations.
pub struct CacheView {
    entries: HashMap<String, Entry>,
}

impl CacheView {
    pub fn read(path: &Path) -> Result<Self, CacheDecodeRefusal> {
        // Without the index lock, a commit may land its frames between the head and journal reads
        // of one decode; one re-read sees both sides of it.
        let decode =
            || decode_cache_owned(path, WriterBuildId::current(), true, DecodeLane::LockFree);
        let mut decoded = decode();
        if matches!(decoded, Err(CacheDecodeRefusal::StaleJournal)) {
            decoded = decode();
        }
        let (entries, _generation, _backing) = decoded?;
        Ok(Self { entries })
    }

    /// [`IngestCache::session_sources`] of the decoded generation.
    pub fn session_sources(&self) -> Vec<SessionSource> {
        session_sources_of(&self.entries)
    }

    /// [`IngestCache::token_projects`] of the decoded generation.
    pub fn token_projects(&self, agent: &str) -> HashMap<String, String> {
        token_projects_of(&self.entries, agent)
    }

    /// The cache exactly as a writer holding the index lock decodes it: a journal it cannot read
    /// loses the frames past its head, so such a cache serves nothing, as a plain pass finds.
    pub fn read_locked(path: &Path) -> Result<Self, CacheDecodeRefusal> {
        let (entries, _generation, _backing) =
            decode_cache_owned(path, WriterBuildId::current(), false, DecodeLane::Writer)?;
        Ok(Self { entries })
    }

    /// Whether a warm pass serves the last good rows of `agent`'s material at `path` when it
    /// fails to read: a token conversation `session` from its own database's entry, or from one
    /// of a database no longer present (`live_namespaces` names those present), as the writer's
    /// moved-database rule does; any other source from its own path's entry.
    pub fn serves(
        &self,
        agent: &str,
        path: &Path,
        session: Option<&str>,
        live_namespaces: &HashSet<String>,
    ) -> bool {
        let holds = |entry: &Entry| {
            entry.msgs.iter().any(|msg| {
                msg.agent == agent && session.is_none_or(|session| &*msg.session == session)
            })
        };
        if !crate::ingest::registry::token_store_agent(agent) {
            return self.entries.iter().any(|(key, entry)| {
                source_path_from_key(key).is_some_and(|source| source == path) && holds(entry)
            });
        }
        let Some(session) = session else {
            return false;
        };
        let prefix = format!("\0tok\0{agent}\0");
        let own = crate::ingest::registry::token_prefix(agent, path).unwrap_or_default();
        self.entries.iter().any(|(key, entry)| {
            let Some(cache_id) = key.strip_prefix(&prefix) else {
                return false;
            };
            let namespace = crate::ingest::registry::token_namespace(agent, cache_id);
            (namespace == own || !live_namespaces.contains(&namespace))
                && (holds(entry) || entry.event_keys.iter().any(|event| &*event.session == session))
        })
    }
}

/// Per cache id of `agent`'s token conversations with cached rows, the project those rows carry:
/// the attribution the last-good generation published.
fn token_projects_of(entries: &HashMap<String, Entry>, agent: &str) -> HashMap<String, String> {
    let prefix = format!("\0tok\0{agent}\0");
    entries
        .iter()
        .filter_map(|(key, entry)| {
            let cache_id = key.strip_prefix(&prefix)?;
            let project = entry.msgs.first()?.project.to_string();
            Some((cache_id.to_owned(), project))
        })
        .collect()
}

impl IngestCache {
    /// Cold-mode field defaults; every load mode states only its deltas via struct update.
    fn base(entries: HashMap<String, Entry>, backing: CacheBacking) -> Self {
        IngestCache {
            entries: normalized_source_entries(entries),
            dirty: HashSet::new(),
            deleted: HashSet::new(),
            backing,
            write_blocked: false,
            warm: false,
            touched: HashSet::new(),
            event_touched: HashSet::new(),
            guarded_stale: false,
            guard_epoch: 0,
            output_incomplete: false,
            unreadable_unserved: false,
            admitted_unpublished_failure: false,
            unproven_token_failures: Vec::new(),
            settled_token_failures: HashSet::new(),
            failed_token_identities: HashMap::new(),
            force_reparse: false,
            repair_mode: false,
            allow_stable_deletions: false,
            repeated_absent_agents: HashSet::new(),
            repeated_absent_roots: Vec::new(),
            published_token_projects: HashMap::new(),
            racy_scan_start_ns: None,
            discarded_base: false,
            discarded_base_confirmed: false,
            #[cfg(test)]
            provisional_deletions: false,
            published_material: None,
            published_blind_scopes: HashSet::new(),
            published_from_cache: false,
            unpublished_agents: HashSet::new(),
            seeded_snapshots: HashSet::new(),
            unvouched_reads: HashSet::new(),
            published_rows: HashMap::new(),
            displaced: HashMap::new(),
            failed_scopes: Vec::new(),
            published_seal: None,
            released: HashMap::new(),
            published_token_namespaces: None,
            last_good_base: false,
            legacy_generation: false,
            repair_expected_agents: HashSet::new(),
            repair_expected_paths: HashSet::new(),
            current_snapshot_complete: false,
            current_source_agents: HashSet::new(),
            current_cleanly_absent_agents: HashSet::new(),
            coverage_expected_paths: HashSet::new(),
            coverage_expected_tokens: HashMap::new(),
            source_stamps: HashMap::new(),
            source_read_issues: Vec::new(),
            source_paths: std::sync::OnceLock::new(),
        }
    }

    fn foreign_owned() -> Self {
        IngestCache {
            write_blocked: true,
            ..Self::base(HashMap::new(), CacheBacking::Rewrite)
        }
    }

    fn put_entry(&mut self, key: String, entry: Entry) {
        let base = self.entries.insert(key.clone(), entry);
        if base.is_none() {
            self.source_paths.take();
        }
        self.displaced.entry(key.clone()).or_insert(base);
        self.deleted.remove(&key);
        self.dirty.insert(key);
    }

    /// Keeps an entry's rows but no identity, so its next read misses and reparses once.
    fn forget_identity(&mut self, key: &str) {
        if let Some(entry) = self.entries.get_mut(key) {
            if entry.identity.take().is_some() {
                self.dirty.insert(key.to_owned());
            }
        }
    }

    fn remove_entry(&mut self, key: &str) -> Option<Entry> {
        let entry = self.entries.remove(key)?;
        self.source_paths.take();
        if !self.displaced.contains_key(key) {
            self.displaced.insert(key.to_owned(), Some(entry.clone()));
        }
        self.dirty.remove(key);
        self.deleted.insert(key.to_string());
        Some(entry)
    }

    fn rename_entry(&mut self, old: &str, new: String) -> bool {
        let Some(entry) = self.remove_entry(old) else {
            return false;
        };
        self.put_entry(new, entry);
        true
    }

    fn update_entry<F>(&mut self, key: &str, update: F) -> bool
    where
        F: FnOnce(&mut Entry),
    {
        let Some(entry) = self.entries.get_mut(key) else {
            return false;
        };
        if !self.displaced.contains_key(key) {
            self.displaced.insert(key.to_owned(), Some(entry.clone()));
        }
        update(entry);
        self.deleted.remove(key);
        self.dirty.insert(key.to_string());
        true
    }

    fn remove_entries_where<F>(&mut self, mut remove: F)
    where
        F: FnMut(&str, &Entry) -> bool,
    {
        let keys: Vec<String> = self
            .entries
            .iter()
            .filter(|(key, entry)| remove(key, entry))
            .map(|(key, _entry)| key.clone())
            .collect();
        for key in keys {
            self.remove_entry(&key);
        }
    }

    /// Load the cache, or an empty one on absence / corruption / version mismatch.
    pub fn load(path: &Path) -> Self {
        Self::load_for_writer(path, WriterBuildId::current())
    }

    fn load_for_writer(path: &Path, writer_build_id: WriterBuildId) -> Self {
        match decode_cache_for(path, writer_build_id) {
            Ok((entries, generation, backing)) => IngestCache {
                warm: generation != CacheGeneration::LegacyReparse,
                force_reparse: generation != CacheGeneration::Current,
                last_good_base: true,
                legacy_generation: generation == CacheGeneration::LegacyReparse,
                ..Self::base(entries, backing)
            },
            Err(CacheDecodeRefusal::ForeignOwner) => Self::foreign_owned(),
            Err(refusal) => {
                if refusal == CacheDecodeRefusal::StaleJournal {
                    crate::emit::human_line(format_args!(
                        "  parse cache journal is behind its last commit; rebuilding the cache from sources"
                    ));
                }
                Self::base(HashMap::new(), CacheBacking::Rewrite)
            }
        }
    }

    /// An empty cache that treats every file as changed (used for `--full`).
    pub fn cold() -> Self {
        Self::base(HashMap::new(), CacheBacking::Rewrite)
    }

    /// Load last-good rows but force all cacheable sources to parse. Returns the decode refusal
    /// when no valid cache generation was available for fallback; callers use it to reject (and
    /// name) an ambiguous repair when both the cache and previously-published source snapshot
    /// are unavailable.
    pub fn repair(path: &Path) -> (Self, Option<CacheDecodeRefusal>) {
        Self::repair_for_writer(path, WriterBuildId::current())
    }

    fn repair_for_writer(
        path: &Path,
        writer_build_id: WriterBuildId,
    ) -> (Self, Option<CacheDecodeRefusal>) {
        match decode_cache_for(path, writer_build_id) {
            Ok((mut entries, generation, _backing)) => {
                // This pass rebuilds every entry's events; one it cannot re-read keeps the flag,
                // so its first good read rebuilds them rather than hitting an unchanged stamp.
                for entry in entries.values_mut() {
                    entry.legacy_needs_reparse = true;
                }
                (
                    IngestCache {
                        force_reparse: true,
                        repair_mode: true,
                        last_good_base: true,
                        legacy_generation: generation == CacheGeneration::LegacyReparse,
                        ..Self::base(entries, CacheBacking::Rewrite)
                    },
                    None,
                )
            }
            Err(CacheDecodeRefusal::ForeignOwner) => (
                IngestCache {
                    force_reparse: true,
                    repair_mode: true,
                    discarded_base: true,
                    ..Self::foreign_owned()
                },
                Some(CacheDecodeRefusal::ForeignOwner),
            ),
            Err(refusal) => (
                IngestCache {
                    force_reparse: true,
                    repair_mode: true,
                    discarded_base: true,
                    ..Self::base(HashMap::new(), CacheBacking::Rewrite)
                },
                Some(refusal),
            ),
        }
    }

    /// Load last-good rows with the automatic-repair guards enabled, but preserve ordinary
    /// cache hits. This is the retry mode for an ingest whose source moved during publication:
    /// only files whose Stat/Token identity changed are reparsed, while a transiently missing
    /// source is still rejected as guarded-stale. Event reconstruction uses [`Self::repair`]
    /// instead because event payloads are intentionally not stored in this cache and therefore
    /// require a complete source reparse.
    pub fn guarded_retry(path: &Path) -> (Self, Option<CacheDecodeRefusal>) {
        Self::guarded_retry_for_writer(path, WriterBuildId::current())
    }

    fn guarded_retry_for_writer(
        path: &Path,
        writer_build_id: WriterBuildId,
    ) -> (Self, Option<CacheDecodeRefusal>) {
        match decode_cache_for(path, writer_build_id) {
            Ok((entries, generation, backing)) => (
                IngestCache {
                    warm: generation != CacheGeneration::LegacyReparse,
                    // Semantic upgrades retain fallback rows but reparse as a complete generation.
                    force_reparse: generation != CacheGeneration::Current,
                    repair_mode: true,
                    allow_stable_deletions: true,
                    last_good_base: true,
                    legacy_generation: generation == CacheGeneration::LegacyReparse,
                    ..Self::base(entries, backing)
                },
                None,
            ),
            Err(CacheDecodeRefusal::ForeignOwner) => (
                IngestCache {
                    force_reparse: true,
                    repair_mode: true,
                    allow_stable_deletions: true,
                    discarded_base: true,
                    ..Self::foreign_owned()
                },
                Some(CacheDecodeRefusal::ForeignOwner),
            ),
            Err(refusal) => (
                IngestCache {
                    force_reparse: true,
                    repair_mode: true,
                    allow_stable_deletions: true,
                    discarded_base: true,
                    ..Self::base(HashMap::new(), CacheBacking::Rewrite)
                },
                Some(refusal),
            ),
        }
    }

    pub fn set_repair_expectations(&mut self, agents: HashSet<String>, paths: HashSet<PathBuf>) {
        self.repair_expected_agents = agents;
        self.repair_expected_paths = paths;
    }

    /// Whether a token store database at `scope` may still hold material a pass must not drop on one
    /// observation: this cache holds its conversations, or a snapshot this run inherits expects it.
    /// `--full` inherits only its own preflight, so a confirmed removal converges there at once.
    pub(crate) fn token_scope_expected(&self, agent: &str, scope: &Path) -> bool {
        self.holds_token_entries(agent, scope)
            || self
                .repair_expected_paths
                .iter()
                .any(|expected| source_path_eq(expected, scope))
    }

    pub fn set_source_coverage(
        &mut self,
        paths: HashSet<PathBuf>,
        tokens: HashMap<String, Vec<(String, String)>>,
    ) {
        self.coverage_expected_paths = paths;
        self.coverage_expected_tokens = tokens;
    }

    pub fn set_current_source_agents(&mut self, agents: HashSet<String>) {
        self.current_snapshot_complete = true;
        self.current_source_agents = agents;
    }

    pub fn set_current_source_snapshot(
        &mut self,
        snapshot: &crate::ingest::registry::SourceSnapshotView,
    ) {
        self.current_cleanly_absent_agents = snapshot.cleanly_absent_agents();
        if !snapshot.complete() {
            return;
        }
        let (agents, _) = snapshot.expectations();
        let (paths, tokens) = snapshot.coverage();
        self.current_snapshot_complete = true;
        self.current_source_agents = agents;
        self.coverage_expected_paths = paths;
        self.coverage_expected_tokens = tokens;
        self.source_stamps = snapshot.stat_stamps();
    }

    pub fn adapter_required(&self, agent: &str) -> bool {
        if !self.current_snapshot_complete
            || self.current_source_agents.contains(agent)
            || self.repair_expected_agents.contains(agent)
        {
            return true;
        }
        if self
            .entries
            .values()
            .any(|entry| entry.legacy_had_events || entry.legacy_needs_reparse)
        {
            return true;
        }
        self.entries.values().any(|entry| {
            entry.msgs.iter().any(|message| message.agent == agent)
                || entry.event_keys.iter().any(|event| event.agent == agent)
        })
    }

    pub fn preflight_source_paths(&self, root: &Path) -> Option<Vec<PathBuf>> {
        if !self.current_snapshot_complete {
            return None;
        }
        let mut paths: Vec<_> = self
            .coverage_expected_paths
            .iter()
            .filter(|path| source_path_within(path, root))
            .cloned()
            .collect();
        paths.sort_unstable();
        Some(paths)
    }

    /// Permit positive individual Stat/Token tombstones during a complete event repair. Exact
    /// preflight coverage and the adapter's second observation must agree on each absence.
    pub fn allow_validated_repair_deletions(&mut self) {
        debug_assert!(self.repair_mode);
        debug_assert!(self.force_reparse);
        debug_assert!(!self.warm);
        self.allow_stable_deletions = true;
    }

    /// Permit whole-store tombstones for agents whose clean absence (ENOENT, or a token store
    /// enumerating validly to zero) two consecutive complete observations agreed on. Per agent,
    /// not byte-identical snapshots, so unrelated churn cannot veto; cold `--full` also enters.
    pub fn allow_repeated_missing_roots(&mut self, agents: HashSet<String>) {
        debug_assert!(self.repair_mode || !self.warm);
        self.repeated_absent_agents = agents;
    }

    /// The per-root form of [`Self::allow_repeated_missing_roots`]: `(agent, store root)` pairs
    /// two consecutive complete, issue-free preflights saw empty. Only that root's rows converge;
    /// the agent's other roots keep the ordinary guards.
    pub fn allow_repeated_missing_store_roots(&mut self, roots: HashSet<(String, PathBuf)>) {
        debug_assert!(self.repair_mode || !self.warm);
        self.repeated_absent_roots = roots.into_iter().collect();
    }

    pub(crate) fn repeated_absence_covers(&self, agent: &str, root: &Path) -> bool {
        self.repeated_absent_agents.contains(agent)
            || self
                .repeated_absent_roots
                .iter()
                .any(|(owner, absent)| owner == agent && source_path_eq(absent, root))
    }

    /// Name the instant before this run's first source stat (the preflight, when one feeds the
    /// cache). Entries whose stamp is not safely older are recorded racy and reparse next run.
    pub fn set_scan_start(&mut self, start: std::time::SystemTime) {
        self.racy_scan_start_ns = epoch_ns(start);
    }

    /// Whether recovery began with no last-good rows because the on-disk base was discarded.
    /// Callers use it to keep deletion evidence at the standard a cached generation set.
    pub fn discarded_base(&self) -> bool {
        self.discarded_base
    }

    /// Whether this cache decoded an on-disk generation rather than starting empty.
    pub fn decoded_last_good_base(&self) -> bool {
        self.last_good_base
    }

    /// No publication recorded an inventory (a release held every snapshot back): stand in
    /// with this decoded cache. It names the Stat sources with rows; see `published_from_cache`.
    pub fn set_published_material_from_cache(&mut self) {
        let paths = self
            .entries
            .iter()
            .filter(|(_, entry)| {
                !entry.msgs.is_empty() || !entry.event_keys.is_empty() || entry.legacy_had_events
            })
            .filter_map(|(key, _)| source_path_from_key(key))
            .collect();
        self.published_material = Some(paths);
        self.published_from_cache = true;
    }

    /// Supply the second stable source observation (a byte-identical preflight pair) that
    /// lets a discarded base's stable-deletion grant bind. Without it the grant is inert.
    pub fn confirm_discarded_base_observation(&mut self) {
        self.discarded_base_confirmed = true;
    }

    /// The one decision point for whether this pass may prove a stable deletion. A discarded
    /// base holds no last-good witness, so its grant binds only after the caller confirmed a
    /// repeated stable observation - in every lane, not just the ones that remember to check.
    fn stable_deletions_provable(&self) -> bool {
        self.allow_stable_deletions && (!self.discarded_base || self.discarded_base_confirmed)
    }

    /// Whether this run observed every source strongly enough to bless a preflight snapshot.
    /// A guarded stale serve is safe for current output, but only if the next run retries it.
    pub fn source_snapshot_safe(&self) -> bool {
        !self.guarded_stale
    }

    /// Checked after admission: a read of the published generation can still vouch for a store.
    pub fn output_complete(&self) -> bool {
        self.output_complete_past_unreadable()
            && !self.unreadable_unserved
            && self.unproven_token_failures.is_empty()
            && self
                .unvouched_reads
                .iter()
                .all(|(agent, root)| self.published_proves_empty(agent, root))
    }

    /// [`Self::output_complete`] but for reads that failed with no last-good copy, which an
    /// explicit rebuild may publish without: every guard else still holds output back.
    pub fn output_complete_past_unreadable(&self) -> bool {
        !self.output_incomplete
    }

    /// See `admitted_unpublished_failure`: the caller refuses a pass that admitted one and
    /// produced no rows, rather than publish an empty generation over an unread store.
    pub fn admitted_unpublished_failure(&self) -> bool {
        self.admitted_unpublished_failure
    }

    /// Admit each unproven failed token conversation whose session the verified published
    /// generation, `published_sessions` of its agent (None when it cannot be read whole), holds
    /// no row, session or event of: nothing published can be dropped with it.
    pub fn admit_unpublished_token_conversations(
        &mut self,
        mut published_sessions: impl FnMut(&str) -> Option<HashSet<String>>,
    ) {
        let agents: HashSet<String> = self
            .unproven_token_failures
            .iter()
            .map(|(agent, _, _)| agent.clone())
            .collect();
        for agent in agents {
            let Some(published) = published_sessions(&agent) else {
                continue;
            };
            let (held, admitted): (Vec<_>, Vec<_>) = std::mem::take(&mut self.unproven_token_failures)
                .into_iter()
                .partition(|(failed, _, session)| *failed != agent || published.contains(session));
            self.unproven_token_failures = held;
            self.admitted_unpublished_failure |= !admitted.is_empty();
            self.settled_token_failures.extend(admitted);
        }
    }

    /// See `settled_token_failures`: whether the failed conversation `session` of the token
    /// store database at `database` is one.
    pub fn token_failure_settled(&self, agent: &str, database: &Path, session: &str) -> bool {
        crate::ingest::registry::token_prefix(agent, database).is_some_and(|namespace| {
            self.settled_token_failures
                .contains(&(agent.to_owned(), namespace, session.to_owned()))
        })
    }

    /// The token of a conversation of the token store `database` whose read failed with no
    /// last-good copy: what an earlier pass's record of the same failure must match.
    pub fn token_failure_identity(
        &self,
        agent: &str,
        database: &Path,
        session: &str,
    ) -> Option<&str> {
        let namespace = crate::ingest::registry::token_prefix(agent, database)?;
        self.failed_token_identities
            .get(&(agent.to_owned(), namespace, session.to_owned()))
            .map(String::as_str)
    }

    /// Names a failed conversation by `identity` (its own rows) instead of its staleness token.
    pub(crate) fn set_token_failure_identity(
        &mut self,
        agent: &str,
        cache_id: &str,
        session: &str,
        identity: String,
    ) {
        let key = (
            agent.to_owned(),
            crate::ingest::registry::token_namespace(agent, cache_id),
            session.to_owned(),
        );
        if let Some(held) = self.failed_token_identities.get_mut(&key) {
            *held = identity;
        }
    }

    pub fn source_read_issues(&self) -> &[SourceReadIssue] {
        &self.source_read_issues
    }

    /// The inventory a publication guard may consult: exactly the source paths the last
    /// published generation contained. An empty set is the positive claim "that generation
    /// held nothing"; never calling this leaves the guard unable to prove anything.
    pub fn set_published_material(&mut self, paths: HashSet<PathBuf>) {
        self.published_material = Some(paths);
    }

    /// Scopes the published inventory itself could not read. It contributed no path for them
    /// either way, so absence under one is blindness, not a claim that they held nothing.
    pub fn set_published_blind_scopes(&mut self, scopes: HashSet<(String, PathBuf)>) {
        self.published_blind_scopes = scopes;
    }

    /// Token-store agents whose verified published generation was read and holds none of their
    /// rows or events: no database of theirs can lose published material, whatever is listed.
    pub fn set_unpublished_token_agents(&mut self, agents: HashSet<String>) {
        self.unpublished_agents = agents;
    }

    /// Whole-store agents with a failed read neither a last-good snapshot nor the inventory
    /// covers: each pays one read of the published generation, `published`, and one that holds
    /// no row or event of it has nothing a publication could drop.
    pub fn admit_unpublished_whole_store_agents(
        &mut self,
        mut published: impl FnMut(&str) -> bool,
    ) {
        let uncovered: HashSet<&'static str> = self
            .source_read_issues
            .iter()
            .filter(|read| crate::ingest::registry::whole_store_agent(read.agent))
            .filter(|read| !self.unreadable_scope_covered(read.agent, &read.path))
            .map(|read| read.agent)
            .collect();
        for agent in uncovered {
            if !published(agent) {
                self.unpublished_agents.insert(agent.to_owned());
            }
        }
    }

    /// In an older generation's cache-derived inventory, an adapter that can read partially is
    /// silent; a failed read of one pays a read of the published generation, `published`, and
    /// one holding no row or event of it could have published nothing uncached either.
    pub fn admit_unpublished_partial_read_agents<'a>(
        &mut self,
        preflight_agents: impl IntoIterator<Item = &'a str>,
        mut published: impl FnMut(&str) -> bool,
    ) {
        if !self.legacy_cache_inventory() {
            return;
        }
        let failed: HashSet<String> = preflight_agents
            .into_iter()
            .chain(self.source_read_issues.iter().map(|read| read.agent))
            .filter(|agent| crate::ingest::registry::partial_read_agent(agent))
            .map(str::to_owned)
            .collect();
        for agent in failed {
            if !self.unpublished_agents.contains(&agent) && !published(&agent) {
                self.unpublished_agents.insert(agent);
            }
        }
    }

    /// Whole stores whose failed read seeded a snapshot this pass: a read of the published
    /// generation, `published`, admits one whose every row, with its attribution, and every event
    /// session the seed serves. Only a failed unit yields nothing, so each failure must be one.
    pub fn admit_fully_served_whole_store_agents<'a>(
        &mut self,
        preflight_issues: impl IntoIterator<Item = (&'a str, &'a Path)>,
        mut published: impl FnMut(&str) -> Option<crate::cache::PublishedAgentRows>,
    ) {
        let unit_scoped = crate::ingest::registry::unit_scoped_read_issue;
        let mut agent_wide: HashSet<String> = preflight_issues
            .into_iter()
            .filter(|(agent, path)| !unit_scoped(agent, path))
            .map(|(agent, _)| agent.to_owned())
            .collect();
        agent_wide.extend(
            self.source_read_issues
                .iter()
                .filter(|read| !unit_scoped(read.agent, &read.path))
                .map(|read| read.agent.to_owned()),
        );
        let seeded: Vec<String> = self
            .seeded_snapshots
            .iter()
            .filter(|agent| !self.unpublished_agents.contains(*agent))
            .filter(|agent| !agent_wide.contains(*agent))
            .filter(|agent| crate::ingest::registry::whole_store_agent(agent))
            .filter(|agent| !crate::ingest::registry::partial_read_agent(agent))
            .cloned()
            .collect();
        for agent in seeded {
            let Some(seed) = self.entries.get(&format!("\x00snapshot\x00{agent}")) else {
                continue;
            };
            let turns: HashMap<(&str, u32), &CMsg> = seed
                .msgs
                .iter()
                .map(|message| ((message.session.as_ref(), message.turn), message))
                .collect();
            let sessions: HashSet<&str> = turns.keys().map(|(session, _)| *session).collect();
            let event_sessions: HashSet<&str> = seed
                .event_keys
                .iter()
                .map(|key| key.session.as_str())
                .collect();
            let attributed = |message: &CMsg, row: &crate::cache::PublishedTurn| {
                *message.project == *row.project
                    && row
                        .model
                        .as_deref()
                        .is_none_or(|model| *message.model == *model)
            };
            let served = published(&agent).is_some_and(|rows| {
                rows.turns.iter().all(|((session, turn), row)| {
                    turns
                        .get(&(session.as_str(), *turn))
                        .is_some_and(|message| attributed(message, row))
                }) && rows
                    .sessions
                    .iter()
                    .all(|session| sessions.contains(session.as_str()))
                    && rows
                        .event_sessions
                        .iter()
                        .all(|session| event_sessions.contains(session.as_str()))
            });
            if served {
                self.unpublished_agents.insert(agent);
            }
        }
    }

    /// A per-file agent's failed read the cache alone cannot answer for pays one read of the
    /// published generation, `published`: an older generation's partial read may have published
    /// uncached, and a listed file this cache never held may have published rows only there.
    pub fn record_published_rows<'a>(
        &mut self,
        preflight_issues: impl IntoIterator<Item = (&'a str, &'a Path)>,
        mut published: impl FnMut(&str) -> Option<crate::cache::PublishedAgentRows>,
    ) {
        let issues: Vec<(&str, &Path)> = preflight_issues.into_iter().collect();
        let mut needed: HashSet<String> = issues
            .iter()
            .filter(|(agent, scope)| self.cache_cannot_answer_for(agent, scope))
            .map(|(agent, _)| (*agent).to_owned())
            .collect();
        needed.extend(
            self.source_read_issues
                .iter()
                .filter(|read| self.cache_cannot_answer_for(read.agent, &read.path))
                .map(|read| read.agent.to_owned()),
        );
        for agent in needed {
            let held = published(&agent).map(|rows| self.holds_published_rows(&agent, &rows));
            self.published_rows.insert(agent, held);
        }
    }

    /// Whether this pass publishes, or saw go, every row and session of `agent` that `published`
    /// holds. Rows match by session, timestamp and text, one for one: turn-collision repair
    /// renumbers turns but changes none of those, so no other row can stand in for a lost one.
    fn holds_published_rows(
        &self,
        agent: &str,
        published: &crate::cache::PublishedAgentRows,
    ) -> bool {
        let changed = self.changed_entries();
        let rows = self.cached_rows(agent, &changed, |_| true);
        let touched = Self::touched_sessions(agent, &changed);
        let mut released = self.released_rows(agent, &changed, &touched, &rows);
        let mut output = published_counts(rows.iter().map(|row| row.row));
        let rows_held = published.turns.iter().all(|((session, _), row)| {
            let key = (session.as_str(), row.ts, row.text.as_str());
            output.take(key) || released.take(key)
        });
        let event_sessions = self
            .entries
            .values()
            .flat_map(|entry| &entry.event_keys)
            .filter(|key| key.agent == agent)
            .map(|key| key.session.as_str());
        let sessions: HashSet<&str> = rows
            .iter()
            .map(|row| row.row.session.as_ref())
            .chain(event_sessions)
            .chain(released.sessions.iter().copied())
            .collect();
        rows_held
            && published
                .sessions
                .iter()
                .chain(&published.event_sessions)
                .all(|session| sessions.contains(session.as_str()))
    }

    fn under_failed_scope(&self, key: &str) -> bool {
        source_path_from_key(key).is_some_and(|path| {
            self.failed_scopes
                .iter()
                .any(|scope| source_path_within(&path, scope))
        })
    }

    /// The entries whose rows or events this pass changed, each with the base's version.
    fn changed_entries(&self) -> HashMap<&str, (Option<&Entry>, Option<&Entry>)> {
        fn held(entry: Option<&Entry>) -> (&[CMsg], &[CEventKey]) {
            entry.map_or((&[], &[]), |entry| (&entry.msgs, &entry.event_keys))
        }
        let mut changed = HashMap::new();
        for (key, base) in &self.displaced {
            let (base, current) = (base.as_ref(), self.entries.get(key));
            let same = held(base) == held(current);
            if !same {
                changed.insert(key.as_str(), (base, current));
            }
        }
        changed
    }

    /// Sessions of `agent` a `changed` entry holds, or held in the base.
    fn touched_sessions<'a>(
        agent: &str,
        changed: &HashMap<&str, (Option<&'a Entry>, Option<&'a Entry>)>,
    ) -> HashSet<&'a str> {
        let mut touched = HashSet::new();
        for (base, current) in changed.values() {
            for entry in base.iter().chain(current) {
                let rows = entry.msgs.iter().filter(|row| row.agent == agent);
                touched.extend(rows.map(|row| row.session.as_ref()));
                let events = entry.event_keys.iter().filter(|event| event.agent == agent);
                touched.extend(events.map(|event| event.session.as_str()));
            }
        }
        touched
    }

    /// The cached rows of `agent` in the sessions `within` accepts.
    fn cached_rows(
        &self,
        agent: &str,
        changed: &HashMap<&str, (Option<&Entry>, Option<&Entry>)>,
        within: impl Fn(&str) -> bool,
    ) -> Vec<CachedRow<'_>> {
        let mut rows = Vec::new();
        for (key, entry) in &self.entries {
            let mut place = None;
            for row in &entry.msgs {
                if row.agent != agent || !within(&row.session) {
                    continue;
                }
                let (beside, unchanged) = *place.get_or_insert_with(|| {
                    (
                        !self.under_failed_scope(key),
                        !changed.contains_key(key.as_str()),
                    )
                });
                rows.push(CachedRow {
                    row,
                    beside,
                    unchanged,
                });
            }
        }
        rows
    }

    /// What this pass, after the passes since the published generation, saw go of `agent` beside
    /// the scopes the preflight failed to read. Base and current entries differ only in `touched`
    /// sessions, so `rows` need hold only the cached rows of those.
    fn released_rows<'a>(
        &'a self,
        agent: &str,
        changed: &HashMap<&'a str, (Option<&'a Entry>, Option<&'a Entry>)>,
        touched: &HashSet<&'a str>,
        rows: &[CachedRow<'a>],
    ) -> Released<'a> {
        let beside = rows
            .iter()
            .filter(|row| row.beside && touched.contains(row.row.session.as_ref()));
        let mut base: Vec<&CMsg> = Vec::new();
        let mut sessions: HashSet<&str> = HashSet::new();
        if self.last_good_base {
            base.extend(
                beside
                    .clone()
                    .filter(|row| row.unchanged)
                    .map(|row| row.row),
            );
            for (key, (entry, _)) in changed {
                let Some(entry) = entry.filter(|_| !self.under_failed_scope(key)) else {
                    continue;
                };
                for row in entry.msgs.iter().filter(|row| row.agent == agent) {
                    base.push(row);
                    sessions.insert(row.session.as_ref());
                }
                let events = entry.event_keys.iter().filter(|event| event.agent == agent);
                sessions.extend(events.map(|event| event.session.as_str()));
            }
        }
        let base = published_counts(base);
        let beside = published_counts(beside.map(|row| row.row));
        let mut released = Released::default();
        if let Some(prior) = self.released.get(agent) {
            for ((session, ts, digest), count) in &prior.rows {
                let recorded = released.recorded.entry((session.as_str(), *ts));
                recorded.or_default().push((*digest, *count));
            }
            sessions.extend(prior.sessions.iter().map(String::as_str));
        }
        // In the touched sessions, what the base held and earlier passes recorded, less what this
        // pass holds there; elsewhere, the records alone.
        let only_beside = beside.iter().filter(|(key, _)| base.count(*key) == 0);
        for (key, _) in base.iter().chain(only_beside) {
            let mut recorded = 0;
            if let Some(records) = released.recorded.get_mut(&(key.0, key.1)) {
                let digest = crate::cache::text_digest(key.2);
                for (_, count) in records.iter_mut().filter(|(held, _)| *held == digest) {
                    recorded += std::mem::take(count);
                }
            }
            let gone = (base.count(key) + recorded).saturating_sub(beside.count(key));
            if gone > 0 {
                released.rows.add(key, gone);
            }
        }
        for entry in self.entries.values() {
            if sessions.is_empty() {
                break;
            }
            for row in entry.msgs.iter().filter(|row| row.agent == agent) {
                sessions.remove(row.session.as_ref());
            }
            for event in entry.event_keys.iter().filter(|event| event.agent == agent) {
                sessions.remove(event.session.as_str());
            }
        }
        released.sessions = sessions;
        released
    }

    /// Before ingest mutates this decoded base, take it as what the pass's changes count from, and
    /// adopt what passes since the published generation, sealed `published`, saw go before a kill
    /// left it published. No count of rows seen go looks under the scopes the preflight failed.
    pub fn mark_base_rows<'a>(
        &mut self,
        preflight_issues: impl IntoIterator<Item = (&'a str, &'a Path)>,
        published: Option<crate::cache::GenerationSeal>,
    ) {
        self.displaced.clear();
        self.failed_scopes = preflight_issues
            .into_iter()
            .map(|(_, scope)| scope.to_path_buf())
            .collect();
        self.published_seal = published.filter(|_| self.last_good_base);
        self.released = match &self.published_seal {
            Some(seal) => recorded_released_rows(&self.entries, &hex16(seal)),
            None => HashMap::new(),
        };
    }

    /// Stage with the cache what this pass and those since the published generation saw go: a kill
    /// between the cache commit and that generation's replacement leaves the cache short of rows
    /// still published. Records against any other generation go.
    pub fn record_released_rows(&mut self) {
        let mut released: HashMap<String, ReleasedRows> = HashMap::new();
        let mut sealed = None;
        if let Some(seal) = self.published_seal.as_ref().map(hex16) {
            let changed = self.changed_entries();
            let mut agents: HashSet<&str> = self.released.keys().map(String::as_str).collect();
            for (base, current) in changed.values() {
                for entry in base.iter().chain(current) {
                    agents.extend(entry.msgs.iter().map(|row| row.agent.as_str()));
                    agents.extend(entry.event_keys.iter().map(|event| event.agent.as_str()));
                }
            }
            // Whole stores keep their last good rows in a snapshot, not in per-source entries.
            for agent in agents
                .into_iter()
                .filter(|agent| !crate::ingest::registry::whole_store_agent(agent))
            {
                let touched = Self::touched_sessions(agent, &changed);
                let rows = self.cached_rows(agent, &changed, |session| touched.contains(session));
                let record = self
                    .released_rows(agent, &changed, &touched, &rows)
                    .record();
                released.insert(agent.to_owned(), record);
            }
            sealed = Some(seal);
        }
        let record = sealed.and_then(|seal| pack_released(&seal, &released));
        let stale: Vec<String> = self
            .entries
            .keys()
            .filter(|key| released_key(key) && Some(*key) != record.as_ref().map(|(key, _)| key))
            .cloned()
            .collect();
        for key in stale {
            self.remove_entry(&key);
        }
        if let Some((key, entry)) = record {
            let staged = self
                .entries
                .get(&key)
                .is_some_and(|held| !held.legacy_needs_reparse && held.identity == entry.identity);
            if !staged {
                self.put_entry(key, entry);
            }
        }
    }

    fn per_file_agent(agent: &str) -> bool {
        !crate::ingest::registry::whole_store_agent(agent)
            && !crate::ingest::registry::token_store_agent(agent)
    }

    /// Whether a pass since the published generation, killed before replacing it, saw rows of
    /// `agent` go that it still holds: this base is ahead of what is published.
    fn base_ahead(&self, agent: &str) -> bool {
        self.released
            .get(agent)
            .is_some_and(|released| !released.rows.is_empty() || !released.sessions.is_empty())
    }

    fn cache_cannot_answer_for(&self, agent: &str, scope: &Path) -> bool {
        Self::per_file_agent(agent)
            && !self.published_rows.contains_key(agent)
            && ((self.legacy_generation && crate::ingest::registry::partial_read_agent(agent))
                || self.listed_files_cached(scope) == Some(false))
    }

    /// The source path of every entry that names one, built once per settled entry set: the
    /// guards that consult it run once per unreadable scope, which can be thousands.
    fn source_paths(&self) -> &HashSet<PathBuf> {
        self.source_paths.get_or_init(|| {
            self.entries
                .keys()
                .filter_map(|key| source_path_from_key(key))
                .collect()
        })
    }

    /// Whether this cache holds an entry, with rows or none, for every file the published
    /// inventory lists under `scope`; None when it lists none there.
    fn listed_files_cached(&self, scope: &Path) -> Option<bool> {
        let published = self.published_material.as_ref()?;
        let mut listed = published
            .iter()
            .filter(|path| source_path_within(path, scope))
            .peekable();
        listed.peek()?;
        let cached = self.source_paths();
        Some(listed.all(|path| {
            cached.contains(path.as_path())
                || (cfg!(windows)
                    && cached
                        .iter()
                        .any(|held| source_path_within(held, scope) && source_path_eq(held, path)))
        }))
    }

    /// Whether this pass holds every row and session of `agent` the published generation does, as
    /// [`Self::record_published_rows`] judged it; None when it was not read.
    fn cache_holds_published_rows(&self, agent: &str) -> Option<bool> {
        self.published_rows.get(agent).copied().flatten()
    }

    /// An inventory rebuilt from a cache an older generation wrote.
    fn legacy_cache_inventory(&self) -> bool {
        self.published_from_cache && self.legacy_generation
    }

    /// The token databases the published generation may hold material of; see
    /// [`Self::token_namespaces`]. `None` when that record is missing or describes another one.
    pub fn set_published_token_namespaces(
        &mut self,
        namespaces: Option<HashSet<(String, String)>>,
    ) {
        self.published_token_namespaces = namespaces;
    }

    /// `(agent, database namespace)` of every cached token conversation with rows or events.
    /// Every token row a pass publishes is served from such an entry, so recorded before the
    /// derived writes this names each database the publication can hold material of.
    pub fn token_namespaces(&self) -> HashSet<(String, String)> {
        self.entries
            .iter()
            .filter(|(_, entry)| {
                !entry.msgs.is_empty() || !entry.event_keys.is_empty() || entry.legacy_had_events
            })
            .filter_map(|(key, _)| key.strip_prefix("\0tok\0")?.split_once('\0'))
            .map(|(agent, cache_id)| {
                let namespace = crate::ingest::registry::token_namespace(agent, cache_id);
                (agent.to_owned(), namespace)
            })
            .collect()
    }

    /// The project each of `agent`'s cached token conversations was published under.
    pub fn token_projects(&self, agent: &str) -> HashMap<String, String> {
        token_projects_of(&self.entries, agent)
    }

    /// The project the published generation gave each of `agent`'s token conversations, read
    /// from its rows when this cache holds none: an attribution gap keeps them unrelabelled.
    pub fn published_token_projects(&self, agent: &str) -> HashMap<String, String> {
        self.published_token_projects
            .get(agent)
            .cloned()
            .unwrap_or_default()
    }

    pub fn set_published_token_projects(&mut self, agent: &str, projects: HashMap<String, String>) {
        self.published_token_projects
            .insert(agent.to_owned(), projects);
    }

    /// Sessions the last-good rows attribute to `scope`. Read before ingest, this is the
    /// per-scope row census a publication may not silently shrink while `scope` is unreadable.
    pub fn sessions_under(&self, scope: &Path) -> HashSet<String> {
        self.entries
            .iter()
            .filter(|(key, _)| {
                source_path_from_key(key).is_some_and(|path| source_path_within(&path, scope))
            })
            .flat_map(|(_, entry)| entry.msgs.iter().map(|msg| msg.session.to_string()))
            .collect()
    }

    /// `(source path, agent, session)` for every cached file with rows: the identities the
    /// store's own filenames carry, resolved against the header ids at publication.
    pub fn session_sources(&self) -> Vec<SessionSource> {
        session_sources_of(&self.entries)
    }

    fn published_inventory_blind_to(&self, agent: &str, scope: &Path) -> bool {
        self.published_blind_scopes
            .iter()
            .any(|(blind_agent, blind)| {
                blind_agent == agent
                    && (source_path_within(blind, scope) || source_path_within(scope, blind))
            })
    }

    /// Whether a cache-derived inventory is silent on `scope`: a whole store, which admission
    /// decides, or, in an older generation, a source whose partial read published uncached.
    /// Older generations cached every other Stat read they published; a failed read published none.
    fn cache_inventory_silent_on(&self, agent: &str, scope: &Path) -> bool {
        self.published_from_cache
            && crate::ingest::registry::token_prefix(agent, scope).is_none()
            && (crate::ingest::registry::whole_store_agent(agent)
                || (self.legacy_cache_inventory()
                    && crate::ingest::registry::partial_read_agent(agent)))
    }

    /// Whether this cache holds conversations with rows or events of the token store database
    /// at `scope`: a pass that held its snapshot back may still have published them.
    fn holds_token_entries(&self, agent: &str, scope: &Path) -> bool {
        let Some(prefix) = crate::ingest::registry::token_prefix(agent, scope) else {
            return false;
        };
        let prefix = format!("\0tok\0{agent}\0{prefix}");
        self.entries.iter().any(|(key, entry)| {
            key.starts_with(&prefix)
                && (!entry.msgs.is_empty()
                    || !entry.event_keys.is_empty()
                    || entry.legacy_had_events)
        })
    }

    /// Whether the token store database at `scope` may hold published or cached material: its
    /// namespace is recorded as published, that record is unknown, or this cache holds it.
    fn token_scope_material(&self, agent: &str, scope: &Path) -> bool {
        let Some(prefix) = crate::ingest::registry::token_prefix(agent, scope) else {
            return false;
        };
        let recorded = |(owner, namespace): &(String, String)| {
            owner == agent && (prefix.starts_with(namespace) || namespace.starts_with(&prefix))
        };
        self.holds_token_entries(agent, scope)
            || self
                .published_token_namespaces
                .as_ref()
                .is_none_or(|published| published.iter().any(recorded))
    }

    /// Whether no published row of `agent`'s token conversations lies beyond this cache's entries:
    /// a decoded base no killed pass moved past the published generation cached each it published
    /// (as `published_token_inventory` relies on), and a verified generation may hold none at all.
    fn token_entries_answer_for(&self, agent: &str) -> bool {
        (self.last_good_base && !self.base_ahead(agent)) || self.unpublished_agents.contains(agent)
    }

    /// Whether no published generation can hold rows of the token conversation `cache_id`, which
    /// this cache holds no entry for. Without a base that answers for them, the inventory must
    /// exist and the published-namespace record must name none of its database.
    fn token_conversation_unpublished(&self, agent: &str, cache_id: &str) -> bool {
        if self.token_entries_answer_for(agent) {
            return true;
        }
        let namespace = crate::ingest::registry::token_namespace(agent, cache_id);
        self.published_material.is_some()
            && self.published_token_namespaces.as_ref().is_some_and(|published| {
                !published.iter().any(|(owner, recorded)| {
                    owner == agent
                        && (recorded.starts_with(&namespace) || namespace.starts_with(recorded.as_str()))
                })
            })
    }

    /// Whether a token store no read could list, and of which this cache holds no entry, had
    /// nothing published: the base answers for it, or the inventory proves empty every scope its
    /// reads failed on this pass (a failure that named no scope proves nothing).
    fn token_store_unpublished(&self, agent: &str) -> bool {
        if self.token_entries_answer_for(agent) {
            return true;
        }
        let mut failed = self
            .source_read_issues
            .iter()
            .filter(|read| read.agent == agent)
            .peekable();
        failed.peek().is_some() && failed.all(|read| self.published_proves_empty(agent, &read.path))
    }

    /// A scope with nothing at stake: no publication can drop anything of its agent, and it is a
    /// whole store, a source an older generation's inventory is silent on, or a token database
    /// this cache holds no conversation of.
    fn unpublished_scope(&self, agent: &str, scope: &Path) -> bool {
        self.unpublished_agents.contains(agent)
            && match crate::ingest::registry::token_prefix(agent, scope) {
                Some(_) => !self.holds_token_entries(agent, scope),
                None => {
                    crate::ingest::registry::whole_store_agent(agent)
                        || (self.legacy_cache_inventory()
                            && crate::ingest::registry::partial_read_agent(agent))
                }
            }
    }

    /// Whether `agent`'s whole-store snapshot holds material an earlier pass published.
    fn snapshot_witnesses(&self, agent: &str) -> bool {
        !self.seeded_snapshots.contains(agent)
            && self
                .entries
                .get(&format!("\x00snapshot\x00{agent}"))
                .is_some_and(|entry| {
                    !entry.msgs.is_empty()
                        || !entry.event_keys.is_empty()
                        || entry.legacy_had_events
                })
    }

    /// Would publishing without `scope` (an unobservable subtree of `agent`'s store) drop
    /// material the last published generation held and this pass cannot serve?
    ///
    /// Absence of the inventory is [`MaterialVerdict::Unknown`], never `Retained`: a guard
    /// whose job is proving absence must not be satisfiable by absence of its own input.
    /// An inventory blind to `scope` is likewise unknown unless retained rows still answer.
    pub fn published_material_under(&self, agent: &str, scope: &Path) -> MaterialVerdict {
        // A read of the published generation itself, not an absent input.
        if self.unpublished_scope(agent, scope) {
            return MaterialVerdict::Retained;
        }
        let Some(published) = self.published_material.as_ref() else {
            return MaterialVerdict::Unknown;
        };
        let has_material = |entry: &Entry| {
            !entry.msgs.is_empty() || !entry.event_keys.is_empty() || entry.legacy_had_events
        };
        let per_file = !crate::ingest::registry::whole_store_agent(agent)
            && !crate::ingest::registry::token_store_agent(agent);
        if per_file {
            if self.legacy_generation && crate::ingest::registry::partial_read_agent(agent) {
                // An older build published a partial read uncached, under any entry or none.
                return match self.cache_holds_published_rows(agent) {
                    Some(true) => MaterialVerdict::Retained,
                    Some(false) => MaterialVerdict::Drops,
                    None => MaterialVerdict::Unknown,
                };
            }
            // A failed read re-emits a cached file's entry, rows or none; a listed file the cache
            // never held may have published rows only that generation holds, unless it shows none.
            match self.listed_files_cached(scope) {
                Some(true) => return MaterialVerdict::Retained,
                Some(false) => {
                    return match self.cache_holds_published_rows(agent) {
                        Some(true) => MaterialVerdict::Retained,
                        _ => MaterialVerdict::Drops,
                    };
                }
                None => {}
            }
            let retained_rows = self.entries.iter().any(|(key, entry)| {
                source_path_from_key(key).is_some_and(|path| source_path_within(&path, scope))
                    && has_material(entry)
            });
            if retained_rows {
                return MaterialVerdict::Retained;
            }
        } else {
            // An Always adapter's last-good snapshot carries its whole store, path keys and
            // all; a token collect serves every cached conversation of a database it cannot read.
            let retained_here = self.snapshot_witnesses(agent)
                || self.holds_token_entries(agent, scope)
                || self.entries.iter().any(|(key, entry)| {
                    source_path_from_key(key).is_some_and(|path| source_path_within(&path, scope))
                        && has_material(entry)
                });
            if retained_here {
                return MaterialVerdict::Retained;
            }
            if published.iter().any(|path| source_path_within(path, scope))
                || self.token_scope_material(agent, scope)
            {
                return MaterialVerdict::Drops;
            }
        }
        // No path here, and nothing retained. Only an inventory that could actually read the
        // scope turns that silence into the positive claim "it held nothing".
        if self.cache_inventory_silent_on(agent, scope)
            || (self.published_inventory_blind_to(agent, scope) && !self.last_good_base)
        {
            return MaterialVerdict::Unknown;
        }
        MaterialVerdict::Retained
    }

    /// May a pass publish although `scope` under `agent` was unreadable?
    ///
    /// True exactly when the guarded snapshot still serves the agent's
    /// last-good rows, or the published inventory positively proves the scope
    /// never published material (fresh boxes carry the proven-empty set).
    /// Narrower than [`Self::published_material_under`] on purpose: an
    /// Always-adapter's stale per-file entries are never re-emitted, so their
    /// Retained verdict must not license publishing without their rows.
    pub fn unreadable_scope_covered(&self, agent: &str, scope: &Path) -> bool {
        if self.snapshot_witnesses(agent) {
            return true;
        }
        self.published_proves_empty(agent, scope)
    }

    /// Did this pass still publish what `scope` held although it could not read it? Covered,
    /// or (outside whole stores, whose stale per-file entries go unserved) its cached rows,
    /// which the per-file and token lanes re-emit for every read that fails.
    pub fn unread_scope_served(&self, agent: &str, scope: &Path) -> bool {
        self.unreadable_scope_covered(agent, scope)
            || (!crate::ingest::registry::whole_store_agent(agent)
                && self.published_material_under(agent, scope) == MaterialVerdict::Retained)
    }

    /// The published inventory's positive claim that `scope` held no material.
    fn published_proves_empty(&self, agent: &str, scope: &Path) -> bool {
        if self.unpublished_scope(agent, scope) {
            return true;
        }
        match self.published_material.as_ref() {
            Some(paths) => {
                (!paths.iter().any(|path| source_path_within(path, scope))
                    || self.base_holds_no_file_under(agent, scope))
                    && !self.token_scope_material(agent, scope)
                    && !self.published_inventory_blind_to(agent, scope)
                    && !self.cache_inventory_silent_on(agent, scope)
            }
            None => false,
        }
    }

    /// A decoded base, sealed against the published generation and not moved past it by a killed
    /// pass, caches every per-file row it published: no entry under `scope` proves it took none,
    /// though its snapshot lists the files it failed to read (older uncached partial reads aside).
    fn base_holds_no_file_under(&self, agent: &str, scope: &Path) -> bool {
        let under = |key: &str| {
            source_path_from_key(key).is_some_and(|path| source_path_within(&path, scope))
        };
        self.last_good_base
            && !self.legacy_generation
            && self.published_seal.is_some()
            && !self.base_ahead(agent)
            && !crate::ingest::registry::whole_store_agent(agent)
            && !crate::ingest::registry::token_store_agent(agent)
            && !self
                .source_paths()
                .iter()
                .any(|path| source_path_within(path, scope))
            && !self
                .displaced
                .iter()
                .any(|(key, base)| base.is_some() && under(key))
    }

    pub(crate) fn guard_epoch(&self) -> u64 {
        self.guard_epoch
    }

    pub(crate) fn mark_guarded_stale(&mut self) {
        self.guarded_stale = true;
        self.guard_epoch = self.guard_epoch.saturating_add(1);
    }

    pub(crate) fn record_source_read_issue(
        &mut self,
        agent: &'static str,
        path: &Path,
        kind: &'static str,
        reason: impl Into<String>,
    ) {
        self.record_read_issue(SourceReadIssue::new(agent, path, kind, reason));
    }

    pub(crate) fn record_read_issue(&mut self, issue: SourceReadIssue) {
        if !self.source_read_issues.iter().any(|prior| prior == &issue) {
            self.source_read_issues.push(issue);
        }
    }

    /// A failed read with no last-good copy holds publication back until a pass reads it or,
    /// for material seen to fail the same way twice, a rebuild's consent.
    fn hold_unserved_read(&mut self, outcome: Option<ReadOutcome>) {
        if matches!(
            outcome,
            Some(ReadOutcome::Invalid | ReadOutcome::Unavailable | ReadOutcome::Moved)
        ) {
            self.unreadable_unserved = true;
        }
    }

    pub(crate) fn extend_source_read_issues(
        &mut self,
        issues: impl IntoIterator<Item = SourceReadIssue>,
    ) {
        for issue in issues {
            self.record_source_read_issue(issue.agent, &issue.path, issue.kind, issue.reason);
        }
    }

    /// Test trace for deletion-guard coverage; production publication does not consume it.
    #[cfg(test)]
    pub fn has_provisional_deletions(&self) -> bool {
        self.provisional_deletions
    }

    /// Exact currently-live event files owned by all cached sources after this ingest pass.
    pub fn live_event_files(&self) -> HashSet<String> {
        self.entries
            .values()
            .flat_map(|entry| entry.event_keys.iter())
            .map(|key| crate::cache::event_fname(&key.agent, &key.session))
            .collect()
    }

    /// Old event ownership touched by changed/deleted sources and no longer owned by any live
    /// source. Incremental event publication may safely prune exactly this set.
    pub fn event_prune_files(&self, live: &HashSet<String>) -> HashSet<String> {
        self.event_touched.difference(live).cloned().collect()
    }

    fn touch_event_keys(&mut self, keys: &[CEventKey]) {
        for key in keys {
            self.touched.insert(key.session.clone());
            self.event_touched
                .insert(crate::cache::event_fname(&key.agent, &key.session));
        }
    }

    /// Seed a whole store's first last-good snapshot from a read that missed part of it. It is
    /// no witness this pass, and it publishes only where nothing published is at stake.
    fn seed_snapshot(
        &mut self,
        agent: &str,
        root: &Path,
        key: String,
        fresh: &[Message],
        event_keys: Vec<CEventKey>,
    ) {
        self.seeded_snapshots.insert(agent.to_owned());
        self.unvouched_reads
            .insert((agent.to_owned(), root.to_path_buf()));
        let sessions: Vec<String> = fresh
            .iter()
            .map(|message| message.session.to_string())
            .chain(event_keys.iter().map(|event| event.session.clone()))
            .collect();
        self.touched.extend(sessions);
        self.touch_event_keys(&event_keys);
        self.put_entry(
            key,
            Entry {
                mtime: 0,
                size: 0,
                identity: None,
                msgs: fresh.iter().map(CMsg::from).collect(),
                event_keys,
                legacy_had_events: false,
                legacy_needs_reparse: false,
            },
        );
    }

    /// Last-good fallback for full-parse adapters: never let a transient empty
    /// result (store locked/absent/unreadable) become an emitted empty - that cascades to
    /// session-index rewrite -> corpus removal-reconciliation -> event-file unlink. A
    /// non-empty run refreshes the adapter's last-good snapshot; an empty run serves it.
    /// The snapshot rides in the same cache file under a reserved key the per-file retain
    /// never touches (it starts with no source root). `--full` (cold cache) has no snapshot,
    /// so a confirmed empty store needs the repeated-snapshot recovery path. Cache-driven
    /// adapters get the equivalent guarantee inside `collect_cached`.
    pub fn guard_never_empty(
        &mut self,
        agent: &str,
        root: &Path,
        fresh: Vec<Message>,
        fresh_events: &[Event],
        source_outcome: ReadOutcome,
    ) -> (Vec<Message>, bool) {
        let key = format!("\x00snapshot\x00{agent}");
        let fresh_event_keys = cached_event_keys(fresh_events);
        if matches!(
            source_outcome,
            ReadOutcome::Invalid
                | ReadOutcome::Partial
                | ReadOutcome::Unavailable
                | ReadOutcome::Moved
        ) {
            self.mark_guarded_stale();
            if let Some(entry) = self.entries.get(&key) {
                return (entry.msgs.iter().map(CMsg::to_msg).collect(), true);
            }
            // No snapshot: may replace the generation only where nothing published is at stake.
            // What did parse seeds the last-good snapshot later failed reads serve; its events
            // stay unwritten, like the rest of this lossy read.
            if fresh.is_empty() {
                self.unvouched_reads
                    .insert((agent.to_owned(), root.to_path_buf()));
            } else {
                self.seed_snapshot(agent, root, key, &fresh, Vec::new());
            }
            return (fresh, true);
        }
        if source_outcome == ReadOutcome::Skipped {
            self.mark_guarded_stale();
            let Some(entry) = self.entries.get(&key).cloned() else {
                if fresh.is_empty() && fresh_event_keys.is_empty() {
                    self.output_incomplete = true;
                    return (fresh, true);
                }
                self.seed_snapshot(agent, root, key, &fresh, fresh_event_keys);
                return (fresh, false);
            };
            let fresh_sessions: HashSet<String> = fresh
                .iter()
                .map(|message| message.session.to_string())
                .chain(fresh_events.iter().map(|event| event.session.clone()))
                .collect();
            if fresh_sessions.is_empty() {
                return (entry.msgs.iter().map(CMsg::to_msg).collect(), true);
            }
            let mut merged: Vec<Message> = entry
                .msgs
                .iter()
                .filter(|message| !fresh_sessions.contains(message.session.as_ref()))
                .map(CMsg::to_msg)
                .collect();
            merged.extend(fresh);
            let replaced_event_keys: Vec<CEventKey> = entry
                .event_keys
                .iter()
                .filter(|event| fresh_sessions.contains(event.session.as_str()))
                .cloned()
                .collect();
            let mut merged_event_keys: Vec<CEventKey> = entry
                .event_keys
                .into_iter()
                .filter(|event| !fresh_sessions.contains(event.session.as_str()))
                .chain(fresh_event_keys.iter().cloned())
                .collect();
            merged_event_keys.sort_unstable();
            merged_event_keys.dedup();
            self.touched.extend(fresh_sessions);
            self.touch_event_keys(&replaced_event_keys);
            self.touch_event_keys(&fresh_event_keys);
            self.put_entry(
                key,
                Entry {
                    mtime: 0,
                    size: 0,
                    identity: None,
                    msgs: merged.iter().map(CMsg::from).collect(),
                    event_keys: merged_event_keys,
                    legacy_had_events: false,
                    legacy_needs_reparse: false,
                },
            );
            return (merged, false);
        }
        // The reparse flag marks a format upgrade, not material: a flagged entry with no rows or
        // events holds nothing a publication could drop, so it guards nothing.
        let prior_material = self.entries.get(&key).map(|entry| {
            !entry.msgs.is_empty() || !entry.event_keys.is_empty() || entry.legacy_had_events
        });
        // A complete preflight with no files/tokens proves clean ENOENT absence.
        // Permission/share failures remain incomplete; legacy expectations without prior rows
        // cannot fabricate an issue for an adapter that never materialized.
        let cleanly_absent = self.current_cleanly_absent_agents.contains(agent)
            || (self.current_snapshot_complete && !self.current_source_agents.contains(agent));
        // A discarded base holds no last-good rows, so one clean absence would converge a
        // whole-store deletion a cached generation would have made wait for its second
        // observation. Keep the guard armed and let `repeated_absent_agents` release it.
        if fresh.is_empty()
            && fresh_event_keys.is_empty()
            && (prior_material == Some(true)
                || (self.repair_mode
                    && prior_material.is_none()
                    && self.repair_expected_agents.contains(agent)
                    && (!cleanly_absent || self.discarded_base)))
        {
            if self.repair_mode
                && self.repeated_absent_agents.contains(agent)
                && !self.current_source_agents.contains(agent)
            {
                if let Some(entry) = self.remove_entry(&key) {
                    self.touched
                        .extend(entry.msgs.iter().map(|message| message.session.to_string()));
                    self.touch_event_keys(&entry.event_keys);
                }
                #[cfg(test)]
                {
                    self.provisional_deletions = true;
                }
                return (fresh, false);
            }
            // Whole-store disappearance is indistinguishable from a transient root/read
            // failure on an implicit run; keep the prior snapshot and retry.
            self.mark_guarded_stale();
            let messages = self
                .entries
                .get(&key)
                .into_iter()
                .flat_map(|entry| entry.msgs.iter().map(CMsg::to_msg))
                .collect();
            return (messages, true);
        }
        let old_event_keys = self
            .entries
            .get(&key)
            .map(|entry| entry.event_keys.clone())
            .unwrap_or_default();
        if let Some(entry) = self.entries.get(&key) {
            self.touched
                .extend(entry.msgs.iter().map(|message| message.session.to_string()));
        }
        self.touched
            .extend(fresh.iter().map(|message| message.session.to_string()));
        self.touch_event_keys(&old_event_keys);
        self.touch_event_keys(&fresh_event_keys);
        self.put_entry(
            key,
            Entry {
                mtime: 0,
                size: 0,
                identity: None,
                msgs: fresh.iter().map(CMsg::from).collect(),
                event_keys: fresh_event_keys,
                legacy_had_events: false,
                legacy_needs_reparse: false,
            },
        );
        (fresh, false)
    }

    /// Token-keyed conversation cache for Fingerprint::Token adapters (crush, cursor):
    /// reparse a conversation only when its token changed, instead of stat-ing one
    /// giant sqlite file whose mtime moves on every write. `tokens` is (session_id, token)
    /// for every live conversation (updatedAt or a value hash; see
    /// registry::token_fingerprint), or None when the store couldn't be opened at all. The
    /// entries ride the same cache file under a reserved per-agent key with the exact token.
    /// Sequential (no Sync bound) - a sqlite Connection isn't Sync and Token stores have few
    /// conversations.
    ///
    /// `None` (open failed) serves every cached conversation; a per-session
    /// parse that comes back empty for a session that had messages keeps the cached copy.
    /// `--full` (cold cache) has no tokens cached, so every conversation reparses - the
    /// documented recovery from a store the token logic wrongly thinks is unchanged.
    pub fn collect_token_cached<F, R>(
        &mut self,
        agent: &str,
        tokens: Option<Vec<(String, String)>>,
        parse: F,
    ) -> Pass
    where
        F: Fn(&str) -> R,
        R: IntoParsed,
    {
        let has_readable_source = tokens.is_some();
        self.collect_token_cached_keyed_partial(
            agent,
            tokens.map(|tokens| {
                tokens
                    .into_iter()
                    .map(|(session, token)| (session.clone(), session, token))
                    .collect()
            }),
            &[],
            false,
            has_readable_source,
            |_, session| parse(session),
        )
    }

    /// Keyed token cache that retains selected database namespaces after a partial open failure.
    pub(crate) fn collect_token_cached_keyed_partial<F, R>(
        &mut self,
        agent: &str,
        tokens: Option<Vec<(String, String, String)>>,
        unavailable: &[String],
        preserve_unlisted: bool,
        has_readable_source: bool,
        parse: F,
    ) -> Pass
    where
        F: Fn(&str, &str) -> R,
        R: IntoParsed,
    {
        let prefix = format!("\0tok\0{agent}\0");
        let coverage_mismatch = self
            .coverage_expected_tokens
            .get(agent)
            .map(|expected| match tokens.as_ref() {
                Some(actual) => {
                    let mut expected = expected.clone();
                    let mut actual: Vec<(String, String)> = actual
                        .iter()
                        .map(|(cache_id, _, token)| (cache_id.clone(), token.clone()))
                        .collect();
                    expected.sort();
                    actual.sort();
                    actual != expected
                }
                None => true,
            })
            .unwrap_or(false);
        if coverage_mismatch {
            self.mark_guarded_stale();
            let has_fallback = self.entries.keys().any(|key| key.starts_with(&prefix));
            let expected_nonempty = self
                .coverage_expected_tokens
                .get(agent)
                .map(|expected| !expected.is_empty())
                .unwrap_or(false);
            if !has_fallback && expected_nonempty {
                self.output_incomplete = true;
            }
            let messages: Vec<Message> = self
                .entries
                .iter()
                .filter(|(key, _)| key.starts_with(&prefix))
                .flat_map(|(_, entry)| entry.msgs.iter().map(CMsg::to_msg))
                .collect();
            crate::emit::rows_only(&messages);
            return Pass {
                messages,
                events: Vec::new(),
                parsed: 0,
            };
        }
        let tokens = match tokens {
            Some(t) => t,
            None => {
                let has_fallback = self.entries.keys().any(|key| key.starts_with(&prefix));
                let messages: Vec<Message> = self
                    .entries
                    .iter()
                    .filter(|(k, _)| k.starts_with(&prefix))
                    .flat_map(|(_, e)| e.msgs.iter().map(CMsg::to_msg))
                    .collect();
                self.mark_guarded_stale();
                // A store no read could list contributes its last-good rows; with none, it holds
                // publication back only where a published generation may hold its rows.
                if !has_fallback {
                    if self.token_store_unpublished(agent) {
                        self.admitted_unpublished_failure = true;
                    } else if self
                        .source_read_issues
                        .iter()
                        .any(|read| read.agent == agent && read.kind == "source-read-failed")
                    {
                        self.unreadable_unserved = true;
                    } else {
                        self.output_incomplete = true;
                    }
                }
                crate::emit::rows_only(&messages);
                return Pass {
                    messages,
                    events: Vec::new(),
                    parsed: 0,
                };
            }
        };
        let unavailable: Vec<String> = unavailable
            .iter()
            .map(|namespace| format!("{prefix}{namespace}"))
            .collect();
        let listed: HashSet<String> = tokens
            .iter()
            .map(|(cache_id, _, _)| format!("{prefix}{cache_id}"))
            .collect();
        let fallback_key = |key: &str| {
            unavailable
                .iter()
                .any(|namespace| key.starts_with(namespace))
                || (preserve_unlisted && key.starts_with(&prefix) && !listed.contains(key))
        };
        let mut partial_fallback: Vec<Message> = self
            .entries
            .iter()
            .filter(|(key, _)| fallback_key(key))
            .flat_map(|(_, entry)| entry.msgs.iter().map(CMsg::to_msg))
            .collect();
        partial_fallback.sort_unstable_by(|left, right| {
            (
                left.agent,
                left.session.as_ref(),
                left.turn,
                left.text.as_ref(),
            )
                .cmp(&(
                    right.agent,
                    right.session.as_ref(),
                    right.turn,
                    right.text.as_ref(),
                ))
        });
        let missing_partial_fallback = unavailable.iter().any(|namespace| {
            !self.entries.iter().any(|(key, entry)| {
                key.starts_with(namespace)
                    && (!entry.msgs.is_empty()
                        || !entry.event_keys.is_empty()
                        || entry.legacy_had_events)
            })
        });
        if preserve_unlisted {
            self.mark_guarded_stale();
        }
        if missing_partial_fallback {
            self.mark_guarded_stale();
            if !has_readable_source {
                self.output_incomplete = true;
            }
        } else if !unavailable.is_empty() && (!self.warm || self.repair_mode) {
            self.mark_guarded_stale();
        }
        let had_material_tokens = self.entries.iter().any(|(key, entry)| {
            key.starts_with(&prefix)
                && (!entry.msgs.is_empty()
                    || !entry.event_keys.is_empty()
                    || entry.legacy_had_events)
        });
        let empty_store_candidate = tokens.is_empty()
            && unavailable.is_empty()
            && !preserve_unlisted
            && (had_material_tokens || self.repair_expected_agents.contains(agent));
        // Confirmation needs a guarded retry/repair pass or a cold --full, never a plain warm
        // run: a validly-empty store (present, readable, zero conversations) converges through
        // the same repeated-observation protocol as clean ENOENT instead of wedging forever.
        let confirmed_empty_store = empty_store_candidate
            && (self.repair_mode || !self.warm)
            && self.repeated_absent_agents.contains(agent);
        if empty_store_candidate && !confirmed_empty_store {
            let messages: Vec<Message> = self
                .entries
                .iter()
                .filter(|(key, _)| key.starts_with(&prefix))
                .flat_map(|(_, entry)| entry.msgs.iter().map(CMsg::to_msg))
                .collect();
            self.mark_guarded_stale();
            crate::emit::rows_only(&messages);
            return Pass {
                messages,
                events: Vec::new(),
                parsed: 0,
            };
        }
        #[cfg(test)]
        if confirmed_empty_store {
            self.provisional_deletions = true;
        }
        let mut live = listed.clone();
        live.extend(self.entries.keys().filter(|key| fallback_key(key)).cloned());
        if crate::emit::on() {
            // the progress denominator is parse work only: cache hits stream rows without a tick
            let n_misses = tokens
                .iter()
                .filter(|(cache_id, _, token)| {
                    !matches!(self.entries.get(&format!("{prefix}{cache_id}")), Some(e)
                        if !self.force_reparse
                            && !e.legacy_needs_reparse
                            && matches!(e.identity.as_ref(),
                                Some(CacheIdentity::Token(cached)) if cached == token.as_str()))
                })
                .count();
            crate::emit::total(n_misses);
        }
        let mut messages: Vec<Message> = partial_fallback;
        crate::emit::rows_only(&messages);
        let mut events: Vec<Event> = Vec::new();
        let mut parsed = 0usize;
        for (cache_id, session, token) in &tokens {
            let key = format!("{prefix}{cache_id}");
            if let Some(e) = self.entries.get(&key) {
                if !self.force_reparse
                    && !e.legacy_needs_reparse
                    && matches!(e.identity.as_ref(),
                        Some(CacheIdentity::Token(cached)) if cached == token)
                {
                    let start = messages.len();
                    messages.extend(e.msgs.iter().map(CMsg::to_msg));
                    crate::emit::rows_only(&messages[start..]);
                    continue;
                }
            }
            let old_event_keys = self
                .entries
                .get(&key)
                .map(|entry| entry.event_keys.clone())
                .unwrap_or_default();
            let old_message_sessions: Vec<String> = self
                .entries
                .get(&key)
                .into_iter()
                .flat_map(|entry| entry.msgs.iter())
                .map(|message| message.session.to_string())
                .collect();
            self.touched.extend(old_message_sessions);
            self.touch_event_keys(&old_event_keys);
            let (m, ev, healthy) = parse(cache_id, session).into_parsed();
            // emit only after the guards decide: a rejected parse must never stream its
            // partial rows, or the reader's dedupe would drop the served last-good copy
            if healthy.is_some_and(|outcome| !outcome.licenses_replacement()) {
                self.mark_guarded_stale();
                let malformed = matches!(healthy, Some(ReadOutcome::Invalid | ReadOutcome::Partial));
                let settled = (
                    agent.to_owned(),
                    crate::ingest::registry::token_namespace(agent, cache_id),
                    session.clone(),
                );
                if let Some(entry) = self.entries.get(&key) {
                    let start = messages.len();
                    messages.extend(entry.msgs.iter().map(CMsg::to_msg));
                    crate::emit::file_done(&messages[start..]);
                    if malformed {
                        self.settled_token_failures.insert(settled);
                    }
                    continue;
                }
                // Its database moved (a new namespace) but the conversation was cached under the
                // old one: that copy is its last good, kept under this key without an identity.
                let moved = self
                    .entries
                    .iter()
                    .filter(|(held, _)| {
                        held.starts_with(&prefix) && !listed.contains(*held) && !fallback_key(held)
                    })
                    .find(|(_, entry)| {
                        entry.msgs.iter().any(|row| row.session.as_ref() == session.as_str())
                            || entry.event_keys.iter().any(|event| event.session == *session)
                    })
                    .map(|(_, entry)| entry.clone());
                if let Some(moved) = moved {
                    let start = messages.len();
                    messages.extend(moved.msgs.iter().map(CMsg::to_msg));
                    crate::emit::file_done(&messages[start..]);
                    self.put_entry(
                        key,
                        Entry {
                            identity: None,
                            ..moved
                        },
                    );
                    if malformed {
                        self.settled_token_failures.insert(settled);
                    }
                    continue;
                }
                // No last-good copy: the read contributes only rows it vouches for, and holds
                // publication back only where a published generation may hold the conversation.
                if malformed {
                    // The token digests the conversation's stored rows: unchanged, the same
                    // failure persists however busy the rest of its database is.
                    self.failed_token_identities
                        .insert(settled.clone(), token.clone());
                    if self.token_conversation_unpublished(agent, cache_id) {
                        self.admitted_unpublished_failure = true;
                        self.settled_token_failures.insert(settled);
                    } else {
                        self.unproven_token_failures.push(settled);
                    }
                }
                if healthy != Some(ReadOutcome::Partial) || (m.is_empty() && ev.is_empty()) {
                    crate::emit::file_done(&[]);
                    continue;
                }
                // Rows a partial read vouches for publish, cached so a later failed read serves
                // them, but with no identity: every pass retries the read until it completes.
                crate::emit::file_done(&m);
                let event_keys = cached_event_keys(&ev);
                self.touch_event_keys(&event_keys);
                self.put_entry(
                    key,
                    Entry {
                        mtime: 0,
                        size: 0,
                        identity: None,
                        msgs: m.iter().map(CMsg::from).collect(),
                        event_keys,
                        legacy_had_events: false,
                        legacy_needs_reparse: false,
                    },
                );
                self.touched.insert(session.clone());
                messages.extend(m);
                events.extend(ev);
                parsed += 1;
                continue;
            }
            if m.is_empty() && ev.is_empty() {
                if let Some(e) = self.entries.get(&key).cloned() {
                    if !e.msgs.is_empty() || !e.event_keys.is_empty() || e.legacy_had_events {
                        self.mark_guarded_stale();
                        let start = messages.len();
                        messages.extend(e.msgs.iter().map(CMsg::to_msg));
                        crate::emit::file_done(&messages[start..]);
                        continue;
                    }
                }
            }
            crate::emit::file_done(&m);
            let event_keys = cached_event_keys(&ev);
            self.touch_event_keys(&event_keys);
            self.put_entry(
                key,
                Entry {
                    mtime: 0,
                    size: 0,
                    identity: Some(CacheIdentity::Token(token.clone())),
                    msgs: m.iter().map(CMsg::from).collect(),
                    event_keys,
                    legacy_had_events: false,
                    legacy_needs_reparse: false,
                },
            );
            self.touched.insert(session.clone());
            messages.extend(m);
            events.extend(ev);
            parsed += 1;
        }
        // Deleted conversations have no fresh parse to name them. Record their old session ids
        // before retain so incremental materialization removes their published rows too.
        let mut deleted_event_keys = Vec::new();
        for (key, entry) in &self.entries {
            if key.starts_with(&prefix) && !live.contains(key) {
                self.touched
                    .extend(entry.msgs.iter().map(|msg| msg.session.to_string()));
                deleted_event_keys.extend(entry.event_keys.iter().cloned());
            }
        }
        self.touch_event_keys(&deleted_event_keys);
        // In repair mode a prior entry is an event-presence sentinel even with zero cached rows;
        // since the old event stream isn't cached, its disappearance stays ambiguous until --full.
        let missing_material_token = self.entries.iter().any(|(key, entry)| {
            key.starts_with(&prefix)
                && !live.contains(key)
                && (!entry.msgs.is_empty()
                    || !entry.event_keys.is_empty()
                    || entry.legacy_had_events)
        });
        if self.repair_mode && missing_material_token && !self.stable_deletions_provable() {
            self.mark_guarded_stale();
        } else {
            #[cfg(test)]
            if self.repair_mode && missing_material_token {
                self.provisional_deletions = true;
            }
            // forget conversations no longer present (genuinely deleted while the store was open)
            self.remove_entries_where(|key, _| key.starts_with(&prefix) && !live.contains(key));
        }
        Pass {
            messages,
            events,
            parsed,
        }
    }

    pub fn stage_save(&self, path: &Path) -> anyhow::Result<StagedCache> {
        anyhow::ensure!(
            !self.write_blocked,
            "ingest cache is owned by another build; refusing write"
        );
        let stage_base = |expected| -> anyhow::Result<StagedCache> {
            let started = std::time::Instant::now();
            let bytes = encode_cache_base(&self.entries)?;
            let instance = parse_base_header(&bytes[..BASE_HEADER_LEN])
                .map_err(|refusal| anyhow::anyhow!("staged cache base unreadable: {refusal}"))?
                .instance;
            let encoded = started.elapsed();
            let tmp = staged_path(path);
            write_staged_file(&tmp, &bytes)?;
            if std::env::var_os("AGREP_DEBUG").is_some() {
                eprintln!(
                    "* [agrep cache] base encode {:.1}ms · write {:.1}ms",
                    encoded.as_secs_f64() * 1000.0,
                    (started.elapsed() - encoded).as_secs_f64() * 1000.0,
                );
            }
            Ok(StagedCache {
                kind: StagedCacheKind::Base {
                    tmp,
                    path: path.to_path_buf(),
                    journal: cache_journal_path(path),
                    instance,
                    expected,
                },
                committed: false,
            })
        };
        let CacheBacking::Delta {
            instance,
            witness,
            cursor,
        } = &self.backing
        else {
            return stage_base(None);
        };
        if !base_matches(path, *instance, witness) {
            anyhow::bail!("ingest cache base changed before staging delta");
        }
        let unowned_base = matches!(
            witness,
            BaseWitness::Legacy { .. }
                | BaseWitness::Wrapped {
                    header: BaseHeader {
                        writer_build_id: None,
                        ..
                    },
                    ..
                }
        );
        if unowned_base {
            return stage_base(Some(base_expectation(*instance, witness.clone(), cursor)?));
        }
        if self.dirty.is_empty() && self.deleted.is_empty() {
            let sequence = cursor
                .lock()
                .map_err(|_| anyhow::anyhow!("ingest cache journal state poisoned"))?
                .sequence;
            return Ok(StagedCache {
                kind: StagedCacheKind::Noop {
                    base: path.to_path_buf(),
                    head: CacheHead {
                        instance: *instance,
                        sequence,
                    },
                },
                committed: false,
            });
        }

        let state = cursor
            .lock()
            .map_err(|_| anyhow::anyhow!("ingest cache journal state poisoned"))?;
        let expected_sequence = state.sequence;
        let expected_observed_len = state.observed_len;
        let expected_valid_len = state.valid_len;
        let expected_reset = state.reset;
        let expected_frames = state.frames;
        let compact_due = !expected_reset
            && (expected_observed_len >= JOURNAL_COMPACT_AT
                || expected_frames >= JOURNAL_COMPACT_FRAMES);
        let journal_overlay = compact_due.then(|| (state.upserts.clone(), state.deletes.clone()));
        drop(state);

        let mut delta_upserts = self.dirty.clone();
        let delta_deletes = self.deleted.clone();
        for key in &delta_deletes {
            delta_upserts.remove(key);
        }
        // Any refusal to journal this save (compaction budget, or a delta too
        // large for one frame) rewrites the base against the state observed above.
        let rebase_expectation = || BaseExpectation {
            instance: *instance,
            witness: witness.clone(),
            cursor: cursor.clone(),
            sequence: expected_sequence,
            observed_len: expected_observed_len,
            valid_len: expected_valid_len,
            reset: expected_reset,
            frames: expected_frames,
        };
        let make_delta = |upserts: &HashSet<String>, deletes: &HashSet<String>| {
            let mut upsert_keys: Vec<&String> = upserts.iter().collect();
            upsert_keys.sort_unstable();
            let mut delete_keys: Vec<String> = deletes.iter().cloned().collect();
            delete_keys.sort_unstable();
            JournalDelta {
                upserts: upsert_keys
                    .into_iter()
                    .filter_map(|key| {
                        self.entries
                            .get(key)
                            .cloned()
                            .map(|entry| (key.clone(), entry))
                    })
                    .collect(),
                deletes: delete_keys,
            }
        };
        // One frame replaces the journal, but the sequence keeps counting: the head a commit
        // recorded must stay comparable with every later replay of this base.
        let next_sequence = expected_sequence
            .checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("ingest cache journal sequence overflow"))?;
        let compacted = if let Some((mut upserts, mut deletes)) = journal_overlay {
            for key in &delta_upserts {
                upserts.insert(key.clone());
                deletes.remove(key);
            }
            for key in &delta_deletes {
                upserts.remove(key);
                deletes.insert(key.clone());
            }
            if compact_size_bound(&self.entries, &upserts, &deletes) > JOURNAL_COMPACT_BUDGET {
                return stage_base(Some(rebase_expectation()));
            }
            let delta = make_delta(&upserts, &deletes);
            let Some(frame) = encode_bounded_journal_frame(0, next_sequence, &delta)? else {
                return stage_base(Some(rebase_expectation()));
            };
            let mut bytes = encode_journal_header(*instance);
            bytes.extend_from_slice(&frame);
            if bytes.len() > JOURNAL_COMPACT_BUDGET {
                return stage_base(Some(rebase_expectation()));
            }
            Some((bytes, upserts, deletes))
        } else {
            None
        };
        let (bytes, mode, staged_upserts, staged_deletes) = if expected_reset {
            let delta = make_delta(&delta_upserts, &delta_deletes);
            let Some(frame) = encode_bounded_journal_frame(0, next_sequence, &delta)? else {
                return stage_base(Some(rebase_expectation()));
            };
            let mut bytes = encode_journal_header(*instance);
            bytes.extend_from_slice(&frame);
            (bytes, JournalWrite::Replace, delta_upserts, delta_deletes)
        } else if let Some((bytes, upserts, deletes)) = compacted {
            (bytes, JournalWrite::Replace, upserts, deletes)
        } else {
            let delta = make_delta(&delta_upserts, &delta_deletes);
            let Some(frame) =
                encode_bounded_journal_frame(expected_sequence, next_sequence, &delta)?
            else {
                return stage_base(Some(rebase_expectation()));
            };
            (frame, JournalWrite::Append, delta_upserts, delta_deletes)
        };
        let journal = cache_journal_path(path);
        let tmp = staged_path(&journal);
        write_staged_file(&tmp, &bytes)?;
        Ok(StagedCache {
            kind: StagedCacheKind::Journal {
                tmp,
                base: path.to_path_buf(),
                journal,
                instance: *instance,
                witness: witness.clone(),
                cursor: cursor.clone(),
                expected_sequence,
                expected_observed_len,
                expected_valid_len,
                expected_reset,
                expected_frames,
                next_sequence,
                mode,
                upserts: staged_upserts,
                deletes: staged_deletes,
            },
            committed: false,
        })
    }

    pub fn save(&self, path: &Path) -> anyhow::Result<()> {
        self.stage_save(path)?.commit().map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    use std::collections::{HashMap, HashSet};
    use std::fs;
    use std::path::PathBuf;

    use super::{
        collect_cached, collect_cached_for, source_key, source_path_within, write_staged_bytes,
        write_staged_file_with, CMsg, IngestCache, ReadOutcome, CACHE_VERSION, STAGED_WRITE_CHUNK,
    };
    #[cfg(not(windows))]
    use super::{source_path_eq, source_relative, LegacyCacheFileV8, LegacyEntryV8};
    use crate::model::{Event, Message};

    /// The payload an older generation (18..=37, no reply shape) wrote for these entries.
    fn legacy_payload_v37(version: u32, entries: &HashMap<String, super::Entry>) -> Vec<u8> {
        let entries: HashMap<String, super::LegacyEntryV37> = entries
            .iter()
            .map(|(key, entry)| (key.clone(), super::LegacyEntryV37::downgrade(entry)))
            .collect();
        bincode::serialize(&super::LegacyCacheFileV37 { version, entries }).unwrap()
    }

    fn test_message(text: &str) -> crate::model::Message {
        crate::model::RawMessage {
            agent: "claude",
            project: "project".into(),
            session: "session".into(),
            ts: 1,
            turn: 0,
            text: text.into(),
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
            reply_shape: Default::default(),
        }
        .freeze()
    }

    fn test_event(session: &str, call_id: &str) -> crate::model::Event {
        crate::model::Event {
            agent: "claude",
            session: session.to_string(),
            ts: 1,
            kind: "tool",
            name: "shell".into(),
            input: String::new(),
            output: String::new(),
            input_chars: 0,
            output_chars: 0,
            output_bytes: 0,
            ok: Some(true),
            call_id: call_id.into(),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        }
    }

    #[test]
    fn staged_cache_bounds_each_write_and_handles_partial_writers() {
        struct PartialWriter {
            bytes: Vec<u8>,
            largest_request: usize,
        }

        impl std::io::Write for PartialWriter {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.largest_request = self.largest_request.max(bytes.len());
                let written = bytes.len().min(17);
                self.bytes.extend_from_slice(&bytes[..written]);
                Ok(written)
            }

            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }

        let expected = vec![0x5a; STAGED_WRITE_CHUNK * 2 + 31];
        let mut writer = PartialWriter {
            bytes: Vec::new(),
            largest_request: 0,
        };
        write_staged_bytes(&mut writer, &expected).unwrap();
        assert_eq!(writer.bytes, expected);
        assert_eq!(writer.largest_request, STAGED_WRITE_CHUNK);
    }

    #[test]
    fn failed_staged_write_removes_its_partial_file() {
        let path = std::env::temp_dir().join(format!(
            "agrep-failed-staged-write-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let error = write_staged_file_with(&path, |file| {
            std::io::Write::write_all(file, b"partial")?;
            Err(std::io::Error::other("injected write failure"))
        })
        .unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::Other);
        assert!(!path.exists());
    }

    #[test]
    fn preflight_paths_require_a_complete_snapshot_and_stay_scoped() {
        let root = PathBuf::from("root");
        let mut cache = IngestCache::cold();
        cache.set_source_coverage(
            HashSet::from([
                root.join("b.jsonl"),
                root.join("a.jsonl"),
                PathBuf::from("other/c.jsonl"),
            ]),
            HashMap::new(),
        );
        assert!(cache.preflight_source_paths(&root).is_none());
        cache.set_current_source_agents(HashSet::from(["claude".to_string()]));
        assert_eq!(
            cache.preflight_source_paths(&root).unwrap(),
            [root.join("a.jsonl"), root.join("b.jsonl")]
        );
    }

    #[test]
    fn empty_always_snapshot_is_not_prior_material() {
        let mut cache = IngestCache::cold();
        cache.guard_never_empty(
            "kimi",
            std::path::Path::new("/fixture-store"),
            Vec::new(),
            &[],
            ReadOutcome::Complete,
        );
        cache.set_current_source_agents(HashSet::new());
        assert!(!cache.adapter_required("kimi"));
    }

    /// A reparse-compatible upgrade flags every cached entry for reparse. An Always store's empty
    /// snapshot entry still holds nothing, so its absent store publishes; flagged material guards.
    #[test]
    fn upgraded_empty_always_snapshot_does_not_guard_an_absent_store() {
        let root = std::env::temp_dir().join(format!(
            "agrep-upgraded-empty-snapshot-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let store = root.join("store");
        let cache_path = root.join("cache.bin");
        let mut published = IngestCache::cold();
        for (agent, fresh) in [
            ("kimi", Vec::new()),
            ("cline", vec![test_message("cline row")]),
        ] {
            published.guard_never_empty(agent, &store, fresh, &[], ReadOutcome::Complete);
        }
        published.guard_never_empty(
            "antigravity",
            &store,
            Vec::new(),
            &[test_event("brain", "call")],
            ReadOutcome::Complete,
        );
        let reparse_compatible = CACHE_VERSION - 1;
        assert!(super::reparse_compatible_cache_version(reparse_compatible));
        fs::write(
            &cache_path,
            legacy_payload_v37(reparse_compatible, &published.entries),
        )
        .unwrap();

        let mut empty = IngestCache::load(&cache_path);
        let flagged = empty
            .entries
            .values()
            .all(|entry| entry.legacy_needs_reparse);
        assert!(flagged);
        let (rows, guarded) =
            empty.guard_never_empty("kimi", &store, Vec::new(), &[], ReadOutcome::Complete);
        assert!(rows.is_empty());
        assert!(!guarded, "an empty upgraded snapshot guarded");
        assert!(empty.source_snapshot_safe());
        assert!(empty.output_complete());

        for agent in ["cline", "antigravity"] {
            let mut material = IngestCache::load(&cache_path);
            let (_, guarded) =
                material.guard_never_empty(agent, &store, Vec::new(), &[], ReadOutcome::Complete);
            assert!(guarded, "{agent}: upgraded material stopped guarding");
            assert!(!material.source_snapshot_safe());
        }
        fs::remove_dir_all(root).unwrap();
    }

    fn source_stamp(path: &std::path::Path) -> crate::ingest::registry::SourceStatStamp {
        let metadata = fs::symlink_metadata(path).unwrap();
        let modified = metadata
            .modified()
            .unwrap()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap();
        #[cfg(windows)]
        let (change_token, file_identity) =
            crate::ingest::registry::metadata_change_token_with_file_identity(path, &metadata)
                .unwrap();
        #[cfg(not(windows))]
        let change_token = crate::ingest::registry::metadata_change_token(path, &metadata).unwrap();
        crate::ingest::registry::SourceStatStamp {
            mtime_ns: i64::try_from(modified.as_nanos()).unwrap_or(i64::MAX),
            len: metadata.len(),
            change_token,
            #[cfg(windows)]
            file_identity: Some(file_identity),
            content_hashed: false,
        }
    }

    /// One indexing run against a cache file, with the preflight stamp injected so a test can
    /// reproduce what Linux's coarse clock does: a same-size rewrite that keeps the stat key.
    #[cfg(not(windows))]
    fn racy_run(
        cache_path: &std::path::Path,
        source: &std::path::Path,
        stamp: &crate::ingest::registry::SourceStatStamp,
        scan_start: std::time::SystemTime,
    ) -> (Vec<String>, usize) {
        let root = source.parent().unwrap();
        let mut cache = IngestCache::load(cache_path);
        cache
            .source_stamps
            .insert(source.to_path_buf(), stamp.clone());
        cache.set_scan_start(scan_start);
        let pass = collect_cached(&mut cache, root, &[source.to_path_buf()], |path| {
            let text = fs::read_to_string(path).unwrap();
            (vec![test_message(&text)], Vec::<Event>::new())
        });
        cache.save(cache_path).unwrap();
        let texts = pass.messages.iter().map(|m| m.text.to_string()).collect();
        (texts, pass.parsed)
    }

    #[cfg(not(windows))]
    fn racy_fixture(label: &str) -> (PathBuf, PathBuf, PathBuf) {
        let root = std::env::temp_dir().join(format!(
            "agrep-racy-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(root.join("store")).unwrap();
        let source = root.join("store").join("session.jsonl");
        fs::write(&source, b"first!").unwrap();
        (root.clone(), root.join("cache.bin"), source)
    }

    /// A rewrite in the very tick the scan began keeps size, mtime and ctime. The racy entry is
    /// re-verified on the next run even though its stat key matches, then trusted while quiet.
    #[cfg(not(windows))]
    #[test]
    fn racy_entry_catches_a_same_tick_rewrite_then_is_trusted() {
        let (root, cache_path, source) = racy_fixture("same-tick");
        let stamp = source_stamp(&source);
        let stamped = std::time::UNIX_EPOCH
            + std::time::Duration::from_nanos(u64::try_from(stamp.mtime_ns).unwrap());
        let later = |secs| stamped + std::time::Duration::from_secs(secs);

        assert_eq!(
            racy_run(&cache_path, &source, &stamp, stamped),
            (vec!["first!".to_string()], 1)
        );
        fs::write(&source, b"second").unwrap();
        assert_eq!(
            racy_run(&cache_path, &source, &stamp, later(10)),
            (vec!["second".to_string()], 1),
            "a same-tick same-size rewrite was served from the cache"
        );
        assert_eq!(
            racy_run(&cache_path, &source, &stamp, later(20)),
            (vec!["second".to_string()], 0),
            "a quiet, re-verified file was not trusted"
        );
        let _ = fs::remove_dir_all(root);
    }

    /// Racy protection costs nothing for a file whose stamp predates the scan: it is trusted
    /// on the very next run, so a quiescent store never re-verifies anything.
    #[cfg(not(windows))]
    #[test]
    fn settled_stamp_is_trusted_on_the_next_run() {
        let (root, cache_path, source) = racy_fixture("settled");
        let stamp = source_stamp(&source);
        let after = std::time::UNIX_EPOCH
            + std::time::Duration::from_nanos(u64::try_from(stamp.mtime_ns).unwrap())
            + std::time::Duration::from_secs(10);
        assert_eq!(racy_run(&cache_path, &source, &stamp, after).1, 1);
        assert_eq!(racy_run(&cache_path, &source, &stamp, after).1, 0);
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(not(windows))]
    #[test]
    fn racy_window_tracks_timestamp_granularity_and_ignores_far_future_stamps() {
        let second = 1_000_000_000_i64;
        let start = 1_800_000_000 * second + 123_456_789;
        let now = start + second;
        assert!(!super::racy_stamp(start - 60 * second, start, now));
        assert!(super::racy_stamp(start, start, now));
        assert!(super::racy_stamp(start + 1_000, start, now));
        // Whole seconds may be FAT's 2 s granules: a later write can share them for two seconds.
        assert!(super::racy_stamp(1_800_000_000 * second, start, now));
        assert!(super::racy_stamp(1_799_999_999 * second, start, now));
        assert!(!super::racy_stamp(1_799_999_990 * second, start, now));
        // A fine stamp a whole second before the scan is settled on every clock in use.
        assert!(!super::racy_stamp(start - second, start, now));
        assert!(!super::racy_stamp(now + 3_600 * second, start, now));
    }

    #[test]
    fn cache_digest_fixed_partition_binds_mutation_truncation_and_leaf_order() {
        let chunk = super::CACHE_DIGEST_CHUNK;
        let mut payload = vec![1_u8; chunk * 2 + 37];
        payload[chunk..chunk * 2].fill(2);
        payload[chunk * 2..].fill(3);
        let expected = super::cache_digest(&payload);

        let mut mutated = payload.clone();
        mutated[chunk + 11] ^= 0x80;
        assert_ne!(super::cache_digest(&mutated), expected);

        let mut truncated = payload.clone();
        truncated.pop();
        assert_ne!(super::cache_digest(&truncated), expected);

        let mut reordered = payload.clone();
        reordered[..chunk * 2].rotate_left(chunk);
        assert_ne!(super::cache_digest(&reordered), expected);
    }

    #[test]
    fn cache_base_v4_header_rejects_invalid_codec_lengths_and_reserved_bits() {
        let encoded = super::encode_cache_base(&HashMap::new()).unwrap();
        let header = super::parse_base_header(&encoded).unwrap();
        assert_eq!(header.storage_version, super::CACHE_BASE_STORAGE_VERSION);
        assert_eq!(
            header.writer_build_id,
            Some(super::WriterBuildId::current())
        );
        assert_eq!(
            encoded.len() as u64,
            header.header_len as u64 + header.stored_len
        );

        let mut invalid = encoded.clone();
        invalid[super::BASE_V4_CODEC_OFFSET..super::BASE_V4_CODEC_OFFSET + 4]
            .copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(super::parse_base_header(&invalid).is_err());

        let mut invalid = encoded.clone();
        invalid[super::BASE_V4_RESERVED_OFFSET..super::BASE_V4_RESERVED_OFFSET + 4]
            .copy_from_slice(&1_u32.to_le_bytes());
        assert!(super::parse_base_header(&invalid).is_err());

        let mut invalid = encoded;
        invalid[super::BASE_V4_STORED_LEN_OFFSET..super::BASE_V4_STORED_LEN_OFFSET + 8]
            .copy_from_slice(&0_u64.to_le_bytes());
        assert!(super::parse_base_header(&invalid).is_err());

        invalid[super::BASE_V4_RAW_LEN_OFFSET..super::BASE_V4_RAW_LEN_OFFSET + 8]
            .copy_from_slice(&u64::MAX.to_le_bytes());
        invalid[super::BASE_V4_CODEC_OFFSET..super::BASE_V4_CODEC_OFFSET + 4]
            .copy_from_slice(&super::CACHE_CODEC_XPRESS.to_le_bytes());
        invalid[super::BASE_V4_STORED_LEN_OFFSET..super::BASE_V4_STORED_LEN_OFFSET + 8]
            .copy_from_slice(&1_u64.to_le_bytes());
        assert!(super::parse_base_header(&invalid).is_err());

        invalid[super::BASE_V4_RAW_LEN_OFFSET..super::BASE_V4_RAW_LEN_OFFSET + 8]
            .copy_from_slice(&(70_u64 * 1024 * 1024).to_le_bytes());
        invalid[super::BASE_V4_STORED_LEN_OFFSET..super::BASE_V4_STORED_LEN_OFFSET + 8]
            .copy_from_slice(&(3_u64 * 1024 * 1024).to_le_bytes());
        assert!(super::parse_base_header(&invalid).is_ok());
    }

    #[test]
    fn cache_base_v4_payload_corruption_is_a_cache_miss() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-storage-v3-corrupt-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let mut encoded = super::encode_cache_base(&HashMap::new()).unwrap();
        let last = encoded.last_mut().unwrap();
        *last ^= 0x80;
        fs::write(&cache_path, encoded).unwrap();

        let loaded = IngestCache::load(&cache_path);
        assert!(!loaded.warm);
        assert!(loaded.entries.is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn writer_build_id_is_exact_bounded_hex() {
        let id = super::current_cache_writer_build_id();
        assert_eq!(id.len(), super::WRITER_BUILD_ID_LEN);
        assert!(id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f')));
    }

    #[test]
    fn cache_owner_probe_classifies_current_foreign_and_legacy_without_payload_decode() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-owner-probe-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let writer_a = super::WriterBuildId::parse(b"aaaaaaaaaaaaaaaaaaaa").unwrap();
        let writer_b = super::WriterBuildId::parse(b"bbbbbbbbbbbbbbbbbbbb").unwrap();
        let mut encoded = super::encode_cache_base_for(&HashMap::new(), writer_a).unwrap();
        // Ownership is a prefix decision: even an incompatible semantic version and invalid
        // payload remain foreign to B before either format is decoded.
        encoded[0..4].copy_from_slice(&(CACHE_VERSION + 7).to_le_bytes());
        *encoded.last_mut().unwrap() ^= 0x80;
        fs::write(&cache_path, &encoded).unwrap();

        assert_eq!(
            super::probe_cache_owner_for(&cache_path, writer_a),
            super::CacheOwnerProbe::Current {
                build_id: "aaaaaaaaaaaaaaaaaaaa".to_string()
            }
        );
        assert_eq!(
            super::probe_cache_owner_for(&cache_path, writer_b),
            super::CacheOwnerProbe::Foreign {
                build_id: "aaaaaaaaaaaaaaaaaaaa".to_string()
            }
        );
        assert!(matches!(
            super::decode_cache_for(&cache_path, writer_b),
            Err(super::CacheDecodeRefusal::ForeignOwner)
        ));
        assert!(matches!(
            super::decode_cache_for(&cache_path, writer_a),
            Err(super::CacheDecodeRefusal::StorageVersion)
        ));

        let missing = root.join("missing.bin");
        assert_eq!(
            super::probe_cache_owner_for(&missing, writer_a),
            super::CacheOwnerProbe::Missing
        );
        fs::write(&cache_path, b"legacy-unwrapped-cache").unwrap();
        assert_eq!(
            super::probe_cache_owner_for(&cache_path, writer_a),
            super::CacheOwnerProbe::LegacyUnowned
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn foreign_cache_load_and_repair_cannot_stage_a_rewrite() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-owner-block-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let writer_a = super::WriterBuildId::parse(b"aaaaaaaaaaaaaaaaaaaa").unwrap();
        let writer_b = super::WriterBuildId::parse(b"bbbbbbbbbbbbbbbbbbbb").unwrap();
        let encoded = super::encode_cache_base_for(&HashMap::new(), writer_a).unwrap();
        fs::write(&cache_path, &encoded).unwrap();

        let loaded = IngestCache::load_for_writer(&cache_path, writer_b);
        assert!(loaded.write_blocked);
        assert!(!loaded.warm);
        assert!(loaded.stage_save(&cache_path).is_err());

        let (repair, refusal) = IngestCache::repair_for_writer(&cache_path, writer_b);
        assert_eq!(refusal, Some(super::CacheDecodeRefusal::ForeignOwner));
        assert!(repair.write_blocked);
        assert!(repair.stage_save(&cache_path).is_err());

        let (retry, refusal) = IngestCache::guarded_retry_for_writer(&cache_path, writer_b);
        assert_eq!(refusal, Some(super::CacheDecodeRefusal::ForeignOwner));
        assert!(retry.write_blocked);
        assert!(retry.stage_save(&cache_path).is_err());
        assert_eq!(fs::read(&cache_path).unwrap(), encoded);
        assert!(!super::cache_journal_path(&cache_path).exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn same_owner_corruption_remains_repairable() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-owner-repair-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let writer = super::WriterBuildId::current();
        let mut encoded = super::encode_cache_base_for(&HashMap::new(), writer).unwrap();
        *encoded.last_mut().unwrap() ^= 0x80;
        fs::write(&cache_path, encoded).unwrap();

        let loaded = IngestCache::load_for_writer(&cache_path, writer);
        assert!(!loaded.write_blocked);
        assert!(!loaded.warm);
        loaded.save(&cache_path).unwrap();
        assert_eq!(
            super::probe_cache_owner_for(&cache_path, writer),
            super::CacheOwnerProbe::Current {
                build_id: writer.as_str().to_string()
            }
        );
        assert!(IngestCache::load_for_writer(&cache_path, writer).warm);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn repair_refusal_names_failed_decode_check() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-refusal-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");

        let (_, refusal) = IngestCache::repair(&cache_path);
        assert_eq!(refusal, Some(super::CacheDecodeRefusal::MissingFile));

        let encoded = super::encode_cache_base(&HashMap::new()).unwrap();

        let mut corrupt = encoded.clone();
        *corrupt.last_mut().unwrap() ^= 0x80;
        fs::write(&cache_path, &corrupt).unwrap();
        let (_, refusal) = IngestCache::repair(&cache_path);
        assert_eq!(
            refusal,
            Some(super::CacheDecodeRefusal::PayloadDigestMismatch)
        );
        assert_eq!(refusal.unwrap().to_string(), "cache digest mismatch");

        let mut truncated = encoded.clone();
        truncated.pop();
        fs::write(&cache_path, &truncated).unwrap();
        let (_, refusal) = IngestCache::repair(&cache_path);
        assert_eq!(refusal, Some(super::CacheDecodeRefusal::BaseLengthMismatch));

        let mut versioned = encoded;
        versioned[0..4].copy_from_slice(&(CACHE_VERSION + 7).to_le_bytes());
        fs::write(&cache_path, &versioned).unwrap();
        let (_, refusal) = IngestCache::repair(&cache_path);
        assert_eq!(refusal, Some(super::CacheDecodeRefusal::StorageVersion));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn wrapped_storage_v2_remains_readable() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-storage-v2-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let entries = HashMap::new();
        let payload = bincode::serialize(&super::CacheFileRef {
            version: CACHE_VERSION,
            entries: &entries,
        })
        .unwrap();
        let mut wrapped = Vec::with_capacity(super::BASE_HEADER_V2_LEN + payload.len());
        wrapped.extend_from_slice(&CACHE_VERSION.to_le_bytes());
        wrapped.extend_from_slice(&0_u64.to_le_bytes());
        wrapped.extend_from_slice(super::CACHE_BASE_MAGIC);
        wrapped.extend_from_slice(&super::CACHE_BASE_STORAGE_VERSION_V2.to_le_bytes());
        wrapped.extend_from_slice(&[7_u8; 16]);
        wrapped.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        wrapped.extend_from_slice(&super::cache_digest(&payload));
        wrapped.extend_from_slice(&payload);
        fs::write(&cache_path, wrapped).unwrap();

        let loaded = IngestCache::load(&cache_path);
        assert!(loaded.warm);
        assert!(loaded.entries.is_empty());
        let stage = loaded.stage_save(&cache_path).unwrap();
        assert!(matches!(&stage.kind, super::StagedCacheKind::Base { .. }));
        stage.commit().unwrap();
        assert!(matches!(
            super::probe_cache_owner(&cache_path),
            super::CacheOwnerProbe::Current { .. }
        ));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn wrapped_storage_v3_remains_readable_and_unowned() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-storage-v3-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let entries = HashMap::new();
        let payload = bincode::serialize(&super::CacheFileRef {
            version: CACHE_VERSION,
            entries: &entries,
        })
        .unwrap();
        let mut wrapped = Vec::with_capacity(super::BASE_HEADER_V3_LEN + payload.len());
        wrapped.extend_from_slice(&CACHE_VERSION.to_le_bytes());
        wrapped.extend_from_slice(&0_u64.to_le_bytes());
        wrapped.extend_from_slice(super::CACHE_BASE_MAGIC);
        wrapped.extend_from_slice(&super::CACHE_BASE_STORAGE_VERSION_V3.to_le_bytes());
        wrapped.extend_from_slice(&[7_u8; 16]);
        wrapped.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        wrapped.extend_from_slice(&super::cache_digest(&payload));
        wrapped.extend_from_slice(&super::CACHE_CODEC_NONE.to_le_bytes());
        wrapped.extend_from_slice(&0_u32.to_le_bytes());
        wrapped.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        wrapped.extend_from_slice(&payload);
        fs::write(&cache_path, wrapped).unwrap();

        assert_eq!(
            super::probe_cache_owner(&cache_path),
            super::CacheOwnerProbe::LegacyUnowned
        );
        let loaded = IngestCache::load(&cache_path);
        assert!(loaded.warm);
        assert!(loaded.entries.is_empty());
        assert!(!loaded.write_blocked);
        let stage = loaded.stage_save(&cache_path).unwrap();
        assert!(matches!(&stage.kind, super::StagedCacheKind::Base { .. }));
        stage.commit().unwrap();
        assert!(matches!(
            super::probe_cache_owner(&cache_path),
            super::CacheOwnerProbe::Current { .. }
        ));
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn xpress_cache_payload_round_trips_and_rejects_corruption() {
        let raw = b"repeated transcript cache payload ".repeat(32_768);
        let mut compressed = super::compress_cache_payload(&raw).unwrap();
        assert!(compressed.len() < raw.len());
        assert_eq!(
            super::decompress_cache_payload(&compressed, raw.len()).as_deref(),
            Some(raw.as_slice())
        );

        let middle = compressed.len() / 2;
        compressed[middle] ^= 0x80;
        assert_ne!(
            super::decompress_cache_payload(&compressed, raw.len()).as_deref(),
            Some(raw.as_slice())
        );
    }

    #[test]
    fn wrapped_storage_v1_fails_closed() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-storage-v1-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"source").unwrap();

        let mut seed = IngestCache::cold();
        collect_cached(&mut seed, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("storage-v1")], Vec::new())
        });
        let payload = bincode::serialize(&super::CacheFileRef {
            version: CACHE_VERSION,
            entries: &seed.entries,
        })
        .unwrap();
        let mut wrapped = Vec::with_capacity(super::BASE_HEADER_LEN + payload.len());
        wrapped.extend_from_slice(&CACHE_VERSION.to_le_bytes());
        wrapped.extend_from_slice(&0_u64.to_le_bytes());
        wrapped.extend_from_slice(super::CACHE_BASE_MAGIC);
        wrapped.extend_from_slice(&1_u32.to_le_bytes());
        wrapped.extend_from_slice(&[7_u8; 16]);
        wrapped.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        wrapped.extend_from_slice(&md5::compute(&payload).0);
        wrapped.extend_from_slice(&payload);
        fs::write(&cache_path, wrapped).unwrap();

        let loaded = IngestCache::load(&cache_path);
        assert!(!loaded.warm);
        assert!(loaded.entries.is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    fn enable_case_sensitive(path: &std::path::Path) {
        use std::os::windows::fs::OpenOptionsExt;
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            FileCaseSensitiveInfo, SetFileInformationByHandle, FILE_CASE_SENSITIVE_INFO,
            FILE_FLAG_BACKUP_SEMANTICS, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ,
            FILE_SHARE_WRITE, FILE_WRITE_ATTRIBUTES,
        };
        use windows_sys::Win32::System::SystemServices::FILE_CS_FLAG_CASE_SENSITIVE_DIR;

        let directory = fs::OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
            .open(path)
            .unwrap();
        let info = FILE_CASE_SENSITIVE_INFO {
            Flags: FILE_CS_FLAG_CASE_SENSITIVE_DIR,
        };
        let enabled = unsafe {
            SetFileInformationByHandle(
                directory.as_raw_handle(),
                FileCaseSensitiveInfo,
                (&info as *const FILE_CASE_SENSITIVE_INFO).cast(),
                std::mem::size_of::<FILE_CASE_SENSITIVE_INFO>() as u32,
            )
        };
        assert_ne!(enabled, 0, "{}", std::io::Error::last_os_error());
    }

    // Every registered adapter's messages must survive a cache round-trip with their agent
    // intact - a name that interns to "unknown" is silent relabeling (the cursor bug).
    #[test]
    fn intern_covers_every_adapter() {
        for a in crate::ingest::registry::ADAPTERS {
            assert_eq!(super::intern_agent(a.name()), a.name());
        }
    }

    #[test]
    fn token_cache_persists_and_compares_the_full_identity() {
        let root =
            std::env::temp_dir().join(format!("agrep-exact-token-cache-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let path = root.join("cache.bin");
        let token = format!("x:{}a", "0123456789abcdef".repeat(8));
        let mut cache = IngestCache::cold();

        let first = cache.collect_token_cached(
            "crush",
            Some(vec![("session".into(), token.clone())]),
            |_| (vec![test_message("fresh")], Vec::new()),
        );
        cache.save(&path).unwrap();
        let mut warm = IngestCache::load(&path);
        let same = warm.collect_token_cached(
            "crush",
            Some(vec![("session".into(), token.clone())]),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!("exact token should cache-hit") },
        );
        let changed = warm.collect_token_cached(
            "crush",
            Some(vec![(
                "session".into(),
                format!("{}b", &token[..token.len() - 1]),
            )]),
            |_| (vec![test_message("changed")], Vec::new()),
        );

        assert_eq!((first.parsed, same.parsed, changed.parsed), (1, 0, 1));
        assert_eq!(changed.messages[0].text.as_ref(), "changed");
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(any(unix, windows))]
    #[test]
    fn cache_reparses_same_size_edit_with_restored_mtime() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let root = std::env::temp_dir().join(format!(
            "agrep-cache-restored-mtime-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        fs::write(&source, b"before").unwrap();
        let modified = fs::metadata(&source).unwrap().modified().unwrap();
        let calls = AtomicUsize::new(0);
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            calls.fetch_add(1, Ordering::Relaxed);
            (vec![test_message("before")], Vec::new())
        });
        crate::ingest::registry::wait_for_next_ctime_tick(&source);

        fs::write(&source, b"after!").unwrap();
        fs::OpenOptions::new()
            .write(true)
            .open(&source)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(modified))
            .unwrap();
        cache
            .source_stamps
            .insert(source.clone(), source_stamp(&source));
        let pass = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            calls.fetch_add(1, Ordering::Relaxed);
            (vec![test_message("after!")], Vec::new())
        });
        assert_eq!(calls.load(Ordering::Relaxed), 2);
        assert_eq!(pass.parsed, 1);
        assert_eq!(pass.messages[0].text.as_ref(), "after!");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn a_retired_source_is_forgotten_like_a_deletion_instead_of_re_added() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-retired-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let legacy = root.join("session.json");
        let current = root.join("session.jsonl");
        fs::write(&legacy, b"legacy").unwrap();
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&legacy), |_| {
            (vec![test_message("legacy row")], Vec::<Event>::new())
        });
        // a present cached file the walker did not list is served again, not dropped
        let omitted = collect_cached(&mut cache, &root, &[], |_| {
            (vec![test_message("legacy row")], Vec::<Event>::new())
        });
        assert_eq!(omitted.messages.len(), 1);

        fs::write(&current, b"current").unwrap();
        let retired = std::collections::HashSet::from([legacy.clone()]);
        let pass = super::collect_cached_retiring_for(
            &mut cache,
            "ingest",
            &root,
            std::slice::from_ref(&current),
            &retired,
            |_| (vec![test_message("current row")], Vec::<Event>::new()),
        );
        let texts: Vec<&str> = pass.messages.iter().map(|m| m.text.as_ref()).collect();
        assert_eq!(texts, vec!["current row"]);
        assert!(!cache.entries.contains_key(&source_key(&legacy)));
        assert!(cache.entries.contains_key(&source_key(&current)));
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(any(unix, windows))]
    #[test]
    fn exact_preflight_stamp_bypasses_restat_but_missing_stamp_does_not() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-preflight-stamp-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        fs::write(&source, b"source").unwrap();
        let stamp = source_stamp(&source);
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("last good")], Vec::new())
        });
        cache.source_stamps.insert(source.clone(), stamp);
        fs::remove_file(&source).unwrap();

        let hit = collect_cached(
            &mut cache,
            &root,
            std::slice::from_ref(&source),
            |_| -> (Vec<Message>, Vec<Event>) { panic!("exact preflight hit reparsed") },
        );
        assert_eq!(hit.parsed, 0);
        assert_eq!(hit.messages[0].text.as_ref(), "last good");
        assert!(cache.source_snapshot_safe());

        cache.source_stamps.clear();
        let fallback = collect_cached(
            &mut cache,
            &root,
            std::slice::from_ref(&source),
            |_| -> (Vec<Message>, Vec<Event>) { panic!("missing file was parsed") },
        );
        assert_eq!(fallback.messages[0].text.as_ref(), "last good");
        assert!(!cache.source_snapshot_safe());
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn omitted_cached_symlink_serves_last_good_without_following() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!(
            "agrep-cache-symlink-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let outside = root.with_extension("outside.jsonl");
        fs::write(&source, b"inside").unwrap();
        fs::write(&outside, b"outside secret").unwrap();
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("last good")], Vec::new())
        });

        fs::remove_file(&source).unwrap();
        symlink(&outside, &source).unwrap();
        let pass = collect_cached(&mut cache, &root, &[], |_| -> (Vec<Message>, Vec<Event>) {
            panic!("cached symlink was parsed")
        });
        assert_eq!(pass.messages.len(), 1);
        assert_eq!(pass.messages[0].text.as_ref(), "last good");
        assert!(!cache.source_snapshot_safe());

        let _ = fs::remove_file(outside);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn explicit_sqlite_file_root_accepts_only_database_and_wal() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let dir = std::env::temp_dir().join(format!(
            "agrep-cache-file-root-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let database = dir.join("opencode.db");
        let sibling = dir.join("opencode.db-backup");
        fs::write(&database, b"database").unwrap();
        fs::write(&sibling, b"sibling").unwrap();
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &dir, std::slice::from_ref(&sibling), |_| {
            (vec![test_message("sibling")], Vec::new())
        });

        let calls = AtomicUsize::new(0);
        let parse = |_: &std::path::Path| {
            calls.fetch_add(1, Ordering::Relaxed);
            (vec![test_message("database")], Vec::new())
        };
        let first = collect_cached(
            &mut cache,
            &database,
            std::slice::from_ref(&database),
            parse,
        );
        assert_eq!(first.parsed, 1);
        cache
            .source_stamps
            .insert(database.clone(), source_stamp(&database));
        let hit = collect_cached(
            &mut cache,
            &database,
            std::slice::from_ref(&database),
            parse,
        );
        assert_eq!(hit.parsed, 0);

        fs::write(crate::ingest::sqlite_sidecar(&database, "-wal"), b"wal").unwrap();
        let changed = collect_cached(
            &mut cache,
            &database,
            std::slice::from_ref(&database),
            parse,
        );
        assert_eq!(changed.parsed, 1);
        assert_eq!(calls.load(Ordering::Relaxed), 2);
        assert!(cache.entries.contains_key(&source_key(&sibling)));
        assert!(super::source_metadata(&database, &sibling).is_err());
        let _ = fs::remove_dir_all(dir);
    }

    /// Regression: a Windows share lock can allow metadata reads while the subsequent source
    /// read fails. The last-good rows are correct for this run, but the old cache key must be
    /// retained and source-snapshot publication forbidden so the unlocked next run retries.
    #[test]
    fn guarded_empty_reparse_disables_snapshot_and_retries_next_run() {
        let root = std::env::temp_dir().join(format!(
            "agrep-guarded-reparse-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"old").unwrap();

        let mut cache = IngestCache::cold();
        let first = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("last good")], Vec::new())
        });
        assert_eq!(first.messages[0].text.as_ref(), "last good");
        assert!(cache.source_snapshot_safe());

        // A size change guarantees a cache miss even on coarse-mtime filesystems. Returning
        // no rows simulates the adapter's lossy/open path seeing a transient share violation.
        fs::write(&source, b"changed while locked").unwrap();
        let guarded = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new())
        });
        assert_eq!(guarded.messages[0].text.as_ref(), "last good");
        assert!(!cache.source_snapshot_safe());

        // The guarded miss keeps the old metadata in the serialized cache. A fresh process
        // therefore retries the same source after the lock clears rather than treating it as
        // a hit; its per-run publication guard starts clean once the retry succeeds.
        cache.save(&cache_path).unwrap();
        let mut retry = IngestCache::load(&cache_path);
        let recovered = collect_cached(&mut retry, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("recovered")], Vec::new())
        });
        assert_eq!(recovered.messages[0].text.as_ref(), "recovered");
        assert!(retry.source_snapshot_safe());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn policy_empty_reparse_prunes_unchanged_source_material() {
        let root = std::env::temp_dir().join(format!(
            "agrep-policy-empty-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"unchanged input").unwrap();
        let mut initial = IngestCache::cold();
        collect_cached(&mut initial, &root, std::slice::from_ref(&source), |_| {
            (
                vec![test_message("excluded by current policy")],
                vec![test_event("session", "old-call")],
                ReadOutcome::Complete,
            )
        });
        initial.save(&cache_path).unwrap();

        let (mut repair, refusal) = IngestCache::repair(&cache_path);
        assert!(refusal.is_none());
        repair.set_repair_expectations(
            HashSet::from(["claude".to_string()]),
            HashSet::from([source.clone()]),
        );
        let pass = collect_cached(&mut repair, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new(), ReadOutcome::Complete)
        });
        assert!(pass.messages.is_empty());
        assert!(pass.events.is_empty());
        assert!(repair.source_snapshot_safe());
        assert_eq!(
            repair.event_prune_files(&repair.live_event_files()),
            HashSet::from([crate::cache::event_fname("claude", "session")])
        );
        repair.save(&cache_path).unwrap();
        let mut reloaded = IngestCache::load(&cache_path);
        let pass = collect_cached(
            &mut reloaded,
            &root,
            std::slice::from_ref(&source),
            |_| -> (Vec<Message>, Vec<Event>) { panic!("completed empty read was not cached") },
        );
        assert!(pass.messages.is_empty());
        assert!(reloaded.source_snapshot_safe());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn policy_empty_reparse_preserves_replaced_source_material() {
        let root = std::env::temp_dir().join(format!(
            "agrep-policy-replacement-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        fs::write(&source, b"old").unwrap();
        let original_mtime = fs::metadata(&source).unwrap().modified().unwrap();
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (
                vec![test_message("last good")],
                vec![test_event("session", "last-call")],
                ReadOutcome::Complete,
            )
        });
        crate::ingest::registry::wait_for_next_ctime_tick(&source);
        let replacement = root.join("replacement.jsonl");
        fs::write(&replacement, b"new").unwrap();
        fs::File::options()
            .write(true)
            .open(&replacement)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(original_mtime))
            .unwrap();
        fs::remove_file(&source).unwrap();
        fs::rename(&replacement, &source).unwrap();

        let pass = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new(), ReadOutcome::Complete)
        });
        assert_eq!(pass.messages[0].text.as_ref(), "last good");
        assert!(!cache.source_snapshot_safe());
        assert!(cache
            .event_prune_files(&cache.live_event_files())
            .is_empty());
        fs::remove_dir_all(root).unwrap();
    }

    /// A read the parser proves empty (`ReadOutcome::Undone`) publishes the emptiness over
    /// last-good rows and events once the file grew, as an append-only log that recorded its own
    /// undo does; a file that shrank may be mid-rewrite, so last-good still wins there.
    #[test]
    fn undone_reparse_publishes_empty_only_after_growth() {
        let root = std::env::temp_dir().join(format!(
            "agrep-undone-empty-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        for (before, after, kept) in [
            (&b"prompt\n"[..], &b"prompt\nrewind\n"[..], false),
            (&b"prompt\nrewind\n"[..], &b"rewind\n"[..], true),
        ] {
            fs::write(&source, before).unwrap();
            let mut cache = IngestCache::cold();
            collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
                (
                    vec![test_message("rewound")],
                    vec![test_event("session", "rewound-call")],
                    ReadOutcome::Complete,
                )
            });
            fs::write(&source, after).unwrap();
            let pass = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
                (Vec::new(), Vec::new(), ReadOutcome::Undone)
            });
            assert_eq!(pass.messages.len(), usize::from(kept));
            assert!(pass.events.is_empty());
            assert_eq!(cache.source_snapshot_safe(), !kept);
            let pruned = cache.event_prune_files(&cache.live_event_files());
            assert_eq!(pruned.is_empty(), kept);
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn policy_empty_reparse_preserves_unattributed_legacy_events() {
        let root = std::env::temp_dir().join(format!(
            "agrep-policy-legacy-events-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"unchanged input").unwrap();
        let mut initial = IngestCache::cold();
        collect_cached(&mut initial, &root, std::slice::from_ref(&source), |_| {
            (
                vec![test_message("legacy material")],
                Vec::new(),
                ReadOutcome::Complete,
            )
        });
        initial
            .entries
            .values_mut()
            .next()
            .unwrap()
            .legacy_had_events = true;
        initial.save(&cache_path).unwrap();
        let (mut repair, _) = IngestCache::repair(&cache_path);
        let pass = collect_cached(&mut repair, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new(), ReadOutcome::Complete)
        });
        assert_eq!(pass.messages[0].text.as_ref(), "legacy material");
        assert!(!repair.source_snapshot_safe());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn policy_empty_sibling_does_not_block_complete_session_events() {
        let root = std::env::temp_dir().join(format!(
            "agrep-policy-sibling-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let changed = root.join("changed.jsonl");
        let sibling = root.join("sibling.jsonl");
        fs::write(&changed, b"old").unwrap();
        fs::write(&sibling, b"unchanged").unwrap();
        let files = [changed.clone(), sibling.clone()];
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, &files, |_| {
            (
                vec![test_message("old material")],
                vec![test_event("session", "old-call")],
                ReadOutcome::Complete,
            )
        });
        fs::write(&changed, b"changed and larger").unwrap();
        let pass = collect_cached(&mut cache, &root, &files, |path| {
            if path == sibling {
                (Vec::new(), Vec::new(), ReadOutcome::Complete)
            } else {
                (
                    vec![test_message("fresh")],
                    vec![test_event("session", "fresh-call")],
                    ReadOutcome::Complete,
                )
            }
        });
        assert_eq!(
            pass.messages
                .iter()
                .map(|message| message.text.as_ref())
                .collect::<Vec<_>>(),
            ["fresh"]
        );
        assert_eq!(
            pass.events
                .iter()
                .map(|event| event.call_id.as_str())
                .collect::<Vec<_>>(),
            ["fresh-call"]
        );
        assert!(cache.source_snapshot_safe());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn policy_takeover_preserves_fallback_rows_and_required_reparse() {
        let root = std::env::temp_dir().join(format!(
            "agrep-policy-adoption-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"unchanged input").unwrap();
        let mut initial = IngestCache::cold();
        collect_cached(&mut initial, &root, std::slice::from_ref(&source), |_| {
            (
                vec![test_message("last good")],
                vec![test_event("session", "old-call")],
                ReadOutcome::Complete,
            )
        });
        fs::write(
            &cache_path,
            legacy_payload_v37(CACHE_VERSION - 1, &initial.entries),
        )
        .unwrap();
        super::adopt_foreign_cache(&cache_path).unwrap();

        let mut guarded = IngestCache::load(&cache_path);
        let pass = collect_cached(&mut guarded, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new(), ReadOutcome::Invalid)
        });
        assert_eq!(pass.messages[0].text.as_ref(), "last good");
        assert!(!guarded.source_snapshot_safe());
        let mut retry = IngestCache::load(&cache_path);
        let pass = collect_cached(&mut retry, &root, std::slice::from_ref(&source), |_| {
            (
                vec![test_message("current policy")],
                vec![test_event("session", "new-call")],
                ReadOutcome::Complete,
            )
        });
        assert_eq!(pass.messages[0].text.as_ref(), "current policy");
        assert_eq!(pass.events[0].call_id, "new-call");
        assert!(retry.source_snapshot_safe());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cold_read_failure_keeps_readable_sibling_and_arms_retry() {
        let root = std::env::temp_dir().join(format!(
            "agrep-partial-cold-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let bad = root.join("bad.jsonl");
        let good = root.join("good.jsonl");
        fs::write(&bad, b"bad").unwrap();
        fs::write(&good, b"good").unwrap();

        let mut cache = IngestCache::cold();
        let pass = collect_cached_for(
            &mut cache,
            "claude",
            &root,
            &[bad.clone(), good.clone()],
            |path| {
                if path == bad {
                    crate::ingest::warn_source_skip(
                        "test",
                        path,
                        std::io::Error::from(std::io::ErrorKind::PermissionDenied),
                    );
                    (Vec::new(), Vec::new(), ReadOutcome::Skipped)
                } else {
                    (
                        vec![test_message("readable")],
                        Vec::new(),
                        ReadOutcome::Complete,
                    )
                }
            },
        );

        assert_eq!(pass.messages.len(), 1);
        assert_eq!(pass.messages[0].text.as_ref(), "readable");
        assert!(cache.output_complete());
        assert!(!cache.source_snapshot_safe());
        assert_eq!(cache.source_read_issues()[0].agent, "claude");
        assert_eq!(cache.source_read_issues()[0].path, bad);
        assert!(!cache.entries.contains_key(&source_key(&bad)));
        assert!(cache.entries.contains_key(&source_key(&good)));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn every_last_good_guard_marks_snapshot_unsafe() {
        // Always-parsed adapters keep one reserved last-good snapshot.
        let mut always = IngestCache::cold();
        let (fresh, guarded) = always.guard_never_empty(
            "gemini",
            std::path::Path::new("/fixture-store"),
            vec![test_message("good")],
            &[],
            ReadOutcome::Complete,
        );
        assert_eq!(fresh.len(), 1);
        assert!(!guarded);
        assert!(always.source_snapshot_safe());
        let (served, guarded) = always.guard_never_empty(
            "gemini",
            std::path::Path::new("/fixture-store"),
            Vec::new(),
            &[],
            ReadOutcome::Complete,
        );
        assert_eq!(served.len(), 1);
        assert!(guarded);
        assert!(!always.source_snapshot_safe());

        // A partial cold read publishes only past an inventory proving nothing was at stake.
        let mut unknown = IngestCache::cold();
        unknown.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            vec![test_message("readable sibling")],
            &[],
            ReadOutcome::Skipped,
        );
        assert!(!unknown.output_complete());
        let mut partial = IngestCache::cold();
        partial.set_published_material(HashSet::new());
        let (readable, guarded) = partial.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            vec![test_message("readable sibling")],
            &[],
            ReadOutcome::Skipped,
        );
        assert_eq!(readable.len(), 1);
        assert!(!guarded);
        assert!(partial.output_complete());
        assert!(!partial.source_snapshot_safe());

        let mut merged = IngestCache::cold();
        let mut old_a = test_message("old a");
        old_a.session = "a".into();
        let mut old_b = test_message("old b");
        old_b.session = "b".into();
        merged.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            vec![old_a, old_b],
            &[test_event("a", "old-a"), test_event("b", "old-b")],
            ReadOutcome::Complete,
        );
        let mut new_a = test_message("new a");
        new_a.session = "a".into();
        let (messages, suppress_events) = merged.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            vec![new_a],
            &[test_event("a", "new-a")],
            ReadOutcome::Skipped,
        );
        let by_session: HashMap<_, _> = messages
            .iter()
            .map(|message| (message.session.as_ref(), message.text.as_ref()))
            .collect();
        assert_eq!(by_session.get("a"), Some(&"new a"));
        assert_eq!(by_session.get("b"), Some(&"old b"));
        assert!(!suppress_events);
        assert!(merged.touched.contains("a"));
        assert!(!merged.source_snapshot_safe());
        let mut no_event_a = test_message("newer a");
        no_event_a.session = "a".into();
        let (_, suppress_events) = merged.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            vec![no_event_a],
            &[],
            ReadOutcome::Skipped,
        );
        assert!(!suppress_events);
        assert!(merged
            .event_prune_files(&merged.live_event_files())
            .contains(&crate::cache::event_fname("claude", "a")));

        let mut invalid = IngestCache::cold();
        let (_, guarded) = invalid.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            Vec::new(),
            &[],
            ReadOutcome::Invalid,
        );
        assert!(guarded);
        assert!(!invalid.output_complete());

        // Token stores retain either one failed changed conversation or the whole store when
        // opening/querying the database fails between preflight and collection.
        let mut token = IngestCache::cold();
        token.collect_token_cached(
            "cursor",
            Some(vec![("session".into(), "token-1".into())]),
            |_| (vec![test_message("good")], Vec::new()),
        );
        assert!(token.source_snapshot_safe());
        token.collect_token_cached(
            "cursor",
            Some(vec![("session".into(), "token-2".into())]),
            |_| (Vec::new(), Vec::new()),
        );
        assert!(!token.source_snapshot_safe());

        let mut unavailable = IngestCache::cold();
        unavailable.collect_token_cached(
            "cursor",
            Some(vec![("session".into(), "token-1".into())]),
            |_| (vec![test_message("good")], Vec::new()),
        );
        assert!(unavailable.source_snapshot_safe());
        unavailable.collect_token_cached("cursor", None, |_| -> (Vec<Message>, Vec<Event>) {
            unreachable!()
        });
        assert!(!unavailable.source_snapshot_safe());
        assert!(unavailable.output_complete());

        let mut first_unavailable = IngestCache::cold();
        first_unavailable.collect_token_cached("cursor", None, |_| -> (Vec<Message>, Vec<Event>) {
            unreachable!()
        });
        assert!(!first_unavailable.source_snapshot_safe());
        assert!(!first_unavailable.output_complete());

        // A store no read could list, with no last-good copy, holds publication back unless the
        // inventory proves that the scope its read failed on held nothing published.
        let database = std::path::Path::new("/fixture/Cursor/User/globalStorage/state.vscdb");
        let unlistable = |published: Option<HashSet<PathBuf>>,
                          namespaces: Option<HashSet<(String, String)>>,
                          recorded: bool| {
            let mut cache = IngestCache::cold();
            if let Some(published) = published {
                cache.set_published_material(published);
            }
            cache.set_published_token_namespaces(namespaces);
            if recorded {
                cache.record_source_read_issue("cursor", database, "source-read-failed", "locked");
            }
            cache.collect_token_cached("cursor", None, |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!()
            });
            assert!(!cache.source_snapshot_safe());
            cache.output_complete()
        };
        let cursor_recorded = || Some(HashSet::from([("cursor".to_string(), String::new())]));
        let crush_recorded = || Some(HashSet::from([("crush".to_string(), String::new())]));
        // A first index publishes nothing it could drop.
        assert!(unlistable(Some(HashSet::new()), Some(HashSet::new()), true));
        assert!(unlistable(Some(HashSet::new()), crush_recorded(), true));
        // Published rows of the store, a listed database, no inventory or no named scope.
        assert!(!unlistable(Some(HashSet::new()), cursor_recorded(), true));
        assert!(!unlistable(Some(HashSet::from([database.to_path_buf()])), Some(HashSet::new()), true));
        assert!(!unlistable(None, Some(HashSet::new()), true));
        assert!(!unlistable(Some(HashSet::new()), None, true));
        assert!(!unlistable(Some(HashSet::new()), Some(HashSet::new()), false));

        // A temporarily unreadable root can make discovery return no files at all.
        let root = std::env::temp_dir().join(format!(
            "agrep-empty-discovery-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        fs::write(&source, b"source").unwrap();
        let mut missing = IngestCache::cold();
        collect_cached(&mut missing, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("good")], Vec::new())
        });
        assert!(missing.source_snapshot_safe());
        fs::remove_dir_all(&root).unwrap();
        let served = collect_cached(
            &mut missing,
            &root,
            &[],
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert_eq!(served.messages.len(), 1);
        assert!(!missing.source_snapshot_safe());
    }

    /// The published snapshot lists a file its pass failed to read; a decoded base holding no
    /// entry for it proves that generation took nothing from it, so a later failure is covered.
    #[test]
    fn a_decoded_base_proves_a_listed_but_never_cached_file_unpublished() {
        let failed = std::path::Path::new("/fixture/.local/share/opencode/opencode.db");
        struct Base {
            decoded: bool,
            legacy: bool,
            cached: bool,
            sealed: bool,
            released: bool,
        }
        let current = Base {
            decoded: true,
            legacy: false,
            cached: false,
            sealed: true,
            released: false,
        };
        let covered = |base: Base| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = base.decoded;
            cache.legacy_generation = base.legacy;
            cache.published_seal = base.sealed.then_some([7; 16]);
            if base.released {
                let mut killed = super::ReleasedRows::default();
                killed.sessions.insert("moved-away".to_owned());
                cache.released.insert("opencode".to_owned(), killed);
            }
            cache.set_published_material(HashSet::from([failed.to_path_buf()]));
            if base.cached {
                let key = failed.to_string_lossy().into_owned();
                cache.put_entry(key, cached_entry(&[test_message("published row")]));
            }
            cache.unreadable_scope_covered("opencode", failed)
        };
        assert!(covered(Base { ..current }));
        assert!(!covered(Base { cached: true, ..current }));
        // No base, or an older one whose partial reads published uncached, proves nothing.
        assert!(!covered(Base { decoded: false, ..current }));
        assert!(!covered(Base { legacy: true, ..current }));
        // Nor one not sealed against the published generation, or one a killed pass moved past
        // it: the rows that pass saw go are still published.
        assert!(!covered(Base { sealed: false, ..current }));
        assert!(!covered(Base { released: true, ..current }));
    }

    /// A crush database that moved keys its conversations under a new namespace: one that fails
    /// to parse there is served from its copy under the old one, which it keeps from then on.
    #[test]
    fn a_failed_conversation_of_a_moved_database_keeps_its_last_good_rows() {
        let tokens = |namespace: &str| {
            Some(
                ["good", "bad"]
                    .map(|session| {
                        let id = format!("{namespace}\0{session}");
                        (id, session.to_owned(), "token".to_owned())
                    })
                    .to_vec(),
            )
        };
        let row = |session: &str, text: &str| {
            let mut message = test_message(text);
            message.session = session.into();
            (vec![message], Vec::new(), ReadOutcome::Complete)
        };
        let mut cache = IngestCache::cold();
        cache.collect_token_cached_keyed_partial("crush", tokens("old"), &[], false, true, |_, s| {
            row(s, "first read")
        });
        cache.last_good_base = true;
        for round in 0..2 {
            let pass = cache
                .collect_token_cached_keyed_partial("crush", tokens("new"), &[], false, true, |_, s| {
                    if s == "bad" {
                        (Vec::new(), Vec::new(), ReadOutcome::Invalid)
                    } else {
                        row(s, "after the move")
                    }
                });
            let mut served: Vec<(&str, &str)> = pass
                .messages
                .iter()
                .map(|m| (m.session.as_ref(), m.text.as_ref()))
                .collect();
            served.sort_unstable();
            assert_eq!(served, [("bad", "first read"), ("good", "after the move")], "{round}");
            assert!(!cache.admitted_unpublished_failure());
            assert!(cache.entries.contains_key("\0tok\0crush\0new\0bad"));
        }
    }

    /// One session id in two crush databases names two conversations: one panicking in the
    /// first is not settled by the second's malformed read being served from its last good copy.
    #[test]
    fn a_settled_token_failure_is_one_databases_conversation() {
        let first = std::path::Path::new("/fixture/first/crush.db");
        let second = std::path::Path::new("/fixture/second/crush.db");
        let id = |database: &std::path::Path| {
            format!("{}s", crate::ingest::registry::token_prefix("crush", database).unwrap())
        };
        let tokens = |token: &str| {
            Some(
                [first, second]
                    .map(|database| (id(database), "s".to_owned(), token.to_owned()))
                    .to_vec(),
            )
        };
        let mut cache = IngestCache::cold();
        cache.collect_token_cached_keyed_partial("crush", tokens("t1"), &[], false, true, |_, s| {
            let mut message = test_message("published");
            message.session = s.into();
            (vec![message], Vec::new(), ReadOutcome::Complete)
        });
        cache.last_good_base = true;
        cache.collect_token_cached_keyed_partial("crush", tokens("t2"), &[], false, true, |key, _| {
            let outcome = if key == id(first) {
                ReadOutcome::Skipped
            } else {
                ReadOutcome::Invalid
            };
            (Vec::new(), Vec::new(), outcome)
        });
        assert!(cache.token_failure_settled("crush", second, "s"));
        assert!(!cache.token_failure_settled("crush", first, "s"));
    }

    /// A killed pass that saw a token conversation go left a base ahead of the published
    /// generation, which still holds it: that conversation failing with no entry is not proven
    /// unpublished by the base, only by a read of that generation.
    #[test]
    fn a_base_a_killed_pass_moved_proves_no_failed_conversation_unpublished() {
        let tokens = || Some(vec![("x".to_owned(), "x".to_owned(), "torn".to_owned())]);
        let failed = |released: bool| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.published_seal = Some([7; 16]);
            if released {
                let mut killed = super::ReleasedRows::default();
                killed.sessions.insert("x".to_owned());
                cache.released.insert("cursor".to_owned(), killed);
            }
            cache.collect_token_cached_keyed_partial("cursor", tokens(), &[], false, true, |_, _| {
                (Vec::new(), Vec::new(), ReadOutcome::Invalid)
            });
            cache
        };
        let current = failed(false);
        assert!(current.output_complete());
        assert!(current.admitted_unpublished_failure());
        let mut ahead = failed(true);
        assert!(!ahead.output_complete());
        assert!(ahead.output_complete_past_unreadable());
        ahead.admit_unpublished_token_conversations(|_| Some(HashSet::from(["x".to_owned()])));
        assert!(!ahead.output_complete());
        let mut ahead = failed(true);
        ahead.admit_unpublished_token_conversations(|_| Some(HashSet::from(["y".to_owned()])));
        assert!(ahead.output_complete());
    }

    /// A conversation whose read failed with no last-good copy contributes nothing, is not cached
    /// (so the next pass reads it again), and holds publication back only where a published
    /// generation may hold it; with a last-good copy that copy is served.
    #[test]
    fn failed_token_conversation_without_last_good_is_retried_not_cached() {
        let parse = |reads: &std::cell::Cell<usize>, session: &str| {
            if session == "bad" {
                reads.set(reads.get() + 1);
                (Vec::new(), Vec::new(), ReadOutcome::Invalid)
            } else {
                let mut message = test_message("healthy");
                message.session = session.into();
                (vec![message], Vec::new(), ReadOutcome::Complete)
            }
        };
        let tokens = || {
            Some(vec![
                ("good".to_string(), "good".to_string(), "good-token".to_string()),
                ("bad".to_string(), "bad".to_string(), "bad-token".to_string()),
            ])
        };
        let reads = std::cell::Cell::new(0);
        let mut first = IngestCache::cold();
        first.set_published_material(HashSet::new());
        first.set_published_token_namespaces(Some(HashSet::new()));
        let pass = first
            .collect_token_cached_keyed_partial("cursor", tokens(), &[], false, true, |_, s| {
                parse(&reads, s)
            });
        let texts: Vec<&str> = pass.messages.iter().map(|m| m.text.as_ref()).collect();
        assert_eq!(texts, ["healthy"]);
        assert!(first.output_complete());
        assert!(!first.source_snapshot_safe());
        assert!(!first.entries.contains_key("\0tok\0cursor\0bad"));
        // Admitted, so the caller would refuse this pass had it produced no row at all.
        assert!(first.admitted_unpublished_failure());
        let database = std::path::Path::new("/fixture/User/globalStorage/state.vscdb");
        assert!(first.token_failure_settled("cursor", database, "bad"));
        // The healthy sibling hits its cached token; the failed conversation is read again.
        let pass = first
            .collect_token_cached_keyed_partial("cursor", tokens(), &[], false, true, |_, s| {
                parse(&reads, s)
            });
        assert_eq!(pass.messages.len(), 1);
        assert_eq!(reads.get(), 2);
        assert!(first.output_complete());

        // A pass with no decoded base over a generation recorded as holding this database's
        // conversations cannot tell the failed one was never published until a read of that
        // generation shows it holds none of it; one it cannot read, or one naming it, holds.
        let cold_over_published = || {
            let mut cold = IngestCache::cold();
            cold.set_published_material(HashSet::new());
            cold.set_published_token_namespaces(Some(HashSet::from([(
                "cursor".into(),
                String::new(),
            )])));
            cold.collect_token_cached_keyed_partial("cursor", tokens(), &[], false, true, |_, s| {
                parse(&reads, s)
            });
            assert!(!cold.output_complete());
            assert!(!cold.admitted_unpublished_failure());
            assert!(!cold.token_failure_settled("cursor", database, "bad"));
            cold
        };
        for published in [None, Some(HashSet::from(["bad".to_owned()]))] {
            let mut held = cold_over_published();
            held.admit_unpublished_token_conversations(|_| published.clone());
            assert!(!held.output_complete());
        }
        let mut admitted = cold_over_published();
        admitted.admit_unpublished_token_conversations(|_| Some(HashSet::from(["good".into()])));
        assert!(admitted.output_complete());
        assert!(admitted.admitted_unpublished_failure());
        assert!(admitted.token_failure_settled("cursor", database, "bad"));

        // A conversation published before keeps its last-good rows when its read fails.
        let mut warm = IngestCache::cold();
        warm.collect_token_cached_keyed_partial("cursor", tokens(), &[], false, true, |_, s| {
            let mut message = test_message("last good");
            message.session = s.into();
            (vec![message], Vec::new(), ReadOutcome::Complete)
        });
        let moved = Some(vec![
            ("good".to_string(), "good".to_string(), "good-token".to_string()),
            ("bad".to_string(), "bad".to_string(), "bad-token-2".to_string()),
        ]);
        let pass = warm.collect_token_cached_keyed_partial("cursor", moved, &[], false, true, |_, s| {
            parse(&reads, s)
        });
        let mut served: Vec<(&str, &str)> = pass
            .messages
            .iter()
            .map(|m| (m.session.as_ref(), m.text.as_ref()))
            .collect();
        served.sort_unstable();
        assert_eq!(served, [("bad", "last good"), ("good", "last good")]);
        assert!(warm.output_complete());
        assert!(!warm.source_snapshot_safe());
        assert!(!warm.admitted_unpublished_failure());

        // Rows a partial read vouches for publish and are kept for a later failed read, but with
        // no identity, so every pass reads the conversation again.
        let mut partial = IngestCache::cold();
        partial.set_published_material(HashSet::new());
        partial.set_published_token_namespaces(Some(HashSet::new()));
        let partial_reads = std::cell::Cell::new(0);
        for _ in 0..2 {
            let pass = partial
                .collect_token_cached_keyed_partial("cursor", tokens(), &[], false, true, |_, s| {
                    partial_reads.set(partial_reads.get() + 1);
                    let mut message = test_message("vouched");
                    message.session = s.into();
                    (vec![message], Vec::new(), ReadOutcome::Partial)
                });
            assert_eq!(pass.messages.len(), 2);
            assert!(partial.output_complete());
        }
        assert_eq!(partial_reads.get(), 4);
    }

    #[test]
    fn repeated_empty_always_store_is_a_validated_deletion() {
        let root = std::env::temp_dir().join(format!(
            "agrep-always-delete-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let mut cache = IngestCache::cold();
        cache.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            vec![test_message("last source")],
            &[],
            ReadOutcome::Complete,
        );
        cache.save(&cache_path).unwrap();

        let (mut vanished_after_preflight, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        vanished_after_preflight.set_current_source_agents(HashSet::from(["cline".to_string()]));
        vanished_after_preflight.allow_repeated_missing_roots(HashSet::from(["cline".to_string()]));
        let (messages, guarded) = vanished_after_preflight.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            Vec::new(),
            &[],
            ReadOutcome::Complete,
        );
        assert_eq!(messages.len(), 1);
        assert!(guarded);
        assert!(!vanished_after_preflight.has_provisional_deletions());
        assert!(!vanished_after_preflight.source_snapshot_safe());

        let (mut retry, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        retry.allow_repeated_missing_roots(HashSet::from(["cline".to_string()]));
        let (messages, guarded) = retry.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            Vec::new(),
            &[],
            ReadOutcome::Complete,
        );
        assert!(messages.is_empty());
        assert!(!guarded);
        assert!(retry.has_provisional_deletions());
        assert!(retry.source_snapshot_safe());
        assert!(retry.touched.contains("session"));

        // Confirmation is per agent: another agent's repeated absence proves nothing here.
        let (mut other, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        other.allow_repeated_missing_roots(HashSet::from(["kimi".to_string()]));
        let (messages, guarded) = other.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            Vec::new(),
            &[],
            ReadOutcome::Complete,
        );
        assert_eq!(messages.len(), 1);
        assert!(guarded);
        assert!(!other.has_provisional_deletions());
        assert!(!other.source_snapshot_safe());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn prior_parse_semantics_cache_reparses_with_fallback() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-version-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("cache.bin");
        let mut old = IngestCache::cold();
        old.guard_never_empty(
            "cline",
            std::path::Path::new("/fixture-store"),
            vec![test_message("old semantics")],
            &[],
            ReadOutcome::Complete,
        );
        let payload = legacy_payload_v37(CACHE_VERSION - 1, &old.entries);
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&(CACHE_VERSION - 1).to_le_bytes());
        bytes.extend_from_slice(&0_u64.to_le_bytes());
        bytes.extend_from_slice(super::CACHE_BASE_MAGIC);
        bytes.extend_from_slice(&super::CACHE_BASE_STORAGE_VERSION_V2.to_le_bytes());
        bytes.extend_from_slice(&[7_u8; 16]);
        bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        bytes.extend_from_slice(&super::cache_digest(&payload));
        bytes.extend_from_slice(&payload);
        fs::write(&path, bytes).unwrap();

        let loaded = IngestCache::load(&path);
        assert!(!loaded.warm);
        assert!(loaded.force_reparse);
        assert_eq!(loaded.entries.len(), 1);
        assert!(loaded
            .entries
            .values()
            .all(|entry| entry.legacy_needs_reparse));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn v18_token_cache_forces_canonical_identity_reparse() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-token-v18-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("cache.bin");
        let mut old = IngestCache::cold();
        old.collect_token_cached(
            "cursor",
            Some(vec![("session#part".into(), "v1".into())]),
            |_| (vec![test_message("legacy token row")], Vec::new()),
        );
        let payload = bincode::serialize(&super::CacheFileRef {
            version: 18,
            entries: &old.entries,
        })
        .unwrap();
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&18_u32.to_le_bytes());
        bytes.extend_from_slice(&0_u64.to_le_bytes());
        bytes.extend_from_slice(super::CACHE_BASE_MAGIC);
        bytes.extend_from_slice(&super::CACHE_BASE_STORAGE_VERSION_V2.to_le_bytes());
        bytes.extend_from_slice(&[8_u8; 16]);
        bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        bytes.extend_from_slice(&super::cache_digest(&payload));
        bytes.extend_from_slice(&payload);
        fs::write(&path, bytes).unwrap();

        let mut loaded = IngestCache::load(&path);
        assert!(loaded.force_reparse);
        let parsed = std::cell::Cell::new(0);
        let pass = loaded.collect_token_cached(
            "cursor",
            Some(vec![("session#part".into(), "v1".into())]),
            |_| {
                parsed.set(parsed.get() + 1);
                (vec![test_message("canonical token row")], Vec::new())
            },
        );
        assert_eq!(parsed.get(), 1);
        assert_eq!(pass.parsed, 1);
        assert_eq!(pass.messages[0].text.as_ref(), "canonical token row");
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(not(windows))]
    #[test]
    fn eventful_token_recovery_accepts_only_canonical_ids() {
        let root = std::env::temp_dir().join(format!(
            "agrep-eventful-canonical-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let canonical =
            crate::intake::token_id(std::path::Path::new("db#part.sqlite"), "session#part");
        let book = serde_json::json!({
            "files": {
                canonical: {"agent": "cursor", "events": 1},
                "db#part.sqlite#legacy#session": {"agent": "cursor", "events": 1}
            }
        });
        fs::write(
            root.join("intake_stats.json"),
            serde_json::to_vec(&book).unwrap(),
        )
        .unwrap();
        let eventful = super::legacy_eventful_keys(&root.join("cache.bin"));
        assert!(eventful.contains("\0tok\0cursor\0session#part"));
        assert!(!eventful.contains("\0tok\0cursor\0session"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn partial_token_cache_skips_an_uncached_unavailable_namespace() {
        let root = std::env::temp_dir().join(format!(
            "agrep-partial-token-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let mut initial = IngestCache::cold();
        initial.collect_token_cached_keyed_partial(
            "crush",
            Some(vec![
                ("healthy\0session".into(), "session".into(), "v1".into()),
                ("cached-bad\0session".into(), "session".into(), "v1".into()),
            ]),
            &[],
            false,
            true,
            |id, _| {
                let text = if id.starts_with("healthy\0") {
                    "healthy old"
                } else {
                    "cached fallback"
                };
                (vec![test_message(text)], Vec::new())
            },
        );
        initial.save(&cache_path).unwrap();

        let mut warm = IngestCache::load(&cache_path);
        let pass = warm.collect_token_cached_keyed_partial(
            "crush",
            Some(vec![(
                "healthy\0session".into(),
                "session".into(),
                "v2".into(),
            )]),
            &["cached-bad\0".into(), "never-cached\0".into()],
            false,
            true,
            |_, _| (vec![test_message("healthy new")], Vec::new()),
        );
        let texts: HashSet<&str> = pass
            .messages
            .iter()
            .map(|message| message.text.as_ref())
            .collect();
        assert_eq!(texts, HashSet::from(["cached fallback", "healthy new"]));
        assert!(warm
            .entries
            .contains_key("\0tok\0crush\0cached-bad\0session"));
        assert!(!warm.source_snapshot_safe());
        assert!(warm.output_complete());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn cached_token_conversations_are_served_but_never_prove_their_database_empty() {
        use super::MaterialVerdict;
        use std::path::Path;

        let chats = Path::new("/fixture/chats/crush.db");
        let empty = Path::new("/fixture/empty/crush.db");
        let prefix = crate::ingest::registry::token_prefix("crush", chats).unwrap();
        let mut cache = IngestCache::cold();
        cache.collect_token_cached_keyed_partial(
            "crush",
            Some(vec![(
                format!("{prefix}session"),
                "session".into(),
                "v1".into(),
            )]),
            &[],
            false,
            true,
            |_, _| (vec![test_message("cached chat")], Vec::new()),
        );
        cache.set_published_material(HashSet::new());
        cache.set_published_token_namespaces(Some(HashSet::new()));
        assert_eq!(
            cache.token_namespaces(),
            HashSet::from([("crush".to_string(), prefix.clone())])
        );
        // Cached conversations are served, so publishing past the database drops none of them;
        // they never prove it held nothing.
        assert_eq!(
            cache.published_material_under("crush", chats),
            MaterialVerdict::Retained
        );
        assert!(!cache.unreadable_scope_covered("crush", chats));
        assert_eq!(
            cache.published_material_under("crush", empty),
            MaterialVerdict::Retained
        );
        assert!(cache.unreadable_scope_covered("crush", empty));
        // A Stat agent's scope never reads token entries.
        assert_eq!(
            cache.published_material_under("claude", chats),
            MaterialVerdict::Retained
        );
        // A recorded or unknown published namespace keeps a database without cached rows.
        let empty_prefix = crate::ingest::registry::token_prefix("crush", empty).unwrap();
        for namespaces in [Some(HashSet::from([("crush".into(), empty_prefix)])), None] {
            cache.set_published_token_namespaces(namespaces);
            assert_eq!(
                cache.published_material_under("crush", empty),
                MaterialVerdict::Drops
            );
            assert!(!cache.unreadable_scope_covered("crush", empty));
            assert_eq!(
                cache.published_material_under("claude", empty),
                MaterialVerdict::Retained
            );
        }
    }

    #[test]
    fn unpublished_token_agent_relaxes_only_listed_databases_without_cached_rows() {
        use super::MaterialVerdict;
        use std::path::{Path, PathBuf};

        let chats = Path::new("/fixture/chats/crush.db");
        let listed = Path::new("/fixture/listed/crush.db");
        let transcript = Path::new("/fixture/claude/chat.jsonl");
        let prefix = crate::ingest::registry::token_prefix("crush", chats).unwrap();
        let mut cache = IngestCache::cold();
        // No inventory at all: only the read of the published generation can answer.
        cache.set_unpublished_token_agents(HashSet::from(["crush".to_string()]));
        assert_eq!(
            cache.published_material_under("crush", listed),
            MaterialVerdict::Retained
        );
        cache.set_published_material(HashSet::from([
            PathBuf::from(listed),
            PathBuf::from(transcript),
        ]));
        cache.set_published_blind_scopes(HashSet::from([("crush".into(), listed.into())]));
        assert_eq!(
            cache.published_material_under("crush", listed),
            MaterialVerdict::Retained
        );
        assert!(cache.unreadable_scope_covered("crush", listed));
        cache.collect_token_cached_keyed_partial(
            "crush",
            Some(vec![(
                format!("{prefix}session"),
                "session".into(),
                "v1".into(),
            )]),
            &[],
            false,
            true,
            |_, _| (vec![test_message("cached, not yet published")], Vec::new()),
        );
        assert_eq!(
            cache.published_material_under("crush", chats),
            MaterialVerdict::Retained
        );
        assert!(!cache.unreadable_scope_covered("crush", chats));
        // Stat scopes and other token agents keep the listed inventory.
        cache.set_unpublished_token_agents(HashSet::from(["claude".to_string()]));
        assert_eq!(
            cache.published_material_under("claude", transcript),
            MaterialVerdict::Drops
        );
        assert_eq!(
            cache.published_material_under("crush", listed),
            MaterialVerdict::Drops
        );
    }

    #[test]
    fn a_snapshot_seeded_from_a_partial_read_never_covers_its_own_pass() {
        use super::MaterialVerdict;
        use std::path::Path;

        let root = Path::new("/fixture/.kimi");
        let lost = Path::new("/fixture/.kimi/sessions/a/lost");
        let published = || HashSet::from([lost.join("context.jsonl")]);
        let skipped = |cache: &mut IngestCache| {
            let fresh = vec![test_message("a sibling session still parses")];
            cache.guard_never_empty("kimi", root, fresh, &[], ReadOutcome::Skipped)
        };

        let mut lost_cache = IngestCache::cold();
        lost_cache.set_published_material(published());
        assert_eq!(skipped(&mut lost_cache).0.len(), 1);
        assert!(!lost_cache.output_complete());
        assert!(!lost_cache.unreadable_scope_covered("kimi", lost));
        assert_eq!(
            lost_cache.published_material_under("kimi", lost),
            MaterialVerdict::Drops
        );

        let mut last_good = IngestCache::cold();
        last_good.set_published_material(published());
        let mut row = test_message("the lost session's last good row");
        row.session = "lost".into();
        last_good.guard_never_empty("kimi", root, vec![row], &[], ReadOutcome::Complete);
        let served = skipped(&mut last_good).0;
        assert!(served.iter().any(|message| &*message.session == "lost"));
        assert!(last_good.output_complete());
        assert!(last_good.unreadable_scope_covered("kimi", lost));

        let mut first = IngestCache::cold();
        first.set_published_material(HashSet::new());
        skipped(&mut first);
        assert!(first.output_complete());
        assert!(first.unreadable_scope_covered("kimi", lost));
    }

    #[test]
    fn only_an_unpublished_whole_store_agent_is_admitted_past_its_failed_read() {
        use super::MaterialVerdict;
        use std::path::Path;

        let lost = Path::new("/fixture/.kimi/sessions/a/lost");
        let transcript = Path::new("/fixture/.claude/projects/p/chat.jsonl");
        let mut cache = IngestCache::cold();
        cache.set_published_material(HashSet::from([lost.join("wire.jsonl"), transcript.into()]));
        cache.record_source_read_issue("kimi", lost, "source-read-failed", "parser panicked");
        cache.record_source_read_issue(
            "claude",
            transcript,
            "source-read-failed",
            "parser panicked",
        );
        cache.admit_unpublished_whole_store_agents(|_| true);
        assert!(!cache.unreadable_scope_covered("kimi", lost));

        let mut read = Vec::new();
        cache.admit_unpublished_whole_store_agents(|agent| {
            read.push(agent.to_owned());
            false
        });
        assert_eq!(read, ["kimi"]);
        assert!(cache.unreadable_scope_covered("kimi", lost));
        assert_eq!(
            cache.published_material_under("kimi", lost),
            MaterialVerdict::Retained
        );
        assert!(!cache.unreadable_scope_covered("claude", transcript));
    }

    #[test]
    fn an_older_generation_admits_a_partial_read_agent_only_past_an_unpublished_generation() {
        use super::MaterialVerdict;
        use std::path::Path;

        let store = Path::new("/fixture/.local/share/opencode");
        let project = Path::new("/fixture/.claude/projects/locked");
        let legacy = || {
            let mut cache = IngestCache::cold();
            cache.legacy_generation = true;
            cache.set_published_material_from_cache();
            cache
        };

        // The release may have published this store's rows uncached: silent until proven.
        let mut published = legacy();
        assert_eq!(
            published.published_material_under("opencode", store),
            MaterialVerdict::Unknown
        );
        published.admit_unpublished_partial_read_agents(["opencode"], |_| true);
        assert_eq!(
            published.published_material_under("opencode", store),
            MaterialVerdict::Unknown
        );
        assert!(!published.unreadable_scope_covered("opencode", store));

        let mut unpublished = legacy();
        let mut read = Vec::new();
        unpublished.admit_unpublished_partial_read_agents(["opencode", "claude"], |agent| {
            read.push(agent.to_owned());
            false
        });
        assert_eq!(read, ["opencode"], "only a silenced agent pays the read");
        assert_eq!(
            unpublished.published_material_under("opencode", store),
            MaterialVerdict::Retained
        );
        assert!(unpublished.unreadable_scope_covered("opencode", store));
        assert_eq!(
            unpublished.published_material_under("claude", project),
            MaterialVerdict::Retained
        );

        // A current inventory is never silent, so nothing is read or admitted.
        let mut current = IngestCache::cold();
        current.set_published_material_from_cache();
        current.admit_unpublished_partial_read_agents(["opencode"], |_| unreachable!());
        assert!(current.unpublished_agents.is_empty());
    }

    #[test]
    fn an_invalid_read_without_a_snapshot_seeds_one_that_covers_only_later_passes() {
        use std::path::Path;

        let root = Path::new("/fixture/.cline/data");
        let task = Path::new("/fixture/.cline/data/tasks/1/api_conversation_history.json");
        let invalid = |cache: &mut IngestCache, rows: Vec<Message>| {
            cache.guard_never_empty("cline", root, rows, &[], ReadOutcome::Invalid)
        };

        let mut first = IngestCache::cold();
        first.set_published_material(HashSet::new());
        let (rows, guarded) = invalid(&mut first, vec![test_message("readable task")]);
        assert_eq!(rows.len(), 1);
        assert!(guarded);
        assert!(first.output_complete());

        // Published material at stake: the seed licenses nothing until a read of the
        // published generation finds none of this agent's rows.
        let mut at_stake = IngestCache::cold();
        at_stake.set_published_material(HashSet::from([task.to_path_buf()]));
        invalid(&mut at_stake, vec![test_message("readable task")]);
        at_stake.record_source_read_issue("cline", task, "source-invalid", "torn");
        assert!(!at_stake.output_complete());
        assert!(!at_stake.unreadable_scope_covered("cline", task));
        at_stake.admit_unpublished_whole_store_agents(|_| true);
        assert!(!at_stake.output_complete());
        at_stake.admit_unpublished_whole_store_agents(|_| false);
        assert!(at_stake.output_complete());

        let dir = std::env::temp_dir().join(format!(
            "agrep-seeded-invalid-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let cache_path = dir.join(".ingest_cache.bin");
        first.save(&cache_path).unwrap();
        let mut next = IngestCache::load(&cache_path);
        next.set_published_material(HashSet::from([task.to_path_buf()]));
        let (served, guarded) = invalid(&mut next, Vec::new());
        assert_eq!(
            served.len(),
            1,
            "the seed did not serve the next failed read"
        );
        assert!(guarded);
        assert!(next.output_complete());
        assert!(next.unreadable_scope_covered("cline", task));
        fs::remove_dir_all(&dir).ok();
    }

    fn cached_entry(rows: &[crate::model::Message]) -> super::Entry {
        super::Entry {
            mtime: 1,
            size: 1,
            identity: None,
            msgs: rows.iter().map(CMsg::from).collect(),
            event_keys: Vec::new(),
            legacy_had_events: false,
            legacy_needs_reparse: true,
        }
    }

    /// A v4 base file of the given semantic version, owned by this build, around `payload`.
    fn write_v4_base(path: &std::path::Path, version: u32, instance: [u8; 16], payload: &[u8]) {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&version.to_le_bytes());
        bytes.extend_from_slice(&0_u64.to_le_bytes());
        bytes.extend_from_slice(super::CACHE_BASE_MAGIC);
        bytes.extend_from_slice(&super::CACHE_BASE_STORAGE_VERSION.to_le_bytes());
        bytes.extend_from_slice(&super::WriterBuildId::current().0);
        bytes.extend_from_slice(&instance);
        bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        bytes.extend_from_slice(&super::cache_digest(payload));
        bytes.extend_from_slice(&super::CACHE_CODEC_NONE.to_le_bytes());
        bytes.extend_from_slice(&0_u32.to_le_bytes());
        bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        bytes.extend_from_slice(payload);
        fs::write(path, bytes).unwrap();
    }

    /// A journal holding one frame (0 -> 1) whose payload is the given bytes.
    fn write_journal_frame(path: &std::path::Path, instance: [u8; 16], payload: &[u8]) {
        let digest = super::cache_digest(payload);
        let mut bytes = super::encode_journal_header(instance);
        bytes.extend_from_slice(super::CACHE_FRAME_MAGIC);
        bytes.extend_from_slice(&0_u64.to_le_bytes());
        bytes.extend_from_slice(&1_u64.to_le_bytes());
        bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        bytes.extend_from_slice(&digest);
        bytes.extend_from_slice(payload);
        bytes.extend_from_slice(super::CACHE_COMMIT_MAGIC);
        bytes.extend_from_slice(&1_u64.to_le_bytes());
        bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        bytes.extend_from_slice(&digest);
        fs::write(path, bytes).unwrap();
    }
    #[test]
    fn a_partial_read_extends_a_v37_entry_and_a_capped_reply() {
        let cap = crate::ingest::REPLY_CAP;
        let with_reply = |text: &str, reply: &str| {
            let mut row = test_message(text);
            row.reply = reply.into();
            row.reply_chars = reply.chars().count();
            row
        };
        let capped = format!("{}…", "x".repeat(cap));
        let cases: [(String, String, bool); 4] = [
            // the text a v37 entry holds is the text a reparse publishes
            ("A. B.".into(), "A. B.".into(), true),
            ("A. B.".into(), "A. B. C.".into(), true),
            // a capped reply grows no further; a later block changes only its shape
            (capped.clone(), capped, true),
            ("A. B.".into(), "A.".into(), false),
        ];
        for (cached, fresh, extends) in cases {
            let prev = cached_entry(&[with_reply("q", &cached)]);
            assert_eq!(
                super::partial_read_extends(&prev, &[with_reply("q", &fresh)], &[]),
                extends,
                "{cached:.20} -> {fresh:.20}"
            );
        }
    }

    #[test]
    fn the_v37_wire_decodes_through_its_own_layout_journal_included() {
        let root = std::env::temp_dir().join(format!(
            "agrep-v37-wire-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let legacy_row = |text: &str, reply: &str| super::LegacyCMsgV37 {
            agent: "claude".into(),
            project: "p".into(),
            session: "s".into(),
            ts: 1,
            turn: 0,
            text: text.into(),
            who: "user".into(),
            model: "".into(),
            model_source: "unknown".into(),
            reply: reply.into(),
            reply_chars: reply.chars().count(),
            side: false,
            parent: "".into(),
        };
        let legacy_entry = |text: &str, reply: &str| super::LegacyEntryV37 {
            mtime: 1,
            size: 1,
            identity: None,
            msgs: vec![legacy_row(text, reply)],
            event_keys: Vec::new(),
            legacy_had_events: false,
            legacy_needs_reparse: false,
        };
        let mut entries = HashMap::new();
        entries.insert("\0file\0a".to_string(), legacy_entry("base", "one two"));
        let payload = bincode::serialize(&super::LegacyCacheFileV37 {
            version: CACHE_VERSION - 1,
            entries,
        })
        .unwrap();
        let instance = [7u8; 16];
        write_v4_base(&cache_path, CACHE_VERSION - 1, instance, &payload);
        let delta = bincode::serialize(&super::LegacyJournalDeltaV37 {
            upserts: vec![("\0file\0b".to_string(), legacy_entry("journaled", "three"))],
            deletes: Vec::new(),
        })
        .unwrap();
        write_journal_frame(&super::cache_journal_path(&cache_path), instance, &delta);
        let (entries, generation, _) =
            super::decode_cache_for(&cache_path, super::WriterBuildId::current()).unwrap();
        assert!(generation == super::CacheGeneration::LegacyReparse);
        let rows: Vec<(String, String, bool)> = {
            let mut rows: Vec<_> = entries
                .iter()
                .map(|(key, entry)| {
                    (
                        key.clone(),
                        entry.msgs[0].reply.to_string(),
                        entry.legacy_needs_reparse,
                    )
                })
                .collect();
            rows.sort();
            rows
        };
        assert_eq!(
            rows,
            [
                ("\0file\0a".to_string(), "one two".to_string(), true),
                ("\0file\0b".to_string(), "three".to_string(), true),
            ]
        );
        assert!(entries
            .values()
            .all(|e| e.msgs[0].reply_shape.breaks.is_empty()));
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_per_file_scope_is_served_only_when_the_cache_holds_every_file_listed_under_it() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.claude/projects/p");
        let chat = scope.join("chat.jsonl");
        let summary = scope.join("summary.jsonl");
        let listed = || HashSet::from([chat.clone(), summary.clone()]);
        // Each cached file, with a row or with none, and the rows a read of the published
        // generation finds should the scope pay one.
        let verdict = |files: &[(&Path, bool)], published: &[(&str, &str)]| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.set_published_material(listed());
            for (path, with_row) in files {
                let rows = if *with_row {
                    vec![test_message("a cached row")]
                } else {
                    Vec::new()
                };
                cache.put_entry(path.to_string_lossy().into_owned(), cached_entry(&rows));
            }
            let mut read = false;
            cache.record_published_rows([("claude", scope)], |_| {
                read = true;
                Some(published_rows(published))
            });
            (cache.published_material_under("claude", scope), read)
        };

        // A file read whole that published nothing is served as surely as one with rows.
        assert_eq!(
            verdict(
                &[(&chat, true), (&summary, false)],
                &[("session", "a cached row")]
            ),
            (MaterialVerdict::Retained, false)
        );
        // Of a listed file the cache never held, only the published generation can tell.
        assert_eq!(
            verdict(&[(&chat, true)], &[("session", "a cached row")]),
            (MaterialVerdict::Retained, true)
        );
        assert_eq!(
            verdict(
                &[(&chat, true)],
                &[
                    ("session", "a cached row"),
                    ("uncached", "a row only published")
                ]
            ),
            (MaterialVerdict::Drops, true)
        );
        assert_eq!(
            verdict(&[(&summary, false)], &[("session", "a cached row")]),
            (MaterialVerdict::Drops, true)
        );
    }

    /// Published rows, as `(session, text)`, each stamped as `test_message` stamps its rows.
    fn published_rows(rows: &[(&str, &str)]) -> crate::cache::PublishedAgentRows {
        crate::cache::PublishedAgentRows {
            turns: rows
                .iter()
                .enumerate()
                .map(|(turn, (session, text))| {
                    let row = crate::cache::PublishedTurn {
                        project: "project".into(),
                        model: None,
                        ts: 1,
                        text: (*text).to_owned(),
                    };
                    (((*session).to_owned(), turn as u32), row)
                })
                .collect(),
            sessions: rows
                .iter()
                .map(|(session, _)| (*session).to_owned())
                .collect(),
            event_sessions: HashSet::new(),
        }
    }

    #[test]
    fn a_row_the_base_held_and_this_pass_dropped_is_not_lost_behind_a_failed_scope() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.claude/projects/locked");
        let never_read = scope.join("never-read.jsonl");
        let deleted = Path::new("/fixture/.claude/projects/gone/chat.jsonl");
        let mut row = test_message("a row this pass deletes");
        row.session = "gone".into();
        // Whether the base census runs, and the sessions the published generation holds.
        let verdict = |census: bool, published: &[(&str, &str)]| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.set_published_material(HashSet::from([never_read.clone(), deleted.into()]));
            let key = deleted.to_string_lossy().into_owned();
            cache.put_entry(key.clone(), cached_entry(std::slice::from_ref(&row)));
            if census {
                cache.mark_base_rows([("claude", scope)], None);
            }
            cache.remove_entry(&key);
            let mut once = Some(published_rows(published));
            cache.record_published_rows([("claude", scope)], |_| once.take());
            cache.published_material_under("claude", scope)
        };

        let gone = ("gone", "a row this pass deletes");
        assert_eq!(verdict(true, &[gone]), MaterialVerdict::Retained);
        assert_eq!(verdict(false, &[gone]), MaterialVerdict::Drops);
        // A row the base never held is one only the published generation can still serve.
        assert_eq!(
            verdict(true, &[gone, ("never-read", "a never-read row")]),
            MaterialVerdict::Drops
        );
    }

    #[test]
    fn a_row_the_base_held_under_a_failed_scope_counts_only_while_this_pass_holds_it() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.codex/sessions/2026/01/03");
        let under = scope.join("rollout-a.jsonl");
        let never_read = Path::new("/fixture/.codex/sessions/2026/01/06/rollout-c.jsonl");
        let mut row = test_message("a prompt only the unreadable rollout holds");
        row.agent = "codex";
        row.session = "split".into();
        // Whether this pass still holds the rollout's entry after ingest.
        let verdict = |kept: bool| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.set_published_material(HashSet::from([under.clone(), never_read.into()]));
            let key = under.to_string_lossy().into_owned();
            cache.put_entry(key.clone(), cached_entry(std::slice::from_ref(&row)));
            let issues = [("codex", scope), ("codex", never_read.parent().unwrap())];
            cache.mark_base_rows(issues, None);
            if !kept {
                cache.remove_entry(&key);
            }
            let held = ("split", "a prompt only the unreadable rollout holds");
            let mut once = Some(published_rows(&[held]));
            cache.record_published_rows(issues, |_| once.take());
            cache.published_material_under("codex", never_read.parent().unwrap())
        };

        assert_eq!(verdict(true), MaterialVerdict::Retained);
        assert_eq!(verdict(false), MaterialVerdict::Drops);
    }

    #[cfg(unix)]
    #[test]
    fn a_file_is_absent_only_beneath_directories_proven_real() {
        use std::path::Path;

        let root = std::env::temp_dir().join(format!(
            "agrep-absence-unproven-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let day = root.join("2026/01/03");
        fs::create_dir_all(&day).unwrap();
        let drive = root.join("drive");
        fs::create_dir_all(drive.join("05")).unwrap();
        std::os::unix::fs::symlink(drive.join("05"), root.join("2026/01/05")).unwrap();
        std::os::unix::fs::symlink(drive.join("gone"), root.join("2026/01/06")).unwrap();
        fs::write(root.join("2026/01/07"), b"a file where a day directory was").unwrap();
        let unproven = |relative: &str| super::absence_unproven(&root, &root.join(relative));

        assert!(!unproven("2026/01/03/rollout.jsonl"), "real directories");
        assert!(!unproven("2026/01/04/rollout.jsonl"), "its directory gone");
        assert!(unproven("2026/01/05/rollout.jsonl"), "a resolving link");
        assert!(unproven("2026/01/06/rollout.jsonl"), "a dangling link");
        assert!(!unproven("2026/01/07/rollout.jsonl"), "a plain file where the directory was");
        assert!(
            !unproven("2026/01/07/deeper/rollout.jsonl"),
            "an ancestor whose stat proves it gone (ENOTDIR under the file)"
        );
        assert!(!unproven("rollout.jsonl"), "directly beneath the root");
        let elsewhere = Path::new("/elsewhere/rollout.jsonl");
        assert!(!super::absence_unproven(&root, elsewhere));
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn a_published_row_is_held_only_by_as_many_cached_rows_of_its_session_time_and_text() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.codex/sessions/2026/01/03");
        let lost = scope.join("rollout-a.jsonl");
        let resumed = Path::new("/fixture/.codex/sessions/2026/01/05/rollout-b.jsonl");
        // The resumed rollout's rows as `(ts, text)`, its raw turns counted from 0, against the
        // session's published rows as `(turn, ts, text)`, renumbered by turn-collision repair.
        let verdict = |cached: &[(i64, &str)], published: &[(u32, i64, &str)]| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.set_published_material(HashSet::from([lost.clone(), resumed.into()]));
            let rows: Vec<_> = (0..)
                .zip(cached)
                .map(|(turn, (ts, text))| {
                    let mut row = test_message(text);
                    (row.agent, row.session, row.turn, row.ts) =
                        ("codex", "split".into(), turn, *ts);
                    row
                })
                .collect();
            let key = resumed.to_string_lossy().into_owned();
            cache.put_entry(key, cached_entry(&rows));
            let turns = published.iter().map(|(turn, ts, text)| {
                let row = crate::cache::PublishedTurn {
                    project: "project".into(),
                    model: None,
                    ts: *ts,
                    text: (*text).to_owned(),
                };
                (("split".to_owned(), *turn), row)
            });
            let mut once = Some(crate::cache::PublishedAgentRows {
                turns: turns.collect(),
                sessions: HashSet::from(["split".to_owned()]),
                event_sessions: HashSet::new(),
            });
            cache.record_published_rows([("codex", scope)], |_| once.take());
            cache.published_material_under("codex", scope)
        };
        let resumed_rows = [(11, "three"), (12, "four")];

        // Held by content, whatever turn repair gave it.
        let renumbered = [(2, 11, "three"), (3, 12, "four")];
        assert_eq!(
            verdict(&resumed_rows, &renumbered),
            MaterialVerdict::Retained
        );
        assert_eq!(
            verdict(&resumed_rows, &[(3, 12, "five")]),
            MaterialVerdict::Drops
        );
        assert_eq!(
            verdict(&resumed_rows, &[(3, 13, "four")]),
            MaterialVerdict::Drops
        );
        // The lost rollout's rows sort first, onto turns the resumed rollout's raw rows bear.
        let split = [
            (0, 9, "one"),
            (1, 10, "two"),
            (2, 11, "three"),
            (3, 12, "four"),
        ];
        assert_eq!(verdict(&resumed_rows, &split), MaterialVerdict::Drops);
        // A lost row at the very time of a held one, renumbered onto that one's raw turn.
        let same_time = [(0, 11, "a lost prompt"), (1, 11, "three")];
        assert_eq!(
            verdict(&[(11, "three")], &same_time),
            MaterialVerdict::Drops
        );
        // A row published twice is held only by two.
        let twice = [(3, 5, "continue"), (7, 5, "continue")];
        assert_eq!(verdict(&[(5, "continue")], &twice), MaterialVerdict::Drops);
        let held_twice = [(5, "continue"), (5, "continue")];
        assert_eq!(verdict(&held_twice, &twice), MaterialVerdict::Retained);
    }

    #[test]
    fn a_row_both_the_base_and_this_pass_hold_counts_once() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.claude/projects/locked");
        let never_read = scope.join("never-read.jsonl");
        let old = Path::new("/fixture/.claude/projects/p/old.jsonl");
        let new = Path::new("/fixture/.claude/projects/p/new.jsonl");
        let row = test_message("continue");
        // Where this pass holds the base's row after ingest, against how often it was published.
        let verdict = |after: Option<&Path>, published: usize| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.set_published_material(HashSet::from([never_read.clone(), old.into()]));
            let entry = || cached_entry(std::slice::from_ref(&row));
            cache.put_entry(old.to_string_lossy().into_owned(), entry());
            cache.mark_base_rows([("claude", scope)], None);
            cache.remove_entry(&old.to_string_lossy());
            if let Some(path) = after {
                cache.put_entry(path.to_string_lossy().into_owned(), entry());
            }
            let rows = vec![("session", "continue"); published];
            let mut once = Some(published_rows(&rows));
            cache.record_published_rows([("claude", scope)], |_| once.take());
            cache.published_material_under("claude", scope)
        };

        assert_eq!(verdict(Some(old), 1), MaterialVerdict::Retained);
        assert_eq!(verdict(Some(new), 1), MaterialVerdict::Retained);
        assert_eq!(verdict(None, 1), MaterialVerdict::Retained);
        // Its file renamed, the one row does not stand in for a second the published generation
        // holds behind the locked directory.
        assert_eq!(verdict(Some(new), 2), MaterialVerdict::Drops);
        assert_eq!(verdict(Some(old), 2), MaterialVerdict::Drops);
    }

    #[test]
    fn a_cached_row_publication_folds_into_another_holds_nothing() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.claude/projects/locked");
        let never_read = scope.join("never-read.jsonl");
        let original = Path::new("/fixture/.claude/projects/p/original.jsonl");
        let replay = Path::new("/fixture/.claude/projects/p/replay.jsonl");
        // A replay of the original's turn at a later time: dedupe publishes the earlier one.
        let at = |ts: i64| {
            let mut row = test_message("continue");
            row.ts = ts;
            cached_entry(&[row])
        };
        let verdict = |published: &[i64]| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.set_published_material(HashSet::from([never_read.clone(), original.into()]));
            cache.put_entry(original.to_string_lossy().into_owned(), at(5));
            cache.put_entry(replay.to_string_lossy().into_owned(), at(6));
            let turns = (0..).zip(published).map(|(turn, ts)| {
                let row = crate::cache::PublishedTurn {
                    project: "project".into(),
                    model: None,
                    ts: *ts,
                    text: "continue".to_owned(),
                };
                (("session".to_owned(), turn), row)
            });
            let mut once = Some(crate::cache::PublishedAgentRows {
                turns: turns.collect(),
                sessions: HashSet::from(["session".to_owned()]),
                event_sessions: HashSet::new(),
            });
            cache.record_published_rows([("claude", scope)], |_| once.take());
            cache.published_material_under("claude", scope)
        };

        assert_eq!(verdict(&[5]), MaterialVerdict::Retained);
        // The later time, published by a file behind the locked directory, is one this pass drops.
        assert_eq!(verdict(&[5, 6]), MaterialVerdict::Drops);
    }

    #[test]
    fn rows_a_killed_pass_saw_go_count_once_while_its_generation_stays_published() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.claude/projects/locked");
        let never_read = scope.join("never-read.jsonl");
        let chat = Path::new("/fixture/.claude/projects/p/chat.jsonl");
        let copy = Path::new("/fixture/.claude/projects/p/copy.jsonl");
        let key = |path: &Path| path.to_string_lossy().into_owned();
        let sealed = [1; 16];
        // A pass over `entries` against the generation `sealed` names: it makes `change`, stages
        // what it saw go with its cache, and is killed before replacing that generation.
        let killed = |entries: &HashMap<String, super::Entry>,
                      change: &dyn Fn(&mut IngestCache)| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.entries = entries.clone();
            cache.mark_base_rows([("claude", scope)], Some(sealed));
            change(&mut cache);
            cache.record_released_rows();
            cache.entries
        };
        // The next pass over `entries`, against the generation `seal` names, which holds the
        // chat's first row `published` times.
        let verdict = |entries: &HashMap<String, super::Entry>,
                       seal: crate::cache::GenerationSeal,
                       published: usize| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.entries = entries.clone();
            cache.set_published_material(HashSet::from([never_read.clone(), chat.into()]));
            cache.mark_base_rows([("claude", scope)], Some(seal));
            let rows = vec![("session", "as first written"); published];
            let mut once = Some(published_rows(&rows));
            cache.record_published_rows([("claude", scope)], |_| once.take());
            cache.published_material_under("claude", scope)
        };
        let first = || cached_entry(&[test_message("as first written")]);
        let written = HashMap::from([(key(chat), first())]);
        let rewritten = killed(&written, &|cache| {
            cache.put_entry(key(chat), cached_entry(&[test_message("as rewritten")]));
        });

        assert_eq!(verdict(&rewritten, sealed, 1), MaterialVerdict::Retained);
        assert_eq!(verdict(&rewritten, sealed, 2), MaterialVerdict::Drops);
        // A pass that replaced that generation published what its cache held instead.
        assert_eq!(verdict(&rewritten, [2; 16], 1), MaterialVerdict::Drops);
        // Another killed pass carries the record on; one that brings the row back holds it itself.
        let carried = killed(&rewritten, &|_| {});
        assert_eq!(verdict(&carried, sealed, 1), MaterialVerdict::Retained);
        let restored = killed(&rewritten, &|cache| cache.put_entry(key(copy), first()));
        assert_eq!(verdict(&restored, sealed, 1), MaterialVerdict::Retained);
        assert_eq!(verdict(&restored, sealed, 2), MaterialVerdict::Drops);
        // The chat deleted, its session with it.
        let deleted = killed(&written, &|cache| {
            cache.remove_entry(&key(chat));
        });
        assert_eq!(verdict(&deleted, sealed, 1), MaterialVerdict::Retained);
        assert_eq!(verdict(&deleted, [2; 16], 1), MaterialVerdict::Drops);
        // The records serve nothing: no row, event, source or published material. However many
        // rows a pass saw go, they take one entry, so the base's entry count stays its sources.
        let mut records = IngestCache::cold();
        records.entries = deleted;
        assert_eq!(records.entries.len(), 1);
        assert!(records.entries.keys().all(|key| super::released_key(key)));
        assert!(records.session_sources().is_empty() && records.live_event_files().is_empty());
        records.set_published_material_from_cache();
        assert_eq!(records.published_material, Some(HashSet::new()));
    }

    #[test]
    fn a_released_record_leaves_a_cache_awaiting_its_whole_reparse() {
        let released = HashMap::from([(
            "claude".to_owned(),
            super::ReleasedRows {
                rows: HashMap::new(),
                sessions: HashSet::from(["session".to_owned()]),
            },
        )]);
        let record = super::pack_released(&super::hex16(&[1; 16]), &released).unwrap();
        let entries = HashMap::from([
            (
                "\0snapshot\0cline".to_owned(),
                cached_entry(&[test_message("a row awaiting reparse")]),
            ),
            record,
        ]);
        let payload = bincode::serialize(&super::CacheFileRef {
            version: CACHE_VERSION,
            entries: &entries,
        })
        .unwrap();
        let Ok((decoded, generation, _)) =
            super::decode_cache_payload(&payload, std::path::Path::new("cache.bin"))
        else {
            panic!("the cache did not decode");
        };
        assert_eq!(decoded.len(), 2);
        assert!(generation == super::CacheGeneration::LegacyReparse);
    }

    #[test]
    fn judging_a_large_agent_against_its_published_rows_stays_fast() {
        use super::MaterialVerdict;
        use std::path::Path;

        let scope = Path::new("/fixture/.claude/projects/locked");
        let never_read = scope.join("never-read.jsonl");
        let mut cache = IngestCache::cold();
        cache.last_good_base = true;
        let mut listed = HashSet::from([never_read]);
        let mut published = crate::cache::PublishedAgentRows::default();
        for file in 0..6_000 {
            let path = PathBuf::from(format!("/fixture/.claude/projects/p/{file}.jsonl"));
            let session = format!("session-{file}");
            let rows: Vec<_> = (0..10_u32)
                .map(|turn| {
                    let mut row =
                        test_message(&format!("{file}:{turn} {}", "row text ".repeat(48)));
                    (row.session, row.turn) = (session.as_str().into(), turn);
                    let turn_row = crate::cache::PublishedTurn {
                        project: "project".into(),
                        model: None,
                        ts: row.ts,
                        text: row.text.to_string(),
                    };
                    published.turns.insert((session.clone(), turn), turn_row);
                    row
                })
                .collect();
            published.sessions.insert(session);
            cache.put_entry(path.to_string_lossy().into_owned(), cached_entry(&rows));
            listed.insert(path);
        }
        cache.set_published_material(listed);
        cache.mark_base_rows([("claude", scope)], None);
        let started = std::time::Instant::now();
        let mut once = Some(published);
        cache.record_published_rows([("claude", scope)], |_| once.take());
        cache.record_released_rows();
        let elapsed = started.elapsed();
        assert_eq!(
            cache.published_material_under("claude", scope),
            MaterialVerdict::Retained
        );
        assert!(elapsed < std::time::Duration::from_secs(10), "{elapsed:?}");
    }

    #[test]
    fn a_legacy_partial_read_agent_is_served_only_where_its_published_rows_are_cached() {
        use super::MaterialVerdict;
        use std::path::Path;

        let store = Path::new("/fixture/.local/share/opencode");
        let stable = store.join("opencode.db");
        let mut cached = test_message("a cached channel");
        cached.agent = "opencode";
        cached.session = "stable".into();
        let project = Path::new("/fixture/.claude/projects/p");
        let verdict = |rows: Option<crate::cache::PublishedAgentRows>, record: bool| {
            let mut cache = IngestCache::cold();
            cache.last_good_base = true;
            cache.legacy_generation = true;
            cache.set_published_material(HashSet::from([stable.clone()]));
            cache.put_entry(
                stable.to_string_lossy().into_owned(),
                cached_entry(std::slice::from_ref(&cached)),
            );
            let mut once = Some(rows);
            let mut read = 0;
            if record {
                cache.record_published_rows([("opencode", store), ("claude", project)], |agent| {
                    assert_eq!(agent, "opencode", "only a partial-read agent pays the read");
                    read += 1;
                    once.take().flatten()
                });
                assert_eq!(read, 1);
            }
            cache.published_material_under("opencode", store)
        };

        assert_eq!(
            verdict(
                Some(published_rows(&[("stable", "a cached channel")])),
                true
            ),
            MaterialVerdict::Retained
        );
        // A second channel's rows, published uncached beside a cached one.
        assert_eq!(
            verdict(
                Some(published_rows(&[
                    ("stable", "a cached channel"),
                    ("nightly", "a nightly channel"),
                ])),
                true
            ),
            MaterialVerdict::Drops
        );
        assert_eq!(verdict(None, true), MaterialVerdict::Unknown);
        assert_eq!(
            verdict(
                Some(published_rows(&[("stable", "a cached channel")])),
                false
            ),
            MaterialVerdict::Unknown
        );
    }

    #[test]
    fn a_seed_is_admitted_only_past_failed_units_and_with_the_attribution_it_published() {
        use std::path::{Path, PathBuf};

        let root = std::env::var_os("CLINE_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| crate::ingest::home().join(".cline"))
            .join("data");
        let torn_task = root.join("tasks/2/api_conversation_history.json");
        let history = root.join("state/taskHistory.json");
        let published = |project: &str, model: Option<&str>| crate::cache::PublishedAgentRows {
            turns: HashMap::from([(
                ("session".to_owned(), 0),
                crate::cache::PublishedTurn {
                    project: project.to_owned(),
                    model: model.map(str::to_owned),
                    ts: 1,
                    text: "a readable task".to_owned(),
                },
            )]),
            sessions: HashSet::from(["session".to_owned()]),
            event_sessions: HashSet::new(),
        };
        let admitted = |issue: &Path, rows: crate::cache::PublishedAgentRows| {
            let mut cache = IngestCache::cold();
            cache.set_published_material(HashSet::from([torn_task.clone()]));
            let mut row = test_message("a readable task");
            row.agent = "cline";
            row.project = "delta".into();
            row.model = "claude-fable-5".into();
            cache.guard_never_empty("cline", &root, vec![row], &[], ReadOutcome::Invalid);
            cache.record_source_read_issue("cline", issue, "source-invalid", "torn");
            let mut once = Some(rows);
            let mut read = false;
            cache.admit_fully_served_whole_store_agents(std::iter::empty(), |_| {
                read = true;
                once.take()
            });
            (cache.output_complete(), read)
        };

        assert_eq!(
            admitted(&torn_task, published("delta", Some("claude-fable-5"))),
            (true, true)
        );
        assert_eq!(admitted(&torn_task, published("delta", None)), (true, true));
        assert_eq!(
            admitted(&torn_task, published("cline", Some("claude-fable-5"))),
            (false, true),
            "a seed that re-attributes a published row is not that row"
        );
        assert_eq!(
            admitted(&torn_task, published("delta", Some("another-model"))),
            (false, true)
        );
        // An index every task draws on failed: the seed's attribution proves nothing.
        assert_eq!(
            admitted(&history, published("delta", Some("claude-fable-5"))),
            (false, false)
        );
    }

    #[test]
    fn deleted_source_session_is_named_in_incremental_delta() {
        let root = std::env::temp_dir().join(format!(
            "agrep-deleted-source-delta-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let kept = root.join("kept.jsonl");
        let deleted = root.join("deleted.jsonl");
        fs::write(&kept, b"kept").unwrap();
        fs::write(&deleted, b"deleted").unwrap();

        let mut cache = IngestCache::cold();
        collect_cached(
            &mut cache,
            &root,
            &[kept.clone(), deleted.clone()],
            |path| {
                let mut message = test_message("row");
                message.session = if path == kept {
                    "live-session".into()
                } else {
                    "deleted-session".into()
                };
                (vec![message], Vec::new())
            },
        );
        cache.touched.clear();

        fs::remove_file(&deleted).unwrap();
        let pass = collect_cached(
            &mut cache,
            &root,
            std::slice::from_ref(&kept),
            |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!("the surviving source is a cache hit")
            },
        );
        assert_eq!(pass.messages.len(), 1);
        assert_eq!(pass.messages[0].session.as_ref(), "live-session");
        assert!(cache.touched.contains("deleted-session"));
        assert!(!cache.touched.contains("live-session"));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn deleted_event_only_source_is_exactly_prunable() {
        let root = std::env::temp_dir().join(format!(
            "agrep-deleted-event-source-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("event-only.jsonl");
        fs::write(&source, b"event").unwrap();
        let fname = crate::cache::event_fname("claude", "event-session");

        let mut cache = IngestCache::cold();
        let first = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (
                Vec::new(),
                vec![test_event("event-session", "event-call")],
                true,
            )
        });
        assert!(first.messages.is_empty());
        assert_eq!(first.events.len(), 1);
        assert_eq!(cache.live_event_files(), HashSet::from([fname.clone()]));

        fs::remove_file(&source).unwrap();
        let deleted = collect_cached(
            &mut cache,
            &root,
            &[],
            |_| -> (Vec<Message>, Vec<Event>, bool) { unreachable!() },
        );
        assert!(deleted.messages.is_empty());
        assert!(deleted.events.is_empty());
        assert!(cache.live_event_files().is_empty());
        assert_eq!(
            cache.event_prune_files(&cache.live_event_files()),
            HashSet::from([fname])
        );

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(not(windows))]
    #[test]
    fn v8_cache_migrates_and_force_populates_exact_event_ownership() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-v8-migration-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join(".ingest_cache.bin");
        fs::write(&source, b"source").unwrap();
        let key = source_key(&source);
        let legacy = LegacyCacheFileV8 {
            version: 8,
            entries: std::collections::HashMap::from([(
                key.clone(),
                LegacyEntryV8 {
                    mtime: 1,
                    size: 6,
                    msgs: vec![super::LegacyCMsgV37::from(&test_message("legacy"))],
                },
            )]),
        };
        fs::write(&cache_path, bincode::serialize(&legacy).unwrap()).unwrap();
        fs::write(
            root.join("intake_stats.json"),
            serde_json::to_vec(&serde_json::json!({
                "files": {
                    (key.clone()): {
                        "agent": "claude",
                        "events": 1
                    }
                }
            }))
            .unwrap(),
        )
        .unwrap();

        let mut cache = IngestCache::load(&cache_path);
        assert!(!cache.warm);
        assert!(cache.entries[&key].legacy_had_events);
        assert!(cache.entries[&key].legacy_needs_reparse);
        let pass = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (
                vec![test_message("migrated")],
                vec![test_event("migrated-session", "call")],
                true,
            )
        });
        assert_eq!(pass.messages[0].text.as_ref(), "migrated");
        assert_eq!(pass.events.len(), 1);
        let entry = &cache.entries[&key];
        assert!(!entry.legacy_had_events);
        assert!(!entry.legacy_needs_reparse);
        assert_eq!(
            cache.live_event_files(),
            HashSet::from([crate::cache::event_fname("claude", "migrated-session")])
        );

        // Source moves after a healthy parse but before postflight: v9 progress commits with the
        // pending marker set, old stat key makes the retry a miss, other migrated entries hit.
        fs::write(&source, b"source moved after migration").unwrap();
        assert!(cache.source_snapshot_safe());
        cache.save(&cache_path).unwrap();
        let wrapped = fs::read(&cache_path).unwrap();
        assert_eq!(
            super::read_u32(&wrapped, 20),
            Some(super::CACHE_BASE_STORAGE_VERSION)
        );
        let (mut retry, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        assert!(retry.warm);
        let reparsed = collect_cached(&mut retry, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("post-move")], Vec::new())
        });
        assert_eq!(reparsed.parsed, 1);
        assert_eq!(reparsed.messages[0].text.as_ref(), "post-move");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn empty_affected_sibling_serves_cache_and_blocks_partial_events() {
        let root = std::env::temp_dir().join(format!(
            "agrep-empty-sibling-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let changed = root.join("changed.jsonl");
        let sibling = root.join("sibling.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&changed, b"old").unwrap();
        fs::write(&sibling, b"sibling").unwrap();

        let mut cache = IngestCache::cold();
        collect_cached(
            &mut cache,
            &root,
            &[changed.clone(), sibling.clone()],
            |path| {
                let mut message = test_message(if path == changed { "old" } else { "sibling" });
                message.session = "shared-session".into();
                (
                    vec![message],
                    if path == changed {
                        vec![test_event("shared-session", "old-call")]
                    } else {
                        Vec::new()
                    },
                )
            },
        );
        cache.save(&cache_path).unwrap();

        // The first file is a real miss; the unchanged sibling shares its affected session but
        // simulates a transient empty read. Its cached row survives and no partial event set is
        // returned to overwrite the last-good per-session event file.
        fs::write(&changed, b"changed and larger").unwrap();
        let pass = collect_cached(
            &mut cache,
            &root,
            &[changed.clone(), sibling.clone()],
            |path| {
                if path == sibling {
                    return (Vec::new(), Vec::new(), false);
                }
                let mut message = test_message("fresh");
                message.session = "shared-session".into();
                (
                    vec![message],
                    vec![test_event("shared-session", "fresh-call")],
                    true,
                )
            },
        );
        let texts: HashSet<&str> = pass.messages.iter().map(|m| m.text.as_ref()).collect();
        assert_eq!(texts, HashSet::from(["fresh", "sibling"]));
        assert!(pass.events.is_empty());
        assert!(!cache.source_snapshot_safe());

        // Main drops (rather than commits) a cache stage for guarded normal runs. Loading the
        // last published generation must therefore see `changed` as a miss again and reparse
        // its affected sibling, yielding the complete two-source event set on retry.
        drop(cache.stage_save(&cache_path).unwrap());
        let mut retry = IngestCache::load(&cache_path);
        let recovered = collect_cached(
            &mut retry,
            &root,
            &[changed.clone(), sibling.clone()],
            |path| {
                let mut message = test_message(if path == changed { "fresh" } else { "sibling" });
                message.session = "shared-session".into();
                (
                    vec![message],
                    vec![test_event(
                        "shared-session",
                        if path == changed {
                            "fresh-call"
                        } else {
                            "sibling-call"
                        },
                    )],
                )
            },
        );
        assert_eq!(recovered.events.len(), 2);
        assert!(retry.source_snapshot_safe());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn discovered_unstatable_path_is_not_deleted() {
        let root = std::env::temp_dir().join(format!(
            "agrep-unstatable-discovered-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        fs::write(&source, b"source").unwrap();
        let key = source_key(&source);

        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("last-good")], Vec::new())
        });
        cache.touched.clear();
        // Discovery already returned this path, then it vanished before metadata: this is an
        // unstatable observation, not a confirmed absence from the discovery set.
        fs::remove_file(&source).unwrap();
        let pass = collect_cached(
            &mut cache,
            &root,
            std::slice::from_ref(&source),
            |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!("an unstatable path must not be parsed")
            },
        );
        assert_eq!(pass.messages.len(), 1);
        assert_eq!(pass.messages[0].text.as_ref(), "last-good");
        assert!(cache.entries.contains_key(&key));
        assert!(cache.touched.is_empty());
        assert!(!cache.source_snapshot_safe());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn staged_cache_does_not_advance_until_commit() {
        let root = std::env::temp_dir().join(format!(
            "agrep-staged-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"old").unwrap();
        let key = source_key(&source);

        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("old")], Vec::new())
        });
        cache.save(&cache_path).unwrap();
        fs::write(&source, b"new and larger").unwrap();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("new")], Vec::new())
        });

        drop(cache.stage_save(&cache_path).unwrap());
        let old = IngestCache::load(&cache_path);
        assert_eq!(old.entries[&key].msgs[0].text.as_ref(), "old");

        cache.stage_save(&cache_path).unwrap().commit().unwrap();
        let new = IngestCache::load(&cache_path);
        assert_eq!(new.entries[&key].msgs[0].text.as_ref(), "new");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn warm_delta_does_not_rewrite_the_base() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-delta-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"old").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("old")], Vec::new())
        });
        cold.save(&cache_path).unwrap();
        let base = fs::read(&cache_path).unwrap();

        let mut warm = IngestCache::load(&cache_path);
        fs::write(&source, b"new and larger").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("new")], Vec::new())
        });
        let stage = warm.stage_save(&cache_path).unwrap();
        assert_eq!(fs::read(&cache_path).unwrap(), base);
        assert!(!super::cache_journal_path(&cache_path).exists());
        drop(stage);
        assert_eq!(fs::read(&cache_path).unwrap(), base);
        assert!(!super::cache_journal_path(&cache_path).exists());

        warm.stage_save(&cache_path).unwrap().commit().unwrap();
        assert_eq!(fs::read(&cache_path).unwrap(), base);
        assert!(super::cache_journal_path(&cache_path).exists());
        let loaded = IngestCache::load(&cache_path);
        let key = source_key(&source);
        assert_eq!(loaded.entries[&key].msgs[0].text.as_ref(), "new");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn first_legacy_mutation_wraps_the_base_and_old_decoder_sees_no_hits() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-wrap-migration-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"legacy").unwrap();

        let mut seed = IngestCache::cold();
        collect_cached(&mut seed, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("legacy")], Vec::new())
        });
        let legacy = bincode::serialize(&super::CacheFileRef {
            version: CACHE_VERSION,
            entries: &seed.entries,
        })
        .unwrap();
        fs::write(&cache_path, &legacy).unwrap();
        let legacy_mtime = fs::metadata(&cache_path).unwrap().modified().unwrap();

        let mut warm = IngestCache::load(&cache_path);
        fs::write(&source, b"changed and larger").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("changed")], Vec::new())
        });
        let stale_stage = warm.stage_save(&cache_path).unwrap();
        let mut replaced = legacy.clone();
        *replaced.last_mut().unwrap() ^= 0x80;
        fs::write(&cache_path, replaced).unwrap();
        fs::File::options()
            .write(true)
            .open(&cache_path)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(legacy_mtime))
            .unwrap();
        assert!(stale_stage.commit().is_err());

        fs::write(&cache_path, &legacy).unwrap();
        fs::File::options()
            .write(true)
            .open(&cache_path)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(legacy_mtime))
            .unwrap();
        let stage = warm.stage_save(&cache_path).unwrap();
        assert!(matches!(stage.kind, super::StagedCacheKind::Base { .. }));
        stage.commit().unwrap();

        let wrapped = fs::read(&cache_path).unwrap();
        let old = bincode::deserialize::<super::CacheFile>(&wrapped).unwrap();
        assert_eq!(old.version, CACHE_VERSION);
        assert!(old.entries.is_empty());
        assert!(!super::cache_journal_path(&cache_path).exists());
        let loaded = IngestCache::load(&cache_path);
        let key = source_key(&source);
        assert_eq!(loaded.entries[&key].msgs[0].text.as_ref(), "changed");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn wrapped_base_tail_change_rejects_staged_delta() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-base-witness-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();
        let mut warm = IngestCache::load(&cache_path);
        fs::write(&source, b"delta").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("delta")], Vec::new())
        });
        let stage = warm.stage_save(&cache_path).unwrap();
        let mut changed = fs::read(&cache_path).unwrap();
        changed.push(0);
        fs::write(&cache_path, changed).unwrap();
        assert!(stage.commit().is_err());
        assert!(!super::cache_journal_path(&cache_path).exists());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn stale_delta_never_replaces_a_newer_base() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-stale-base-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();

        let mut stale = IngestCache::load(&cache_path);
        fs::write(&source, b"stale delta").unwrap();
        collect_cached(&mut stale, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("stale")], Vec::new())
        });
        let mut newer = IngestCache::cold();
        collect_cached(&mut newer, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("newer")], Vec::new())
        });
        newer.save(&cache_path).unwrap();
        assert!(stale.stage_save(&cache_path).is_err());

        let mut staged_stale = IngestCache::load(&cache_path);
        fs::write(&source, b"staged stale delta").unwrap();
        collect_cached(
            &mut staged_stale,
            &root,
            std::slice::from_ref(&source),
            |_| (vec![test_message("staged stale")], Vec::new()),
        );
        let stage = staged_stale.stage_save(&cache_path).unwrap();
        let mut newest = IngestCache::cold();
        collect_cached(&mut newest, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("newest")], Vec::new())
        });
        newest.save(&cache_path).unwrap();
        assert!(stage.commit().is_err());
        let loaded = IngestCache::load(&cache_path);
        let key = source_key(&source);
        assert_eq!(loaded.entries[&key].msgs[0].text.as_ref(), "newest");

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn journal_symlink_never_mutates_its_target() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!(
            "agrep-cache-journal-link-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let victim = root.join("victim.txt");
        let cache_path = root.join("cache.bin");
        let journal_path = super::cache_journal_path(&cache_path);
        fs::write(&source, b"base").unwrap();
        fs::write(&victim, b"do not touch").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();
        let mut first = IngestCache::load(&cache_path);
        fs::write(&source, b"first delta").unwrap();
        collect_cached(&mut first, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("first")], Vec::new())
        });
        first.stage_save(&cache_path).unwrap().commit().unwrap();

        let mut append = IngestCache::load(&cache_path);
        fs::write(&source, b"second delta").unwrap();
        collect_cached(&mut append, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("second")], Vec::new())
        });
        let stage = append.stage_save(&cache_path).unwrap();
        fs::remove_file(&journal_path).unwrap();
        symlink(&victim, &journal_path).unwrap();
        assert!(stage.commit().is_err());
        assert_eq!(fs::read(&victim).unwrap(), b"do not touch");

        // The link stands where the committed frame was: a writer cannot read it, so the frames
        // are lost and the base refused as stale; the rebuild unlinks the link, never its target.
        let mut reset = IngestCache::load(&cache_path);
        assert!(!reset.decoded_last_good_base());
        collect_cached(&mut reset, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("second")], Vec::new())
        });
        let head = reset.stage_save(&cache_path).unwrap().commit().unwrap();
        assert_eq!(head.sequence, 0);
        assert_eq!(fs::read(&victim).unwrap(), b"do not touch");
        assert!(fs::symlink_metadata(&journal_path).is_err());
        let loaded = IngestCache::load(&cache_path);
        assert!(loaded.decoded_last_good_base());
        assert_eq!(
            loaded.entries[&source_key(&source)].msgs[0].text.as_ref(),
            "second"
        );

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn cache_base_symlink_never_reads_or_mutates_its_target() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!(
            "agrep-cache-base-link-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let victim = root.join("victim-cache.bin");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();

        let mut victim_cache = IngestCache::cold();
        collect_cached(
            &mut victim_cache,
            &root,
            std::slice::from_ref(&source),
            |_| (vec![test_message("victim")], Vec::new()),
        );
        victim_cache.save(&victim).unwrap();
        let victim_bytes = fs::read(&victim).unwrap();
        symlink(&victim, &cache_path).unwrap();
        assert!(IngestCache::load(&cache_path).entries.is_empty());

        let mut replacement = IngestCache::cold();
        collect_cached(
            &mut replacement,
            &root,
            std::slice::from_ref(&source),
            |_| (vec![test_message("replacement")], Vec::new()),
        );
        replacement.save(&cache_path).unwrap();
        assert_eq!(fs::read(&victim).unwrap(), victim_bytes);
        assert!(!fs::symlink_metadata(&cache_path)
            .unwrap()
            .file_type()
            .is_symlink());

        let mut warm = IngestCache::load(&cache_path);
        fs::write(&source, b"delta").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("delta")], Vec::new())
        });
        let stage = warm.stage_save(&cache_path).unwrap();
        fs::remove_file(&cache_path).unwrap();
        symlink(&victim, &cache_path).unwrap();
        assert!(stage.commit().is_err());
        assert_eq!(fs::read(&victim).unwrap(), victim_bytes);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn oversized_overlay_rebases_instead_of_growing_forever() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-overlay-rebase-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();
        let huge = "x".repeat(super::JOURNAL_COMPACT_BUDGET + 1);
        let mut warm = IngestCache::load(&cache_path);
        fs::write(&source, b"large delta").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message(&huge)], Vec::new())
        });
        warm.stage_save(&cache_path).unwrap().commit().unwrap();

        let mut due = IngestCache::load(&cache_path);
        let super::CacheBacking::Delta { cursor, .. } = &due.backing else {
            panic!("wrapped base must support deltas")
        };
        cursor.lock().unwrap().frames = super::JOURNAL_COMPACT_FRAMES;
        fs::write(&source, b"another large delta").unwrap();
        collect_cached(&mut due, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message(&huge)], Vec::new())
        });
        let stale_rebase = due.stage_save(&cache_path).unwrap();
        assert!(matches!(
            stale_rebase.kind,
            super::StagedCacheKind::Base { .. }
        ));

        let mut writer = IngestCache::load(&cache_path);
        fs::write(&source, b"interleaved delta").unwrap();
        collect_cached(&mut writer, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("interleaved")], Vec::new())
        });
        writer.stage_save(&cache_path).unwrap().commit().unwrap();
        assert!(stale_rebase.commit().is_err());

        let mut rebase = IngestCache::load(&cache_path);
        let super::CacheBacking::Delta { cursor, .. } = &rebase.backing else {
            panic!("wrapped base must support deltas")
        };
        cursor.lock().unwrap().frames = super::JOURNAL_COMPACT_FRAMES;
        fs::write(&source, b"large delta after interleave").unwrap();
        collect_cached(&mut rebase, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message(&huge)], Vec::new())
        });
        let rebase_stage = rebase.stage_save(&cache_path).unwrap();
        assert!(matches!(
            rebase_stage.kind,
            super::StagedCacheKind::Base { .. }
        ));
        rebase_stage.commit().unwrap();
        assert!(!super::cache_journal_path(&cache_path).exists());

        let mut next = IngestCache::load(&cache_path);
        fs::write(&source, b"small delta after rebase").unwrap();
        collect_cached(&mut next, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("small")], Vec::new())
        });
        let stage = next.stage_save(&cache_path).unwrap();
        assert!(matches!(stage.kind, super::StagedCacheKind::Journal { .. }));
        stage.commit().unwrap();

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn oversized_delta_rewrites_base_instead_of_erroring() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-frame-limit-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let kept = root.join("kept.jsonl");
        let adopted = root.join("adopted.jsonl");
        let cache_path = root.join("cache.bin");
        let journal_path = super::cache_journal_path(&cache_path);
        fs::write(&kept, b"kept").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&kept), |_| {
            (vec![test_message("kept")], Vec::new())
        });
        cold.save(&cache_path).unwrap();

        // Adoption-scale publication: the delta cannot fit one journal frame under
        // the (test-lowered) limit, so the save must rewrite the base instead of
        // failing "ingest cache delta is too large" on every retry forever.
        super::TEST_MAX_FRAME_BYTES.with(|limit| limit.set(64));
        fs::write(&adopted, b"adopted").unwrap();
        let files = [kept.clone(), adopted.clone()];
        let mut warm = IngestCache::load(&cache_path);
        collect_cached(&mut warm, &root, &files, |path| {
            let text = if path.ends_with("adopted.jsonl") {
                "adopted"
            } else {
                "kept"
            };
            (vec![test_message(text)], Vec::new())
        });
        let stage = warm.stage_save(&cache_path).unwrap();
        assert!(matches!(stage.kind, super::StagedCacheKind::Base { .. }));
        stage.commit().unwrap();
        assert!(!journal_path.exists());

        let reopened = IngestCache::load(&cache_path);
        assert_eq!(reopened.entries.len(), 2);
        assert_eq!(
            reopened.entries[&source_key(&kept)].msgs[0].text.as_ref(),
            "kept"
        );
        assert_eq!(
            reopened.entries[&source_key(&adopted)].msgs[0]
                .text
                .as_ref(),
            "adopted"
        );

        // A small delta under the real limit still appends a journal frame.
        super::TEST_MAX_FRAME_BYTES.with(|limit| limit.set(0));
        fs::write(&kept, b"kept grew larger").unwrap();
        let mut small = IngestCache::load(&cache_path);
        collect_cached(&mut small, &root, &files, |_| {
            (vec![test_message("small")], Vec::new())
        });
        let stage = small.stage_save(&cache_path).unwrap();
        assert!(matches!(stage.kind, super::StagedCacheKind::Journal { .. }));
        stage.commit().unwrap();
        assert!(fs::metadata(&journal_path).unwrap().len() > 0);

        let reopened = IngestCache::load(&cache_path);
        assert_eq!(reopened.entries.len(), 2);
        assert_eq!(
            reopened.entries[&source_key(&kept)].msgs[0].text.as_ref(),
            "small"
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn warm_delta_persists_tombstones() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-tombstone-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let kept = root.join("kept.jsonl");
        let deleted = root.join("deleted.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&kept, b"kept").unwrap();
        fs::write(&deleted, b"deleted").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, &[kept.clone(), deleted.clone()], |path| {
            let mut message = test_message(&path.to_string_lossy());
            message.session = path
                .file_name()
                .unwrap()
                .to_string_lossy()
                .into_owned()
                .into();
            (vec![message], Vec::new())
        });
        cold.save(&cache_path).unwrap();

        let deleted_key = source_key(&deleted);
        fs::remove_file(&deleted).unwrap();
        let mut warm = IngestCache::load(&cache_path);
        warm.source_stamps.insert(kept.clone(), source_stamp(&kept));
        collect_cached(
            &mut warm,
            &root,
            std::slice::from_ref(&kept),
            |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!("the surviving source must remain a cache hit")
            },
        );
        warm.stage_save(&cache_path).unwrap().commit().unwrap();
        let loaded = IngestCache::load(&cache_path);
        assert!(loaded.entries.contains_key(&source_key(&kept)));
        assert!(!loaded.entries.contains_key(&deleted_key));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn incomplete_journal_tail_replays_only_committed_frames() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-tail-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();

        let mut first = IngestCache::load(&cache_path);
        fs::write(&source, b"first delta").unwrap();
        collect_cached(&mut first, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("first")], Vec::new())
        });
        first.stage_save(&cache_path).unwrap().commit().unwrap();

        let mut second = IngestCache::load(&cache_path);
        fs::write(&source, b"second delta is larger").unwrap();
        collect_cached(&mut second, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("second")], Vec::new())
        });
        let stage = second.stage_save(&cache_path).unwrap();
        let staged = match &stage.kind {
            super::StagedCacheKind::Journal { tmp, .. } => fs::read(tmp).unwrap(),
            _ => panic!("second warm mutation must stage one journal frame"),
        };
        drop(stage);
        let journal_path = super::cache_journal_path(&cache_path);
        let committed = fs::read(&journal_path).unwrap();
        let key = source_key(&source);
        let assert_ignored = |tail: &[u8]| {
            let mut damaged = committed.clone();
            damaged.extend_from_slice(tail);
            fs::write(&journal_path, damaged).unwrap();
            let loaded = IngestCache::load(&cache_path);
            assert_eq!(loaded.entries[&key].msgs[0].text.as_ref(), "first");
        };
        for cut in [1, super::FRAME_HEADER_LEN, staged.len() - 1] {
            assert_ignored(&staged[..cut]);
        }
        let mut corrupt_payload = staged.clone();
        corrupt_payload[super::FRAME_HEADER_LEN] ^= 0x80;
        assert_ignored(&corrupt_payload);

        let mut oversized = staged.clone();
        oversized[24..32].copy_from_slice(&u64::MAX.to_le_bytes());
        assert_ignored(&oversized);

        let footer = staged.len() - super::FRAME_FOOTER_LEN;
        for offset in [footer, footer + 8, footer + 16, footer + 24] {
            let mut corrupt_footer = staged.clone();
            corrupt_footer[offset] ^= 0x80;
            assert_ignored(&corrupt_footer);
        }

        let empty = super::JournalDelta {
            upserts: Vec::new(),
            deletes: Vec::new(),
        };
        assert_ignored(&super::encode_journal_frame(3, 4, &empty).unwrap());
        assert_ignored(&super::encode_journal_frame(0, 1, &empty).unwrap());

        let mut third_entry = second.entries[&key].clone();
        third_entry.msgs = vec![super::CMsg::from(&test_message("third"))];
        let third = super::JournalDelta {
            upserts: vec![(key.clone(), third_entry)],
            deletes: Vec::new(),
        };
        let mut broken_chain = corrupt_payload;
        broken_chain.extend_from_slice(&super::encode_journal_frame(2, 3, &third).unwrap());
        assert_ignored(&broken_chain);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn lost_last_journal_frame_reparses_its_changed_source() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-lost-tail-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();

        fs::write(&source, b"first delta").unwrap();
        let mut first = IngestCache::load(&cache_path);
        collect_cached(&mut first, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("first")], Vec::new())
        });
        first.stage_save(&cache_path).unwrap().commit().unwrap();
        let journal_path = super::cache_journal_path(&cache_path);
        let durable_prefix = fs::read(&journal_path).unwrap();

        fs::write(&source, b"second delta is larger").unwrap();
        let mut second = IngestCache::load(&cache_path);
        collect_cached(&mut second, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("second")], Vec::new())
        });
        second.stage_save(&cache_path).unwrap().commit().unwrap();
        fs::write(&journal_path, durable_prefix).unwrap();

        let mut retry = IngestCache::load(&cache_path);
        let pass = collect_cached(&mut retry, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("reparsed")], Vec::new())
        });
        assert_eq!(pass.parsed, 1);
        assert_eq!(pass.messages[0].text.as_ref(), "reparsed");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn journal_is_bound_to_its_base_instance() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-binding-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"old base").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("old")], Vec::new())
        });
        cold.save(&cache_path).unwrap();
        let mut warm = IngestCache::load(&cache_path);
        fs::write(&source, b"old delta").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("stale delta")], Vec::new())
        });
        warm.stage_save(&cache_path).unwrap().commit().unwrap();
        let journal_path = super::cache_journal_path(&cache_path);
        let stale_journal = fs::read(&journal_path).unwrap();

        fs::write(&source, b"replacement base").unwrap();
        let mut replacement = IngestCache::cold();
        collect_cached(
            &mut replacement,
            &root,
            std::slice::from_ref(&source),
            |_| (vec![test_message("replacement")], Vec::new()),
        );
        replacement.save(&cache_path).unwrap();
        fs::write(&journal_path, stale_journal).unwrap();
        let loaded = IngestCache::load(&cache_path);
        let key = source_key(&source);
        assert_eq!(loaded.entries[&key].msgs[0].text.as_ref(), "replacement");

        let _ = fs::remove_dir_all(root);
    }

    fn scratch_root(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        root
    }

    /// Commit `texts` as successive warm deltas over a cold base, returning the cache path.
    fn committed_deltas(root: &std::path::Path, texts: &[&str]) -> PathBuf {
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();
        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();
        for text in texts {
            fs::write(&source, format!("delta {text}")).unwrap();
            let mut warm = IngestCache::load(&cache_path);
            collect_cached(&mut warm, root, std::slice::from_ref(&source), |_| {
                (vec![test_message(text)], Vec::new())
            });
            warm.stage_save(&cache_path).unwrap().commit().unwrap();
        }
        cache_path
    }

    #[test]
    fn every_commit_records_the_head_it_establishes() {
        let root = scratch_root("head");
        let cache_path = committed_deltas(&root, &["first", "second"]);
        let head_path = super::cache_head_path(&cache_path);
        let head = super::read_cache_head(&cache_path).expect("the commits recorded a head");
        assert_eq!(head.sequence, 2);
        assert_eq!(
            fs::read_to_string(&head_path).unwrap(),
            head.encode(),
            "the sidecar is the head's text form"
        );
        let loaded = IngestCache::load(&cache_path);
        let super::CacheBacking::Delta {
            instance, cursor, ..
        } = &loaded.backing
        else {
            panic!("a committed base replays as a delta backing")
        };
        assert_eq!(*instance, head.instance);
        assert_eq!(cursor.lock().unwrap().sequence, 2);
        // A commit that changes nothing re-records the same head.
        let noop = loaded.stage_save(&cache_path).unwrap();
        assert!(matches!(noop.kind, super::StagedCacheKind::Noop { .. }));
        assert_eq!(noop.commit().unwrap(), head);
        // A base rewrite starts a new instance at sequence 0.
        let rewritten = IngestCache::cold()
            .stage_save(&cache_path)
            .unwrap()
            .commit()
            .unwrap();
        assert_ne!(rewritten.instance, head.instance);
        assert_eq!(rewritten.sequence, 0);
        assert_eq!(super::read_cache_head(&cache_path), Some(rewritten));
        assert_eq!(super::CacheHead::parse(b"not a head"), None);
        assert_eq!(
            super::CacheHead::parse(rewritten.encode().as_bytes()),
            Some(rewritten)
        );
        let _ = fs::remove_dir_all(root);
    }

    /// A directory in the head's place is moved aside, its contents intact, before the base or
    /// journal moves; one that cannot be moved refuses the commit naming the path with nothing
    /// changed, so the last commit's base, journal and head still agree.
    #[cfg(unix)]
    #[test]
    fn a_directory_in_the_heads_place_is_displaced_before_the_commit_or_refuses_it() {
        use std::os::unix::fs::PermissionsExt;

        let root = scratch_root("head-directory");
        let cache_path = committed_deltas(&root, &["first"]);
        let source = root.join("session.jsonl");
        let head_path = super::cache_head_path(&cache_path);
        let journal_path = super::cache_journal_path(&cache_path);
        let recorded = super::read_cache_head(&cache_path).unwrap();
        fs::remove_file(&head_path).unwrap();
        fs::create_dir(&head_path).unwrap();
        fs::write(head_path.join("keep.txt"), b"user content").unwrap();

        // Movable: the delta commits, the head is recorded, the directory survives aside.
        fs::write(&source, b"second delta").unwrap();
        let mut warm = IngestCache::load(&cache_path);
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("second")], Vec::new())
        });
        let head = warm.stage_save(&cache_path).unwrap().commit().unwrap();
        assert_eq!(head.sequence, recorded.sequence + 1);
        assert_eq!(super::read_cache_head(&cache_path), Some(head));
        let aside: Vec<_> = fs::read_dir(&root)
            .unwrap()
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .is_some_and(|name| name.to_string_lossy().contains(".head.displaced-"))
            })
            .collect();
        assert_eq!(aside.len(), 1);
        assert_eq!(
            fs::read(aside[0].join("keep.txt")).unwrap(),
            b"user content"
        );

        // Not movable (its parent read-only): the commit refuses naming the path before the
        // journal or base changed, and the committed state still decodes whole.
        let journal_before = fs::read(&journal_path).unwrap();
        fs::write(&source, b"third delta").unwrap();
        let mut blocked = IngestCache::load(&cache_path);
        assert!(blocked.decoded_last_good_base());
        collect_cached(&mut blocked, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("third")], Vec::new())
        });
        let stage = blocked.stage_save(&cache_path).unwrap();
        fs::remove_file(&head_path).unwrap();
        fs::create_dir(&head_path).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o555)).unwrap();
        let refused = if fs::rename(&head_path, root.join("probe")).is_ok() {
            // A privileged runner ignores the mode bits; nothing to refuse.
            fs::rename(root.join("probe"), &head_path).unwrap();
            None
        } else {
            Some(
                stage
                    .commit()
                    .expect_err("a blocked head refuses the commit"),
            )
        };
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        if let Some(error) = refused {
            let message = error.to_string();
            assert!(
                message.contains(&head_path.display().to_string())
                    && message.contains("could not be moved aside"),
                "{message}"
            );
            assert_eq!(fs::read(&journal_path).unwrap(), journal_before);
            assert!(fs::symlink_metadata(&head_path).is_ok_and(|m| m.is_dir()));
            fs::remove_dir(&head_path).unwrap();
            super::write_cache_head(&cache_path, head).unwrap();
            let loaded = IngestCache::load(&cache_path);
            assert!(loaded.decoded_last_good_base());
            assert_eq!(
                loaded.entries[&source_key(&source)].msgs[0].text.as_ref(),
                "second"
            );
        }
        let _ = fs::remove_dir_all(root);
    }

    /// A journal replaying short of the recorded head has lost frames: the base is older than
    /// what was committed from it, and every decode lane refuses it rather than serving it.
    #[test]
    fn a_journal_behind_its_last_commit_is_refused_in_every_lane() {
        let root = scratch_root("stale-journal");
        let cache_path = committed_deltas(&root, &["first", "second"]);
        let journal_path = super::cache_journal_path(&cache_path);
        let full = fs::read(&journal_path).unwrap();
        let writer = super::WriterBuildId::current();
        let refusal = |cache: &std::path::Path| super::decode_cache_for(cache, writer).err();
        use super::CacheDecodeRefusal;

        fs::remove_file(&journal_path).unwrap();
        assert_eq!(refusal(&cache_path), Some(CacheDecodeRefusal::StaleJournal));
        assert!(!CacheDecodeRefusal::StaleJournal.is_undecodable());
        assert!(IngestCache::load(&cache_path).entries.is_empty());
        assert!(!IngestCache::load(&cache_path).decoded_last_good_base());
        let (repair, refused) = IngestCache::repair(&cache_path);
        assert!(repair.discarded_base() && refused == Some(CacheDecodeRefusal::StaleJournal));
        let (retry, refused) = IngestCache::guarded_retry(&cache_path);
        assert!(retry.discarded_base() && refused == Some(CacheDecodeRefusal::StaleJournal));
        assert_eq!(
            super::adopt_foreign_cache(&cache_path),
            Err(CacheDecodeRefusal::StaleJournal.label())
        );

        // Only the newest frame lost: still short of the head.
        let instance = super::read_cache_head(&cache_path).unwrap().instance;
        let replayed = |path: &std::path::Path, entries: &mut HashMap<String, super::Entry>| {
            match super::replay_journal(path, instance, entries) {
                super::JournalReplay::Replayed(cursor) => cursor,
                super::JournalReplay::Unreadable(_) => panic!("a regular journal reads"),
            }
        };
        let mut cursor_entries = HashMap::new();
        fs::write(&journal_path, &full).unwrap();
        assert_eq!(replayed(&journal_path, &mut cursor_entries).sequence, 2);
        let first_frame_end = {
            let mut partial = HashMap::new();
            let mut probe = full.clone();
            probe.truncate(full.len() - 1);
            fs::write(&journal_path, &probe).unwrap();
            replayed(&journal_path, &mut partial).valid_len as usize
        };
        fs::write(&journal_path, &full[..first_frame_end]).unwrap();
        assert_eq!(refusal(&cache_path), Some(CacheDecodeRefusal::StaleJournal));

        // The journal whole again: trusted, and a torn append past the head still is.
        fs::write(&journal_path, &full).unwrap();
        assert!(refusal(&cache_path).is_none());
        let mut torn = full.clone();
        torn.extend_from_slice(b"a frame a kill cut short");
        fs::write(&journal_path, &torn).unwrap();
        assert!(refusal(&cache_path).is_none());

        // Unreadable under the lock (mode 000, a link, a directory): lost for every writer lane,
        // which refuses; the lock-free lane alone keeps reading the base.
        let source = root.join("session.jsonl");
        let unreadable = |shape: &str| {
            let _ = fs::remove_file(&journal_path);
            let _ = fs::remove_dir(&journal_path);
            match shape {
                #[cfg(unix)]
                "mode-000" => {
                    use std::os::unix::fs::PermissionsExt;
                    fs::write(&journal_path, &full).unwrap();
                    fs::set_permissions(&journal_path, fs::Permissions::from_mode(0o000)).unwrap();
                }
                #[cfg(unix)]
                "link" => {
                    let copy = root.join("journal-copy");
                    fs::write(&copy, &full).unwrap();
                    std::os::unix::fs::symlink(&copy, &journal_path).unwrap();
                }
                _ => fs::create_dir(&journal_path).unwrap(),
            }
        };
        let shapes: &[&str] = if cfg!(unix) {
            &["mode-000", "link", "directory"]
        } else {
            &["directory"]
        };
        for shape in shapes.iter().copied() {
            unreadable(shape);
            if shape == "mode-000" && fs::read(&journal_path).is_ok() {
                continue;
            }
            assert_eq!(
                refusal(&cache_path),
                Some(CacheDecodeRefusal::StaleJournal),
                "{shape}"
            );
            assert!(
                !IngestCache::load(&cache_path).decoded_last_good_base(),
                "{shape}"
            );
            assert_eq!(
                super::adopt_foreign_cache(&cache_path),
                Err(CacheDecodeRefusal::StaleJournal.label()),
                "{shape}"
            );
            assert_eq!(
                super::CacheView::read(&cache_path).map(|view| view.session_sources().len()),
                Ok(1),
                "{shape}: the lock-free lane reads the base"
            );
        }
        if fs::symlink_metadata(&journal_path).is_ok_and(|metadata| metadata.is_dir()) {
            // A directory in the journal's place is moved aside by the rebuild, never deleted.
            fs::write(journal_path.join("keep.txt"), b"user content").unwrap();
            let mut rebuilt = IngestCache::load(&cache_path);
            collect_cached(&mut rebuilt, &root, std::slice::from_ref(&source), |_| {
                (vec![test_message("rebuilt")], Vec::new())
            });
            let head = rebuilt.stage_save(&cache_path).unwrap().commit().unwrap();
            assert_eq!(head.sequence, 0);
            assert!(fs::symlink_metadata(&journal_path).is_err());
            let aside: Vec<_> = fs::read_dir(&root)
                .unwrap()
                .flatten()
                .map(|entry| entry.path())
                .filter(|path| {
                    path.file_name()
                        .is_some_and(|name| name.to_string_lossy().contains(".journal.displaced-"))
                })
                .collect();
            assert_eq!(aside.len(), 1);
            assert_eq!(
                fs::read(aside[0].join("keep.txt")).unwrap(),
                b"user content"
            );
            assert!(IngestCache::load(&cache_path).decoded_last_good_base());
        }
        let _ = fs::remove_dir_all(root);
    }

    /// Compaction folds the frames into one but keeps counting: the head a commit recorded stays
    /// comparable, so a compacted journal is never mistaken for a truncated one.
    #[test]
    fn compaction_keeps_the_journal_sequence_comparable_with_the_head() {
        let root = scratch_root("compaction-head");
        let cache_path = committed_deltas(&root, &["first", "second", "third"]);
        let source = root.join("session.jsonl");
        let mut due = IngestCache::load(&cache_path);
        let super::CacheBacking::Delta { cursor, .. } = &due.backing else {
            panic!("wrapped base must support deltas")
        };
        cursor.lock().unwrap().frames = super::JOURNAL_COMPACT_FRAMES;
        fs::write(&source, b"compacting delta").unwrap();
        collect_cached(&mut due, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("fourth")], Vec::new())
        });
        let stage = due.stage_save(&cache_path).unwrap();
        assert!(matches!(
            stage.kind,
            super::StagedCacheKind::Journal {
                mode: super::JournalWrite::Replace,
                ..
            }
        ));
        let head = stage.commit().unwrap();
        assert_eq!(head.sequence, 4);
        let loaded = IngestCache::load(&cache_path);
        assert!(loaded.decoded_last_good_base());
        let super::CacheBacking::Delta { cursor, .. } = &loaded.backing else {
            panic!("wrapped base must support deltas")
        };
        let state = cursor.lock().unwrap();
        assert_eq!((state.sequence, state.frames), (4, 1));
        drop(state);
        assert_eq!(
            loaded.entries[&source_key(&source)].msgs[0].text.as_ref(),
            "fourth"
        );
        let _ = fs::remove_dir_all(root);
    }

    /// A lock-free reader (why-source) may see a commit land between its two sidecar reads. Read
    /// head-first, a landed commit only adds frames past the head, so the base still decodes;
    /// read the other way round, the same commit would show a head past the frames and refuse.
    #[test]
    fn a_commit_landing_between_the_head_and_journal_reads_is_not_a_stale_journal() {
        let root = scratch_root("decode-race");
        let cache_path = committed_deltas(&root, &["first"]);
        let source = root.join("session.jsonl");
        let commit_between = {
            let (root, cache_path, source) = (root.clone(), cache_path.clone(), source.clone());
            move || {
                fs::write(&source, b"landed between the reads").unwrap();
                let mut warm = IngestCache::load(&cache_path);
                collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
                    (vec![test_message("landed")], Vec::new())
                });
                warm.stage_save(&cache_path).unwrap().commit().unwrap();
            }
        };
        super::BETWEEN_HEAD_AND_JOURNAL
            .with(|hook| *hook.borrow_mut() = Some(Box::new(commit_between)));
        let sources = super::CacheView::read(&cache_path)
            .expect("a healthy cache decodes")
            .session_sources();
        assert_eq!(sources.len(), 1);
        super::BETWEEN_HEAD_AND_JOURNAL
            .with(|hook| assert!(hook.borrow().is_none(), "the hook ran"));
        assert_eq!(super::read_cache_head(&cache_path).unwrap().sequence, 2);

        // The reverse race, a head ahead of a journal that is intact, is a loss and still refuses.
        let mut ahead = super::read_cache_head(&cache_path).unwrap();
        ahead.sequence += 1;
        super::write_cache_head(&cache_path, ahead).unwrap();
        assert_eq!(
            super::CacheView::read(&cache_path).err(),
            Some(super::CacheDecodeRefusal::StaleJournal)
        );
        let _ = fs::remove_dir_all(root);
    }

    /// The record packs every released row and session, reads back exactly, reads a 0.4.2 record
    /// too, and is serialized ahead of every source so a bounded prelude reader can skip it.
    #[test]
    fn a_released_record_packs_compactly_and_serializes_first() {
        let digest = |text: &str| crate::cache::text_digest(text);
        let mut rows = HashMap::new();
        rows.insert(("s1".to_owned(), 5_i64, digest("one")), 1_usize);
        rows.insert(("s1".to_owned(), 6, digest("two")), 2);
        rows.insert(("s2".to_owned(), 7, digest("three")), 1);
        let released = HashMap::from([
            (
                "claude".to_owned(),
                super::ReleasedRows {
                    rows: rows.clone(),
                    sessions: HashSet::from(["s2".to_owned(), "s3".to_owned()]),
                },
            ),
            ("codex".to_owned(), super::ReleasedRows::default()),
        ]);
        let seal = super::hex16(&[9; 16]);
        let (key, entry) = super::pack_released(&seal, &released).unwrap();
        assert_eq!(key, format!("{}{seal}", super::RELEASED_KEY));
        assert!(super::released_key(&key));
        let read =
            super::recorded_released_rows(&HashMap::from([(key.clone(), entry.clone())]), &seal);
        assert_eq!(
            read.len(),
            1,
            "an agent with nothing released records nothing"
        );
        assert_eq!(read["claude"].rows, rows);
        assert_eq!(
            read["claude"].sessions,
            HashSet::from(["s2".to_owned(), "s3".to_owned()])
        );
        let other = super::hex16(&[8; 16]);
        assert!(super::recorded_released_rows(
            &HashMap::from([(key.clone(), entry.clone())]),
            &other
        )
        .is_empty());
        // Equal records pack equal, whatever order the maps iterate in.
        let (again_key, again) = super::pack_released(&seal, &released).unwrap();
        assert!(again_key == key && again.identity == entry.identity);
        assert!(super::pack_released(&seal, &HashMap::new()).is_none());

        let legacy = format!(
            "{}{}",
            super::RELEASED_KEY,
            serde_json::json!({"seal": seal, "agents": {"claude": [
                [["s1", 5, super::hex16(&digest("one")), 1]], ["s3"]]}})
        );
        let legacy_entries = HashMap::from([(legacy, super::released_entry(Vec::new()))]);
        let read = super::recorded_released_rows(&legacy_entries, &seal);
        assert_eq!(read["claude"].rows.len(), 1);
        assert_eq!(read["claude"].sessions, HashSet::from(["s3".to_owned()]));

        // Synthetic sources keyed as this platform keys them: Windows refuses a bare path key.
        let synthetic_key = |index: u8| {
            let path = std::path::PathBuf::from(format!("/fixture/{index}.jsonl"));
            #[cfg(windows)]
            {
                let identity = crate::ingest::registry::WindowsFileIdentity {
                    kind: 1,
                    volume: 1,
                    id: [index; 16],
                };
                super::windows_source_key(&path, identity)
            }
            #[cfg(not(windows))]
            {
                super::source_key(&path)
            }
        };
        let mut entries: HashMap<String, super::Entry> = (0..64)
            .map(|index| (synthetic_key(index), cached_entry(&[test_message("row")])))
            .collect();
        entries.insert(key.clone(), entry);
        let payload = bincode::serialize(&super::CacheFileRef {
            version: CACHE_VERSION,
            entries: &entries,
        })
        .unwrap();
        let first_len = super::read_u64(&payload, 12).unwrap() as usize;
        assert_eq!(&payload[20..20 + first_len], key.as_bytes());
        let (decoded, _, _) =
            super::decode_cache_payload(&payload, std::path::Path::new("cache.bin")).unwrap();
        assert_eq!(decoded.len(), 65);
        assert_eq!(
            decoded
                .keys()
                .filter(|key| !super::released_key(key))
                .count(),
            64
        );
    }

    #[test]
    fn stale_staged_delta_cannot_overwrite_a_committed_delta() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cache-stale-stage-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"base").unwrap();

        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("base")], Vec::new())
        });
        cold.save(&cache_path).unwrap();
        let mut warm = IngestCache::load(&cache_path);
        fs::write(&source, b"first").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("first")], Vec::new())
        });
        let first = warm.stage_save(&cache_path).unwrap();
        fs::write(&source, b"second and larger").unwrap();
        collect_cached(&mut warm, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("second")], Vec::new())
        });
        let stale = warm.stage_save(&cache_path).unwrap();

        first.commit().unwrap();
        assert!(stale.commit().is_err());
        let loaded = IngestCache::load(&cache_path);
        let key = source_key(&source);
        assert_eq!(loaded.entries[&key].msgs[0].text.as_ref(), "first");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn guarded_retry_preserves_hits_and_reparses_only_changed_sources() {
        let root = std::env::temp_dir().join(format!(
            "agrep-guarded-incremental-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let store = root.join("store");
        fs::create_dir_all(&store).unwrap();
        let source = store.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"old").unwrap();

        let mut initial = IngestCache::cold();
        collect_cached(&mut initial, &store, std::slice::from_ref(&source), |_| {
            (vec![test_message("last-good")], Vec::new())
        });
        initial.save(&cache_path).unwrap();

        let (mut retry, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        assert!(retry.warm, "current cache should remain incremental");
        assert!(
            retry.repair_mode,
            "source disappearance guards must be active"
        );
        assert!(
            !retry.force_reparse,
            "a pending retry must preserve stat hits"
        );
        let hit = collect_cached(
            &mut retry,
            &store,
            std::slice::from_ref(&source),
            |_| -> (Vec<Message>, Vec<Event>) {
                panic!("an unchanged source was reparsed during guarded retry")
            },
        );
        assert_eq!(hit.parsed, 0);
        assert_eq!(hit.messages[0].text.as_ref(), "last-good");

        fs::write(&source, b"new and larger").unwrap();
        let changed = collect_cached(&mut retry, &store, std::slice::from_ref(&source), |_| {
            (vec![test_message("fresh")], Vec::new())
        });
        assert_eq!(changed.parsed, 1);
        assert_eq!(changed.messages[0].text.as_ref(), "fresh");

        // Preflight omission plus an exact NotFound under an existing root proves deletion.
        fs::remove_file(&source).unwrap();
        let missing = collect_cached(&mut retry, &store, &[], |_| -> (Vec<Message>, Vec<Event>) {
            unreachable!()
        });
        assert!(missing.messages.is_empty());
        assert!(retry.source_snapshot_safe());
        assert!(retry.has_provisional_deletions());
        assert!(!retry
            .entries
            .contains_key(source.to_string_lossy().as_ref()));

        // Losing the whole root is still indistinguishable from a transient unmount/share
        // failure. Guarded retry must serve the last-good generation, never provisional-delete.
        let (mut missing_root, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        fs::remove_dir_all(&store).unwrap();
        let served = collect_cached(
            &mut missing_root,
            &store,
            &[],
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert_eq!(served.messages.len(), 1);
        assert_eq!(served.messages[0].text.as_ref(), "last-good");
        assert!(!missing_root.source_snapshot_safe());
        assert!(!missing_root.has_provisional_deletions());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn guarded_retry_provisionally_accepts_exact_token_deletion_but_not_read_failure() {
        let root = std::env::temp_dir().join(format!(
            "agrep-guarded-token-delete-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");

        let tokens = vec![
            ("keep".to_string(), "v1".to_string()),
            ("deleted".to_string(), "v1".to_string()),
        ];
        let mut initial = IngestCache::cold();
        initial.collect_token_cached("cursor", Some(tokens.clone()), |sid| {
            let mut message = test_message(sid);
            message.session = sid.into();
            (vec![message], vec![test_event(sid, &format!("{sid}-call"))])
        });
        initial.save(&cache_path).unwrap();

        let current = vec![("keep".to_string(), "v1".to_string())];
        let (mut retry, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        retry.set_repair_expectations(HashSet::from(["cursor".into()]), HashSet::new());
        retry.set_source_coverage(
            HashSet::new(),
            HashMap::from([("cursor".to_string(), current.clone())]),
        );
        let pass = retry.collect_token_cached(
            "cursor",
            Some(current),
            |_| -> (Vec<Message>, Vec<Event>) {
                panic!("unchanged surviving token should remain a cache hit")
            },
        );
        assert_eq!(pass.messages.len(), 1);
        assert_eq!(pass.messages[0].session.as_ref(), "keep");
        assert!(retry.touched.contains("deleted"));
        assert!(retry.has_provisional_deletions());
        assert!(retry.source_snapshot_safe());
        assert_eq!(
            retry.event_prune_files(&retry.live_event_files()),
            HashSet::from([crate::cache::event_fname("claude", "deleted")])
        );

        // None/coverage mismatch is a DB read failure, not deletion evidence. It remains a
        // hard guard and serves both last-good conversations.
        let (mut unavailable, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        unavailable.set_source_coverage(
            HashSet::new(),
            HashMap::from([("cursor".to_string(), tokens)]),
        );
        let pass =
            unavailable.collect_token_cached("cursor", None, |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!()
            });
        assert_eq!(pass.messages.len(), 2);
        assert!(!unavailable.source_snapshot_safe());
        assert!(!unavailable.has_provisional_deletions());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn validated_event_repair_reparses_survivors_and_accepts_exact_token_deletion() {
        let root = std::env::temp_dir().join(format!(
            "agrep-event-repair-token-delete-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let tokens = vec![
            ("keep".to_string(), "v1".to_string()),
            ("deleted".to_string(), "v1".to_string()),
        ];
        let mut initial = IngestCache::cold();
        initial.collect_token_cached("cursor", Some(tokens.clone()), |sid| {
            let mut message = test_message(sid);
            message.session = sid.into();
            let mut event = test_event(sid, &format!("{sid}-call"));
            event.agent = "cursor";
            (vec![message], vec![event])
        });
        initial.save(&cache_path).unwrap();

        let current = vec![("keep".to_string(), "v1".to_string())];
        let (mut repair, refusal) = IngestCache::repair(&cache_path);
        assert!(refusal.is_none());
        repair.set_repair_expectations(HashSet::from(["cursor".into()]), HashSet::new());
        repair.set_source_coverage(
            HashSet::new(),
            HashMap::from([("cursor".to_string(), current.clone())]),
        );
        repair.allow_validated_repair_deletions();
        let pass = repair.collect_token_cached("cursor", Some(current), |sid| {
            let mut message = test_message("fresh");
            message.session = sid.into();
            let mut event = test_event(sid, "keep-call");
            event.agent = "cursor";
            (vec![message], vec![event])
        });
        assert_eq!(pass.parsed, 1, "event repair must force-parse the survivor");
        assert_eq!(pass.messages.len(), 1);
        assert_eq!(pass.messages[0].text.as_ref(), "fresh");
        assert_eq!(pass.events.len(), 1);
        assert!(repair.source_snapshot_safe());
        assert!(repair.has_provisional_deletions());
        assert!(repair.touched.contains("deleted"));
        assert_eq!(
            repair.event_prune_files(&repair.live_event_files()),
            HashSet::from([crate::cache::event_fname("cursor", "deleted")])
        );
        assert_eq!(
            repair.live_event_files(),
            HashSet::from([crate::cache::event_fname("cursor", "keep")])
        );

        // A database that becomes unreadable after preflight is never deletion evidence, even
        // under the explicitly validated event-repair contract.
        let (mut unavailable, refusal) = IngestCache::repair(&cache_path);
        assert!(refusal.is_none());
        unavailable.set_source_coverage(
            HashSet::new(),
            HashMap::from([("cursor".to_string(), tokens)]),
        );
        unavailable.allow_validated_repair_deletions();
        let pass =
            unavailable.collect_token_cached("cursor", None, |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!()
            });
        assert_eq!(pass.messages.len(), 2);
        assert!(!unavailable.source_snapshot_safe());
        assert!(!unavailable.has_provisional_deletions());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn repair_forces_parsing_but_keeps_last_good_guards() {
        let root = std::env::temp_dir().join(format!(
            "agrep-force-repair-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"unchanged source").unwrap();
        let mut initial = IngestCache::cold();
        collect_cached(&mut initial, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("last-good")], Vec::new())
        });
        initial.save(&cache_path).unwrap();

        let (mut repair, refusal) = IngestCache::repair(&cache_path);
        assert!(refusal.is_none());
        let pass = collect_cached(&mut repair, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new())
        });
        assert_eq!(pass.messages[0].text.as_ref(), "last-good");
        assert!(!repair.source_snapshot_safe());

        // A source snapshot may be the only fallback when the parse cache is corrupt. A path
        // observed by that snapshot but returning no rows/events during reconstruction is not
        // enough evidence to publish an empty event generation.
        let (mut no_fallback, refusal) = IngestCache::repair(&root.join("missing-cache.bin"));
        assert_eq!(refusal, Some(super::CacheDecodeRefusal::MissingFile));
        no_fallback.set_repair_expectations(
            HashSet::from(["claude".to_string()]),
            HashSet::from([source.clone()]),
        );
        let pass = collect_cached(
            &mut no_fallback,
            &root,
            std::slice::from_ref(&source),
            |_| (Vec::new(), Vec::new()),
        );
        assert!(pass.messages.is_empty());
        assert!(pass.events.is_empty());
        assert!(!no_fallback.source_snapshot_safe());
        assert!(!no_fallback
            .entries
            .contains_key(source.to_string_lossy().as_ref()));

        // A COMPLETE read that is truthfully empty (e.g. a workflow journal)
        // must not wedge the repair pass: the ambiguous no-outcome case above
        // stays guarded; the healthy one publishes.
        let (mut journal, refusal) = IngestCache::repair(&root.join("missing-cache-2.bin"));
        assert_eq!(refusal, Some(super::CacheDecodeRefusal::MissingFile));
        journal.set_repair_expectations(
            HashSet::from(["claude".to_string()]),
            HashSet::from([source.clone()]),
        );
        let pass = collect_cached(&mut journal, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new(), ReadOutcome::Complete)
        });
        assert!(pass.messages.is_empty());
        assert!(journal.output_complete());
        assert!(journal.source_snapshot_safe());

        // Empty message vectors are not empty source identities: a tool-event-only file has a
        // cache entry but its events intentionally are not serialized. Preserve that entry as a
        // repair sentinel and reject an ambiguous no-output reparse.
        let event_source = root.join("event-only.jsonl");
        let event_cache_path = root.join("event-cache.bin");
        fs::write(&event_source, b"event source").unwrap();
        let mut event_cache = IngestCache::cold();
        let first = collect_cached(
            &mut event_cache,
            &root,
            std::slice::from_ref(&event_source),
            |_| (Vec::new(), vec![test_event("event-session", "event-call")]),
        );
        assert_eq!(first.events.len(), 1);
        event_cache.save(&event_cache_path).unwrap();
        let (mut event_repair, refusal) = IngestCache::repair(&event_cache_path);
        assert!(refusal.is_none());
        let pass = collect_cached(
            &mut event_repair,
            &root,
            std::slice::from_ref(&event_source),
            |_| (Vec::new(), Vec::new()),
        );
        assert!(pass.events.is_empty());
        assert!(!event_repair.source_snapshot_safe());
        assert!(event_repair
            .entries
            .contains_key(&source_key(&event_source)));

        // Token stores reporting an expected-but-empty live set during automatic repair retain
        // cached conversations; only explicit --full may accept a genuine empty reset.
        let mut token = IngestCache::cold();
        token.collect_token_cached(
            "cursor",
            Some(vec![("conversation".into(), "v1".into())]),
            |_| (vec![test_message("token-last-good")], Vec::new()),
        );
        token.repair_mode = true;
        token.repair_expected_agents.insert("cursor".into());
        let pass = token.collect_token_cached(
            "cursor",
            Some(Vec::new()),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert_eq!(pass.messages[0].text.as_ref(), "token-last-good");
        assert!(!token.source_snapshot_safe());

        // A token entry with zero messages can still own uncached tool events. Its presence is
        // therefore a repair sentinel just like an empty per-file Entry.
        let mut event_token = IngestCache::cold();
        let first = event_token.collect_token_cached(
            "cursor",
            Some(vec![("event-conversation".into(), "v1".into())]),
            |_| {
                (
                    Vec::new(),
                    vec![test_event("event-conversation", "token-event")],
                )
            },
        );
        assert_eq!(first.events.len(), 1);
        event_token.repair_mode = true;
        event_token.force_reparse = true;
        let repaired = event_token.collect_token_cached(
            "cursor",
            Some(vec![("event-conversation".into(), "v1".into())]),
            |_| (Vec::new(), Vec::new()),
        );
        assert!(repaired.events.is_empty());
        assert!(!event_token.source_snapshot_safe());

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_v16_path_cache_rebuilds_cold_once() {
        let root = std::env::temp_dir().join(format!(
            "agrep-v16-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("Session.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"source").unwrap();
        let entries = HashMap::from([(
            source.to_string_lossy().to_lowercase(),
            super::Entry {
                mtime: 1,
                size: 6,
                identity: None,
                msgs: vec![CMsg::from(&test_message("v16"))],
                event_keys: Vec::new(),
                legacy_had_events: false,
                legacy_needs_reparse: false,
            },
        )]);
        let wire = super::CacheFileRef {
            version: 16,
            entries: &entries,
        };
        fs::write(&cache_path, bincode::serialize(&wire).unwrap()).unwrap();

        let mut cold = IngestCache::load(&cache_path);
        assert!(!cold.warm);
        assert!(cold.entries.is_empty());
        let rebuilt = collect_cached(&mut cold, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("current")], Vec::new())
        });
        assert_eq!(rebuilt.parsed, 1);
        cold.save(&cache_path).unwrap();
        let mut warm = IngestCache::load(&cache_path);
        let hit = collect_cached(
            &mut warm,
            &root,
            std::slice::from_ref(&source),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!("rebuilt current cache must hit") },
        );
        assert_eq!(hit.parsed, 0);

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_cache_hits_survive_path_case_changes() {
        let root = std::env::temp_dir().join(format!(
            "agrep-case-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("Session.JSONL");
        let cache_path = root.join("cache.bin");
        fs::write(&source, b"source").unwrap();

        let mut initial = IngestCache::cold();
        collect_cached(&mut initial, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("case-stable")], Vec::new())
        });
        initial.save(&cache_path).unwrap();

        let upper_root = PathBuf::from(root.to_string_lossy().to_uppercase());
        let upper_source = upper_root.join("SESSION.JSONL");
        let mut warm = IngestCache::load(&cache_path);
        let pass = collect_cached(
            &mut warm,
            &upper_root,
            std::slice::from_ref(&upper_source),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!("path casing caused a cache miss") },
        );
        assert_eq!(pass.parsed, 0);
        assert_eq!(pass.messages[0].text.as_ref(), "case-stable");
        assert_eq!(warm.entries.len(), 1);
        assert!(source_path_within(&upper_source, &root));
        assert_eq!(
            crate::ingest::registry::logical_path_key(&source),
            crate::ingest::registry::logical_path_key(&upper_source)
        );

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_alias_vanish_after_discovery_keeps_last_good() {
        let root = std::env::temp_dir().join(format!(
            "agrep-alias-vanish-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("Session.JSONL");
        fs::write(&source, b"source").unwrap();
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("last-good")], Vec::new())
        });
        let alias = root.join("SESSION.JSONL");
        fs::remove_file(&source).unwrap();

        let pass = collect_cached(
            &mut cache,
            &root,
            std::slice::from_ref(&alias),
            |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!("a vanished source cannot be parsed")
            },
        );
        assert_eq!(pass.messages[0].text.as_ref(), "last-good");
        assert_eq!(cache.entries.len(), 1);
        assert!(!cache.source_snapshot_safe());

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_case_sensitive_siblings_keep_distinct_cache_entries() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let root = std::env::temp_dir().join(format!(
            "agrep-sensitive-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        enable_case_sensitive(&root);

        let upper = root.join("rollout-A.jsonl");
        let lower = root.join("rollout-a.jsonl");
        let cache_path = root.join("cache.bin");
        fs::write(&upper, b"upper").unwrap();
        fs::write(&lower, b"lower").unwrap();
        crate::ingest::registry::reset_logical_path_key_calls();
        let files = vec![upper.clone(), lower.clone()];
        let mut cold = IngestCache::cold();
        let first = collect_cached(&mut cold, &root, &files, |path| {
            let marker = path.file_name().unwrap().to_string_lossy();
            (vec![test_message(&marker)], Vec::new())
        });
        assert_eq!(first.parsed, 2);
        assert_eq!(cold.entries.len(), 2);
        assert_ne!(source_key(&upper), source_key(&lower));
        cold.save(&cache_path).unwrap();

        let parses = AtomicUsize::new(0);
        let mut warm = IngestCache::load(&cache_path);
        let second = collect_cached(&mut warm, &root, &files, |_| {
            parses.fetch_add(1, Ordering::Relaxed);
            (Vec::<Message>::new(), Vec::<Event>::new())
        });
        let texts: HashSet<_> = second
            .messages
            .iter()
            .map(|message| message.text.as_ref())
            .collect();
        assert_eq!(second.parsed, 0);
        assert_eq!(parses.load(Ordering::Relaxed), 0);
        assert_eq!(crate::ingest::registry::logical_path_key_calls(), 0);
        assert_eq!(texts, HashSet::from(["rollout-A.jsonl", "rollout-a.jsonl"]));
        assert_ne!(
            crate::ingest::registry::logical_path_key(&upper),
            crate::ingest::registry::logical_path_key(&lower)
        );
        assert_ne!(
            crate::ingest::registry::logical_path_key(&root.join("missing").join("A.jsonl")),
            crate::ingest::registry::logical_path_key(&root.join("missing").join("a.jsonl"))
        );
        assert!(!source_path_within(
            &root.join("Store").join("session.jsonl"),
            &root.join("store")
        ));

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_sensitive_case_rename_reparses() {
        let root = std::env::temp_dir().join(format!(
            "agrep-sensitive-rename-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        enable_case_sensitive(&root);
        let upper = root.join("Session.jsonl");
        let lower = root.join("session.jsonl");
        fs::write(&upper, b"source").unwrap();

        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&upper), |_| {
            (vec![test_message("before")], Vec::new())
        });
        fs::rename(&upper, &lower).unwrap();
        let pass = collect_cached(&mut cache, &root, std::slice::from_ref(&lower), |_| {
            (vec![test_message("after")], Vec::new())
        });
        assert_eq!(pass.parsed, 1);
        assert_eq!(pass.messages[0].text.as_ref(), "after");
        assert_eq!(cache.entries.len(), 1);

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_hardlinks_remain_distinct_logical_sources() {
        let root = std::env::temp_dir().join(format!(
            "agrep-hardlink-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let first = root.join("first.jsonl");
        let second = root.join("second.jsonl");
        let first_alias = root.join("FIRST.JSONL");
        let cache_path = root.join("cache.bin");
        fs::write(&first, b"source").unwrap();
        fs::hard_link(&first, &second).unwrap();
        let files = vec![first, second, first_alias];

        let mut cache = IngestCache::cold();
        for path in &files[..2] {
            cache.source_stamps.insert(path.clone(), source_stamp(path));
        }
        let pass = collect_cached(&mut cache, &root, &files, |path| {
            let name = path.file_name().unwrap().to_string_lossy();
            let mut message = test_message(&name);
            message.session = name.as_ref().into();
            (vec![message], Vec::new())
        });
        assert_eq!(pass.parsed, 2);
        assert_eq!(cache.entries.len(), 2);
        cache.save(&cache_path).unwrap();

        let mut warm = IngestCache::load(&cache_path);
        let hit = collect_cached(
            &mut warm,
            &root,
            &files,
            |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!("hardlink paths should remain independent cache hits")
            },
        );
        assert_eq!(hit.parsed, 0);
        fs::remove_file(&files[1]).unwrap();
        warm.touched.clear();
        let remaining = collect_cached(
            &mut warm,
            &root,
            &files[..1],
            |_| -> (Vec<Message>, Vec<Event>) {
                unreachable!("the surviving hardlink should remain a cache hit")
            },
        );
        assert_eq!(remaining.messages.len(), 1);
        assert_eq!(warm.entries.len(), 1);
        assert!(warm.touched.contains("second.jsonl"));

        let modified = fs::metadata(&files[0]).unwrap().modified().unwrap();
        fs::write(&files[0], b"change").unwrap();
        fs::OpenOptions::new()
            .write(true)
            .open(&files[0])
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(modified))
            .unwrap();
        let changed = collect_cached(&mut warm, &root, &files[..1], |_| {
            (vec![test_message("changed")], Vec::new())
        });
        assert_eq!(changed.parsed, 1);
        assert_eq!(changed.messages[0].text.as_ref(), "changed");

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_same_path_replacement_keeps_last_good_on_read_failure() {
        let root = std::env::temp_dir().join(format!(
            "agrep-replaced-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("session.jsonl");
        let old_file = root.join("old-session.jsonl");
        fs::write(&source, b"old").unwrap();
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("last-good")], Vec::new())
        });
        fs::rename(&source, &old_file).unwrap();
        fs::write(&source, b"replacement").unwrap();

        let pass = collect_cached(&mut cache, &root, std::slice::from_ref(&source), |_| {
            (Vec::new(), Vec::new(), ReadOutcome::Skipped)
        });
        assert_eq!(pass.messages[0].text.as_ref(), "last-good");
        assert_eq!(cache.entries.len(), 1);
        assert!(!cache.source_snapshot_safe());

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_source_key_roundtrips_unpaired_utf16() {
        use std::ffi::OsString;
        use std::os::windows::ffi::{OsStrExt, OsStringExt};

        let path = PathBuf::from(OsString::from_wide(&[0xd800, 0x61]));
        let identity = crate::ingest::registry::WindowsFileIdentity {
            kind: 1,
            volume: 1,
            id: [1; 16],
        };
        let key = super::windows_source_key(&path, identity);
        let decoded = super::source_path_from_key(&key).unwrap();
        assert_eq!(
            decoded.as_os_str().encode_wide().collect::<Vec<_>>(),
            [0xd800, 0x61]
        );
    }

    #[cfg(not(windows))]
    #[test]
    fn unix_path_checks_keep_native_component_semantics() {
        let root = PathBuf::from("/tmp/Store");
        let child = root.join("Session.jsonl");

        assert!(source_path_eq(&root, &root));
        assert!(!source_path_eq(&root, &PathBuf::from("/tmp/store")));
        assert!(source_path_within(&child, &root));
        assert!(!source_path_within(
            &PathBuf::from("/tmp/Store-copy"),
            &root
        ));
        assert_eq!(source_relative(&child, &root), Some("Session.jsonl".into()));
    }

    fn foreign_owned_cache(dir: &std::path::Path) -> (PathBuf, PathBuf) {
        let source = dir.join("session.jsonl");
        fs::write(&source, b"one line of transcript\n").unwrap();
        let cache_path = dir.join(".ingest_cache.bin");
        let mut cache = IngestCache::cold();
        collect_cached(&mut cache, dir, std::slice::from_ref(&source), |_path| {
            (vec![test_message("adopted row")], Vec::<Event>::new())
        });
        let foreign = super::WriterBuildId::parse(b"ffffffffffffffffffff").unwrap();
        let bytes = super::encode_cache_base_for(&cache.entries, foreign).unwrap();
        fs::write(&cache_path, bytes).unwrap();
        (cache_path, source)
    }

    #[test]
    fn takeover_adopts_a_verified_foreign_cache_without_reparsing() {
        let dir = std::env::temp_dir().join(format!(
            "agrep-adopt-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let (cache_path, source) = foreign_owned_cache(&dir);
        assert!(matches!(
            super::probe_cache_owner(&cache_path),
            super::CacheOwnerProbe::Foreign { .. }
        ));

        let adopted = super::adopt_foreign_cache(&cache_path).unwrap();
        assert!(adopted > 0, "adoption reported no entries");
        assert!(matches!(
            super::probe_cache_owner(&cache_path),
            super::CacheOwnerProbe::Current { .. }
        ));
        // the adopted entries serve as cache hits: an unchanged source must
        // never reach the parser again
        let mut reloaded = IngestCache::load(&cache_path);
        assert!(reloaded.warm, "adopted cache reloaded cold");
        let pass = collect_cached(
            &mut reloaded,
            &dir,
            &[source],
            |_path| -> (Vec<Message>, Vec<Event>) {
                panic!("adopted cache missed; source was reparsed")
            },
        );
        assert_eq!(pass.parsed, 0);
        assert_eq!(pass.messages.len(), 1);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_partial_read_is_cached_and_refreshed_only_by_a_read_that_loses_nothing() {
        let dir = std::env::temp_dir().join(format!(
            "agrep-partial-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let source = dir.join("store.db");
        fs::write(&source, b"store").unwrap();
        let row = |session: &str, text: &str| {
            let mut row = test_message(text);
            row.session = session.into();
            row
        };
        let texts = |pass: &super::Pass| {
            let mut texts: Vec<String> = pass.messages.iter().map(|m| m.text.to_string()).collect();
            texts.sort_unstable();
            texts
        };
        let mut cache = IngestCache::cold();
        let read = |cache: &mut IngestCache,
                    rows: Vec<Message>,
                    events: Vec<Event>,
                    outcome: ReadOutcome| {
            collect_cached(cache, &dir, std::slice::from_ref(&source), move |_| {
                (rows.clone(), events.clone(), outcome)
            })
        };

        let first = read(
            &mut cache,
            vec![row("a", "first")],
            vec![],
            ReadOutcome::Partial,
        );
        assert_eq!(texts(&first), ["first"]);
        let entry = &cache.entries[&source_key(&source)];
        assert_eq!(entry.msgs.len(), 1);
        assert!(entry.identity.is_none(), "a partial entry must retry");

        let rows = vec![row("a", "first"), row("b", "second")];
        let events = vec![test_event("a", "old-a"), test_event("b", "new-b")];
        let grown = read(&mut cache, rows, events, ReadOutcome::Partial);
        assert_eq!(texts(&grown), ["first", "second"]);
        let sessions: Vec<&str> = grown.events.iter().map(|e| e.session.as_str()).collect();
        assert_eq!(
            sessions,
            ["b"],
            "events of a session the entry held were rewritten"
        );

        let lost = read(
            &mut cache,
            vec![row("b", "second")],
            vec![],
            ReadOutcome::Partial,
        );
        assert_eq!(texts(&lost), ["first", "second"]);
        let failed = read(&mut cache, vec![], vec![], ReadOutcome::Invalid);
        assert_eq!(texts(&failed), ["first", "second"]);

        let complete = read(
            &mut cache,
            vec![row("c", "third")],
            vec![],
            ReadOutcome::Complete,
        );
        assert_eq!(texts(&complete), ["third"]);
        assert!(cache.entries[&source_key(&source)].identity.is_some());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn leftover_flagged_entries_reparse_one_by_one_on_a_warm_cache() {
        let dir = std::env::temp_dir().join(format!(
            "agrep-leftover-reparse-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let cache_path = dir.join(".ingest_cache.bin");
        let current = dir.join("current.jsonl");
        let leftover = dir.join("leftover.jsonl");
        fs::write(&current, b"current").unwrap();
        fs::write(&leftover, b"leftover").unwrap();
        let sources = [current.clone(), leftover.clone()];
        // One session per source, so no reparse re-reads the other as a sibling.
        let row = |path: &std::path::Path, text: &str| {
            crate::model::RawMessage {
                agent: "claude",
                project: "project".into(),
                session: path.file_stem().unwrap().to_string_lossy().into_owned(),
                ts: 1,
                turn: 0,
                text: text.into(),
                model: String::new(),
                reply: String::new(),
                reply_chars: 0,
                side: false,
                parent: String::new(),
                reply_shape: Default::default(),
            }
            .freeze()
        };
        let mut cold = IngestCache::cold();
        collect_cached(&mut cold, &dir, &sources, |path| {
            let text = format!("{} row", path.file_stem().unwrap().to_string_lossy());
            (vec![row(path, &text)], Vec::new(), ReadOutcome::Complete)
        });
        let flagged = |keys: &[&std::path::Path]| {
            let mut entries = cold.entries.clone();
            for key in keys {
                entries
                    .get_mut(&source_key(key))
                    .unwrap()
                    .legacy_needs_reparse = true;
            }
            IngestCache::base(entries, super::CacheBacking::Rewrite)
                .save(&cache_path)
                .unwrap();
        };

        // Every entry flagged is an older generation takeover re-encoded: it reparses whole.
        flagged(&[&current, &leftover]);
        let adopted = IngestCache::load(&cache_path);
        assert!(!adopted.warm && adopted.force_reparse);

        flagged(&[&leftover]);
        let mut warm = IngestCache::load(&cache_path);
        assert!(warm.warm && !warm.force_reparse);
        let pass = collect_cached(&mut warm, &dir, &sources, |_| {
            (Vec::new(), Vec::new(), ReadOutcome::Invalid)
        });
        assert_eq!(pass.parsed, 1, "only the flagged entry reparses");
        let mut texts: Vec<&str> = pass.messages.iter().map(|m| m.text.as_ref()).collect();
        texts.sort_unstable();
        assert_eq!(texts, ["current row", "leftover row"]);
        assert!(warm.entries[&source_key(&leftover)].legacy_needs_reparse);
        assert!(!warm.entries[&source_key(&current)].legacy_needs_reparse);

        let pass = collect_cached(&mut warm, &dir, &sources, |path| {
            (
                vec![row(path, "leftover reparsed")],
                Vec::new(),
                ReadOutcome::Complete,
            )
        });
        assert_eq!(pass.parsed, 1);
        let mut texts: Vec<&str> = pass.messages.iter().map(|m| m.text.as_ref()).collect();
        texts.sort_unstable();
        assert_eq!(texts, ["current row", "leftover reparsed"]);
        assert!(!warm.entries[&source_key(&leftover)].legacy_needs_reparse);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn takeover_adopts_released_cache_versions_with_last_good_rows() {
        for (version, journaled) in [
            (36_u32, false),
            (36, true),
            (35, false),
            (35, true),
            (34, false),
            (34, true),
            (33, false),
            (33, true),
            (32, false),
            (32, true),
            (31, false),
            (31, true),
            (30, false),
            (30, true),
            (29, false),
            (29, true),
            (28, false),
            (28, true),
            (27, false),
            (27, true),
            (26, false),
            (26, true),
            (25, false),
            (25, true),
            (24, false),
            (24, true),
            (22, false),
            (22, true),
            (21, false),
        ] {
            let dir = std::env::temp_dir().join(format!(
                "agrep-adopt-v{version}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            fs::create_dir_all(&dir).unwrap();
            let source = dir.join("session.jsonl");
            fs::write(&source, b"last-good transcript").unwrap();
            let cache_path = dir.join(".ingest_cache.bin");
            let mut cache = IngestCache::cold();
            collect_cached(&mut cache, &dir, std::slice::from_ref(&source), |_| {
                (
                    vec![test_message("released last-good row")],
                    Vec::<Event>::new(),
                )
            });
            // a released generation wrote its own (reply-shape-less) row layout
            let payload = legacy_payload_v37(version, &cache.entries);
            let mut bytes = Vec::new();
            bytes.extend_from_slice(&version.to_le_bytes());
            bytes.extend_from_slice(&0_u64.to_le_bytes());
            bytes.extend_from_slice(super::CACHE_BASE_MAGIC);
            bytes.extend_from_slice(&super::CACHE_BASE_STORAGE_VERSION.to_le_bytes());
            bytes.extend_from_slice(b"ffffffffffffffffffff");
            bytes.extend_from_slice(&[7_u8; 16]);
            bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
            bytes.extend_from_slice(&super::cache_digest(&payload));
            bytes.extend_from_slice(&super::CACHE_CODEC_NONE.to_le_bytes());
            bytes.extend_from_slice(&0_u32.to_le_bytes());
            bytes.extend_from_slice(&(payload.len() as u64).to_le_bytes());
            bytes.extend_from_slice(&payload);
            fs::write(&cache_path, bytes).unwrap();
            let expected = if journaled {
                let mut entry = cache.entries[&source_key(&source)].clone();
                entry.msgs[0].text = "released journal row".into();
                let delta = bincode::serialize(&super::LegacyJournalDeltaV37 {
                    upserts: vec![(
                        source_key(&source),
                        super::LegacyEntryV37::downgrade(&entry),
                    )],
                    deletes: Vec::new(),
                })
                .unwrap();
                write_journal_frame(&super::cache_journal_path(&cache_path), [7_u8; 16], &delta);
                "released journal row"
            } else {
                "released last-good row"
            };
            assert!(matches!(
                super::probe_cache_owner(&cache_path),
                super::CacheOwnerProbe::Foreign { .. }
            ));

            assert_eq!(super::adopt_foreign_cache(&cache_path), Ok(1), "v{version}");
            assert!(matches!(
                super::probe_cache_owner(&cache_path),
                super::CacheOwnerProbe::Current { .. }
            ));
            let mut reloaded = IngestCache::load(&cache_path);
            assert!(reloaded.force_reparse, "v{version}");
            assert!(reloaded.entries[&source_key(&source)].legacy_needs_reparse);
            let pass = collect_cached(&mut reloaded, &dir, &[source], |_| {
                (Vec::new(), Vec::new(), ReadOutcome::Skipped)
            });
            assert_eq!(pass.parsed, 1);
            assert_eq!(pass.messages.len(), 1);
            assert_eq!(pass.messages[0].text.as_ref(), expected);
            assert!(!reloaded.source_snapshot_safe());
            fs::remove_dir_all(&dir).unwrap();
        }
    }

    #[test]
    fn takeover_refuses_to_adopt_a_corrupt_foreign_cache() {
        let dir = std::env::temp_dir().join(format!(
            "agrep-adopt-corrupt-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        let (cache_path, _source) = foreign_owned_cache(&dir);
        let mut bytes = fs::read(&cache_path).unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 0xFF;
        fs::write(&cache_path, bytes).unwrap();

        super::adopt_foreign_cache(&cache_path)
            .expect_err("a corrupt foreign cache must fall back to discard");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn validly_empty_token_store_confirms_only_after_repeated_observation() {
        let root = std::env::temp_dir().join(format!(
            "agrep-empty-token-store-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let cache_path = root.join("cache.bin");
        let tokens = vec![("only".to_string(), "v1".to_string())];
        let mut initial = IngestCache::cold();
        initial.collect_token_cached("cursor", Some(tokens), |sid| {
            let mut message = test_message(sid);
            message.session = sid.into();
            (vec![message], Vec::<Event>::new())
        });
        initial.save(&cache_path).unwrap();

        // A normal warm pass over the emptied store serves last-good and stays guarded.
        let mut warm = IngestCache::load(&cache_path);
        let pass = warm.collect_token_cached(
            "cursor",
            Some(Vec::new()),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert_eq!(pass.messages.len(), 1);
        assert!(!warm.source_snapshot_safe());
        assert!(!warm.has_provisional_deletions());

        // The first guarded retry is still a single observation: withhold again.
        let (mut unconfirmed, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        let pass = unconfirmed.collect_token_cached(
            "cursor",
            Some(Vec::new()),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert_eq!(pass.messages.len(), 1);
        assert!(!unconfirmed.source_snapshot_safe());
        assert!(!unconfirmed.has_provisional_deletions());

        // Two agreeing complete observations confirm: the last conversation is deletable.
        let (mut retry, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        retry.allow_repeated_missing_roots(HashSet::from(["cursor".to_string()]));
        let pass = retry.collect_token_cached(
            "cursor",
            Some(Vec::new()),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert!(pass.messages.is_empty());
        assert!(retry.source_snapshot_safe());
        assert!(retry.has_provisional_deletions());
        assert!(retry.touched.contains("only"));

        // Torn/garbage protection intact: an unopenable store is never deletion evidence.
        let (mut torn, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        torn.allow_repeated_missing_roots(HashSet::from(["cursor".to_string()]));
        let pass = torn.collect_token_cached("cursor", None, |_| -> (Vec<Message>, Vec<Event>) {
            unreachable!()
        });
        assert_eq!(pass.messages.len(), 1);
        assert!(!torn.source_snapshot_safe());
        assert!(!torn.has_provisional_deletions());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn cold_full_accepts_only_a_confirmed_empty_token_store() {
        // --full's cold cache participates once the durable repeated observation exists...
        let mut confirmed = IngestCache::cold();
        confirmed.set_repair_expectations(HashSet::from(["cursor".into()]), HashSet::new());
        confirmed.allow_repeated_missing_roots(HashSet::from(["cursor".to_string()]));
        let pass = confirmed.collect_token_cached(
            "cursor",
            Some(Vec::new()),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert!(pass.messages.is_empty());
        assert!(confirmed.source_snapshot_safe());

        // ...and stays guarded on a first observation of an expected store's emptiness.
        let mut unconfirmed = IngestCache::cold();
        unconfirmed.set_repair_expectations(HashSet::from(["cursor".into()]), HashSet::new());
        let pass = unconfirmed.collect_token_cached(
            "cursor",
            Some(Vec::new()),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        assert!(pass.messages.is_empty());
        assert!(!unconfirmed.source_snapshot_safe());
        assert!(!unconfirmed.has_provisional_deletions());
    }
}

/// Component-wise metadata prevents a swapped parent link from escaping the adapter root.
fn source_metadata(root: &Path, path: &Path) -> std::io::Result<Option<fs::Metadata>> {
    use std::path::Component;

    let mut current = root.to_path_buf();
    let root_metadata = match fs::symlink_metadata(&current) {
        Ok(metadata) => metadata,
        Err(error) if crate::ingest::registry::stat_proves_absent(&current, &error) => {
            return Ok(None)
        }
        Err(error) => return Err(error),
    };
    if crate::ingest::registry::metadata_is_link(&root_metadata) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "symlink source roots are not followed",
        ));
    }
    if root_metadata.is_file() {
        if source_path_eq(path, root) {
            return Ok(Some(root_metadata));
        }
        if !source_path_eq(path, &crate::ingest::sqlite_sidecar(root, "-wal")) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "cached source escaped its file root",
            ));
        }
        let metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if crate::ingest::registry::stat_proves_absent(path, &error) => {
                return Ok(None)
            }
            Err(error) => return Err(error),
        };
        if crate::ingest::registry::metadata_is_link(&metadata) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "symlink SQLite sidecars are not followed",
            ));
        }
        return Ok(metadata.is_file().then_some(metadata));
    }
    if !root_metadata.is_dir() {
        return Ok(None);
    }
    let relative = source_relative(path, root).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "cached source escaped its store root",
        )
    })?;
    let components: Vec<_> = relative.components().collect();
    for (index, component) in components.iter().enumerate() {
        let Component::Normal(name) = component else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "cached source path is not normalized",
            ));
        };
        current.push(name);
        let metadata = match fs::symlink_metadata(&current) {
            Ok(metadata) => metadata,
            Err(error) if crate::ingest::registry::stat_proves_absent(&current, &error) => {
                return Ok(None)
            }
            Err(error) => return Err(error),
        };
        if crate::ingest::registry::metadata_is_link(&metadata) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "symlink source paths are not followed",
            ));
        }
        if index + 1 < components.len() && !metadata.is_dir() {
            return Ok(None);
        }
        if index + 1 == components.len() {
            return Ok(metadata.is_file().then_some(metadata));
        }
    }
    Ok(None)
}

fn single_file_stat(
    root: &Path,
    p: &Path,
    sqlite: bool,
) -> std::io::Result<Option<(i64, u64, crate::ingest::registry::ChangeToken)>> {
    let m = match source_metadata(root, p) {
        Ok(Some(metadata)) => metadata,
        Ok(None) => return Ok(None),
        Err(error) if crate::ingest::registry::stat_proves_absent(p, &error) => return Ok(None),
        Err(error) => return Err(error),
    };
    let mtime = m
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|duration| i64::try_from(duration.as_nanos()).unwrap_or(i64::MAX))
        .unwrap_or(0);
    #[cfg(target_os = "linux")]
    let identity = if sqlite {
        crate::ingest::sqlite_stable_change_token(p, &m)?
    } else {
        crate::ingest::registry::metadata_change_token(p, &m)?
    };
    #[cfg(not(target_os = "linux"))]
    let identity = {
        let _ = sqlite;
        crate::ingest::registry::metadata_change_token(p, &m)?
    };
    Ok(Some((mtime, m.len(), identity)))
}

#[cfg(windows)]
fn single_file_stat_identified(
    root: &Path,
    p: &Path,
) -> std::io::Result<
    Option<(
        i64,
        u64,
        crate::ingest::registry::ChangeToken,
        crate::ingest::registry::WindowsFileIdentity,
    )>,
> {
    let m = match source_metadata(root, p) {
        Ok(Some(metadata)) => metadata,
        Ok(None) => return Ok(None),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mtime = m
        .modified()
        .ok()
        .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|duration| i64::try_from(duration.as_nanos()).unwrap_or(i64::MAX))
        .unwrap_or(0);
    let (change, file) = crate::ingest::registry::metadata_change_token_with_file_identity(p, &m)?;
    Ok(Some((mtime, m.len(), change, file)))
}

fn sqlite_database(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            [".db", ".sqlite", ".sqlite3", ".vscdb"]
                .iter()
                .any(|suffix| name.ends_with(suffix))
        })
}

#[cfg(not(windows))]
fn file_stat(root: &Path, p: &Path) -> std::io::Result<Option<(i64, u64, CacheIdentity)>> {
    let database = sqlite_database(p);
    let Some((mut mtime, mut size, identity)) = single_file_stat(root, p, database)? else {
        return Ok(None);
    };
    let mut identities = vec![identity];
    if database {
        let wal = crate::ingest::sqlite_sidecar(p, "-wal");
        if let Some((wal_mtime, wal_size, wal_identity)) = single_file_stat(root, &wal, true)? {
            mtime = mtime.max(wal_mtime);
            size = size.wrapping_add(wal_size.rotate_left(7));
            identities.push(wal_identity);
        }
    }
    Ok(Some((mtime, size, CacheIdentity::Source(identities))))
}

#[cfg(windows)]
fn identified_file_stat(
    root: &Path,
    p: &Path,
) -> std::io::Result<Option<(String, i64, u64, CacheIdentity)>> {
    let Some((mut mtime, mut size, identity, file_id)) = single_file_stat_identified(root, p)?
    else {
        return Ok(None);
    };
    let mut identities = vec![identity];
    if sqlite_database(p) {
        let wal = crate::ingest::sqlite_sidecar(p, "-wal");
        if let Some((wal_mtime, wal_size, wal_identity)) = single_file_stat(root, &wal, true)? {
            mtime = mtime.max(wal_mtime);
            size = size.wrapping_add(wal_size.rotate_left(7));
            identities.push(wal_identity);
        }
    }
    Ok(Some((
        windows_source_key(p, file_id),
        mtime,
        size,
        CacheIdentity::Source(identities),
    )))
}

#[cfg(not(windows))]
fn identified_file_stat(
    root: &Path,
    p: &Path,
) -> std::io::Result<Option<(String, i64, u64, CacheIdentity)>> {
    Ok(file_stat(root, p)?.map(|(mtime, size, identity)| (source_key(p), mtime, size, identity)))
}

fn preflight_file_stat(
    path: &Path,
    stamp: &crate::ingest::registry::SourceStatStamp,
) -> Option<(String, i64, u64, CacheIdentity)> {
    if sqlite_database(path) || stamp.content_hashed {
        return None;
    }
    let identity = CacheIdentity::Source(vec![stamp.change_token.clone()]);
    #[cfg(windows)]
    let key = windows_source_key(path, stamp.file_identity?);
    #[cfg(not(windows))]
    let key = source_key(path);
    Some((key, stamp.mtime_ns, stamp.len, identity))
}

#[cfg(windows)]
fn reconcile_hardlink_identities(
    root: &Path,
    stats: &mut [(PathBuf, String, i64, u64, CacheIdentity)],
    cached_identities: &HashMap<String, Vec<String>>,
    cache: &mut IngestCache,
) {
    let mut groups: HashMap<String, Vec<usize>> = HashMap::new();
    for (index, (path, key, ..)) in stats.iter().enumerate() {
        if !sqlite_database(path) {
            if let Some(prefix) = file_identity_prefix(key) {
                groups.entry(prefix.to_string()).or_default().push(index);
            }
        }
    }
    for (prefix, indices) in groups {
        let cached_keys = cached_identities
            .get(&prefix)
            .map(Vec::as_slice)
            .unwrap_or_default();
        let tracked = indices.len() > 1
            || cached_keys.len() > 1
            || indices.iter().any(|index| {
                cache
                    .entries
                    .get(&stats[*index].1)
                    .and_then(|entry| entry.identity.as_ref())
                    .is_some_and(|identity| {
                        matches!(identity, CacheIdentity::HardlinkedSource { .. })
                    })
            })
            || cached_keys.iter().any(|key| {
                cache
                    .entries
                    .get(key)
                    .and_then(|entry| entry.identity.as_ref())
                    .is_some_and(|identity| {
                        matches!(identity, CacheIdentity::HardlinkedSource { .. })
                    })
            });
        if !tracked {
            continue;
        }

        let mut known_states = Vec::new();
        for key in indices
            .iter()
            .map(|index| &stats[*index].1)
            .chain(cached_keys.iter())
        {
            if let Some(CacheIdentity::HardlinkedSource {
                change,
                content_sha256,
            }) = cache
                .entries
                .get(key)
                .and_then(|entry| entry.identity.as_ref())
            {
                known_states.push((*content_sha256, change.clone()));
            }
        }
        let mut known_digests: Vec<_> = known_states.iter().map(|(digest, _)| *digest).collect();
        known_digests.sort_unstable();
        known_digests.dedup();

        let reusable = known_digests
            .first()
            .copied()
            .filter(|_| known_digests.len() == 1);
        let reusable = reusable.filter(|digest| {
            let known_changes: HashSet<_> = known_states
                .iter()
                .filter(|(known_digest, _)| known_digest == digest)
                .map(|(_, change)| change.clone())
                .collect();
            indices.iter().all(|index| match &stats[*index].4 {
                CacheIdentity::HardlinkedSource { content_sha256, .. } => content_sha256 == digest,
                CacheIdentity::Source(change) => known_changes.contains(change),
                CacheIdentity::Token(_) | CacheIdentity::Released(_) => false,
            })
        });
        let digest = reusable.or_else(|| {
            let path = &stats[*indices.first()?].0;
            let metadata = source_metadata(root, path).ok().flatten()?;
            let (digest, identity) =
                crate::ingest::registry::content_sha256_with_file_identity(path, &metadata).ok()?;
            let key = windows_source_key(path, identity);
            (file_identity_prefix(&key) == Some(prefix.as_str())).then_some(digest)
        });
        let Some(digest) = digest else {
            continue;
        };

        for index in &indices {
            let identity = &mut stats[*index].4;
            match identity {
                CacheIdentity::Source(change) => {
                    let change = std::mem::take(change);
                    *identity = CacheIdentity::HardlinkedSource {
                        change,
                        content_sha256: digest,
                    };
                }
                CacheIdentity::HardlinkedSource { content_sha256, .. } => {
                    *content_sha256 = digest;
                }
                CacheIdentity::Token(_) | CacheIdentity::Released(_) => {}
            }
        }
        for index in &indices {
            let (_, key, mtime, size, observed) = &stats[*index];
            let refresh = cache.entries.get(key).is_some_and(|entry| {
                entry.mtime == *mtime
                    && entry.size == *size
                    && entry
                        .identity
                        .as_ref()
                        .is_some_and(|cached| refreshable_hardlink_identity(cached, observed))
                    && entry.identity.as_ref() != Some(observed)
            });
            if refresh {
                let observed = observed.clone();
                cache.update_entry(key, |entry| entry.identity = Some(observed));
            }
        }
    }
}

/// Result of an incremental adapter pass.
pub struct Pass {
    /// All current messages for this adapter (cached for unchanged files, fresh for the rest).
    pub messages: Vec<Message>,
    /// Events ONLY for sessions touched this run (changed files + their siblings). Unchanged
    /// sessions keep their existing event files.
    pub events: Vec<Event>,
    /// Number of source files actually parsed (for logging).
    pub parsed: usize,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct SourceReadIssue {
    pub agent: &'static str,
    pub path: PathBuf,
    pub kind: &'static str,
    pub reason: String,
    /// The conversation of a token store whose parse this issue records; None for a failure the
    /// collector cannot tie to one, which a reader must then attribute to no chat.
    pub session: Option<String>,
    /// A failure a retry may clear (a lock, a journal, an I/O error), though it may persist.
    pub retryable: bool,
    /// The source changed under the read: it never fails the same way twice.
    pub moving: bool,
}

impl SourceReadIssue {
    pub(crate) fn new(
        agent: &'static str,
        path: impl Into<PathBuf>,
        kind: &'static str,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            agent,
            path: path.into(),
            kind,
            reason: reason.into(),
            session: None,
            retryable: false,
            moving: false,
        }
    }

    pub(crate) fn for_session(mut self, session: impl Into<String>) -> Self {
        self.session = Some(session.into());
        self
    }

    pub(crate) fn retryable(mut self) -> Self {
        self.retryable = true;
        self
    }

    pub(crate) fn moving(mut self) -> Self {
        self.moving = true;
        self
    }

    /// Whether this records material the read could not take for a durable reason (not an
    /// absence, a failure a retry may clear, a source that moved under the read, nor a parser
    /// panic, which a fixed build reads): what a rebuild may publish without once a later pass
    /// sees it fail the same way.
    pub fn unreadable_material(&self) -> bool {
        self.kind == "source-read-failed"
            && !self.retryable
            && !self.moving
            && !self
                .reason
                .starts_with(crate::ingest::parse_guard::PANIC_REASON_PREFIX)
    }
}

/// Whether an unreadable scope may be published past. Deliberately not a bool: only
/// [`Self::Retained`] authorizes publication, so a caller cannot spend `Unknown` as proof.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MaterialVerdict {
    /// The published generation held material here that this pass cannot serve.
    Drops,
    /// Nothing was published under this scope, or retained rows still carry it.
    Retained,
    /// No inventory covers this scope; neither loss nor safety is proven.
    Unknown,
}

/// Parser result with an explicit source-read health bit. A successfully read source may
/// legitimately yield zero rows/events; an I/O/open failure may not be inferred from emptiness.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReadOutcome {
    Complete,
    Invalid,
    Skipped,
    /// Trustworthy rows, but some scope inside the source went unobserved. Too incomplete to
    /// license deletion, so last-good wins; with no last-good there is nothing to lose and
    /// what parsed is published.
    Partial,
    /// A complete read that came back empty because the source itself undid what it held: rolled
    /// back or rewound in records the read saw whole. Where an empty read of a changed file may be
    /// torn, this one proves the emptiness, so a file that only grew may publish it over last-good.
    Undone,
    /// The source could not be opened or read at this moment (a writer's lock or journal, an I/O
    /// error), which a retry may clear or which may persist. It vouches for nothing.
    Unavailable,
    /// The source changed under the read (its generation moved, it shrank or was replaced): a
    /// later pass reads another source, so this failure never counts as persistent.
    Moved,
}

impl ReadOutcome {
    pub fn merge(self, other: Self) -> Self {
        match (self, other) {
            (Self::Moved, _) | (_, Self::Moved) => Self::Moved,
            (Self::Unavailable, _) | (_, Self::Unavailable) => Self::Unavailable,
            (Self::Invalid, _) | (_, Self::Invalid) => Self::Invalid,
            (Self::Skipped, _) | (_, Self::Skipped) => Self::Skipped,
            (Self::Partial, _) | (_, Self::Partial) => Self::Partial,
            _ => Self::Complete,
        }
    }

    /// Whether this read may replace a cached entry wholesale (and so publish deletions).
    fn licenses_replacement(self) -> bool {
        matches!(self, Self::Complete | Self::Undone)
    }

    /// An `Undone` read is otherwise the complete read it also is.
    fn settled(self) -> Self {
        if self == Self::Undone {
            Self::Complete
        } else {
            self
        }
    }
}

fn read_issue_reason(outcome: Option<ReadOutcome>, panicked: Option<String>) -> String {
    if let Some(reason) = panicked {
        reason
    } else if outcome == Some(ReadOutcome::Partial) {
        "source parser could not cover part of the read".to_owned()
    } else if outcome == Some(ReadOutcome::Unavailable) {
        "source could not be opened or read; a retry may read it".to_owned()
    } else if outcome == Some(ReadOutcome::Moved) {
        "source changed while it was being read".to_owned()
    } else {
        "source parser could not complete the read".to_owned()
    }
}

/// The record of a read that failed, typed by whether a retry may clear it.
fn failed_read_issue(
    agent: &'static str,
    path: &Path,
    outcome: Option<ReadOutcome>,
    panicked: Option<String>,
) -> SourceReadIssue {
    // A source whose read failed for a named cause (a store's journal, an empty file) says so.
    let opened = crate::ingest::take_read_failure(path).filter(|_| panicked.is_none());
    let issue = SourceReadIssue::new(
        agent,
        path,
        "source-read-failed",
        opened.unwrap_or_else(|| read_issue_reason(outcome, panicked)),
    );
    match outcome {
        Some(ReadOutcome::Unavailable) => issue.retryable(),
        Some(ReadOutcome::Moved) => issue.moving(),
        _ => issue,
    }
}

pub trait IntoParsed {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>);
}

impl IntoParsed for (Vec<Message>, Vec<Event>) {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>) {
        (self.0, self.1, None)
    }
}

impl IntoParsed for (Vec<Message>, Vec<Event>, bool) {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>) {
        let outcome = if self.2 {
            ReadOutcome::Complete
        } else {
            ReadOutcome::Invalid
        };
        (self.0, self.1, Some(outcome))
    }
}

impl IntoParsed for (Vec<Message>, Vec<Event>, ReadOutcome) {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>) {
        (self.0, self.1, Some(self.2))
    }
}

type IsolatedParse = (
    Vec<Message>,
    Vec<Event>,
    Option<ReadOutcome>,
    Option<String>,
);

/// A parser that panics reads as an unreadable (skipped) source, with the panic's disclosable
/// reason carried for its issue; see [`crate::ingest::parse_guard`].
fn parse_isolated<R: IntoParsed>(
    agent: &'static str,
    path: &Path,
    parse: impl FnOnce() -> R,
) -> IsolatedParse {
    match crate::ingest::parse_guard::isolate(agent, path, || parse().into_parsed()) {
        Ok((messages, events, outcome)) => (messages, events, outcome, None),
        Err(panic) => (
            Vec::new(),
            Vec::new(),
            Some(ReadOutcome::Skipped),
            Some(panic.reason),
        ),
    }
}

type IdentifiedStat = std::io::Result<Option<(String, i64, u64, CacheIdentity)>>;
type ObservedFile = (PathBuf, Option<String>, IdentifiedStat);
type ParsedMiss = (
    String,
    PathBuf,
    i64,
    u64,
    CacheIdentity,
    Vec<Message>,
    Vec<Event>,
    Option<ReadOutcome>,
    Option<String>,
);
type ParsedSibling = (
    String,
    PathBuf,
    Vec<Message>,
    Vec<Event>,
    Option<ReadOutcome>,
    Option<String>,
);

/// Parse `files`, pulling unchanged ones from `cache`. Updates the cache in place.
///
/// `root` scopes the stale-entry cleanup: the cache is SHARED across adapters (claude,
/// then codex, against the same store), so this call may only forget entries under its
/// own root - wiping everything not in `files` would erase the other adapters' entries
/// and silently disable the cache for them (run N parses claude, run N+1 codex, forever).
pub fn collect_cached<F, R>(
    cache: &mut IngestCache,
    root: &Path,
    files: &[PathBuf],
    parse: F,
) -> Pass
where
    F: Fn(&Path) -> R + Sync,
    R: IntoParsed,
{
    collect_cached_for(cache, "ingest", root, files, parse)
}

pub fn collect_cached_for<F, R>(
    cache: &mut IngestCache,
    agent: &'static str,
    root: &Path,
    files: &[PathBuf],
    parse: F,
) -> Pass
where
    F: Fn(&Path) -> R + Sync,
    R: IntoParsed,
{
    collect_cached_stamped_for(cache, agent, root, files, |path, _, _| parse(path))
}

/// Variant for parsers that reuse the freshness stamp instead of restatting each miss.
pub fn collect_cached_stamped<F, R>(
    cache: &mut IngestCache,
    root: &Path,
    files: &[PathBuf],
    parse: F,
) -> Pass
where
    F: Fn(&Path, i64, u64) -> R + Sync,
    R: IntoParsed,
{
    collect_cached_stamped_for(cache, "ingest", root, files, parse)
}

pub fn collect_cached_stamped_for<F, R>(
    cache: &mut IngestCache,
    agent: &'static str,
    root: &Path,
    files: &[PathBuf],
    parse: F,
) -> Pass
where
    F: Fn(&Path, i64, u64) -> R + Sync,
    R: IntoParsed,
{
    collect_cached_stamped_retiring_for(cache, agent, root, files, &HashSet::new(), parse)
}

/// [`collect_cached_for`] for a store where a file that still exists can stop being a source
/// because a newer sibling supersedes it: `retired` paths are forgotten exactly like deletions
/// instead of being re-added from the cache or the coverage snapshot, and so is their tally.
pub fn collect_cached_retiring_for<F, R>(
    cache: &mut IngestCache,
    agent: &'static str,
    root: &Path,
    files: &[PathBuf],
    retired: &HashSet<PathBuf>,
    parse: F,
) -> Pass
where
    F: Fn(&Path) -> R + Sync,
    R: IntoParsed,
{
    collect_cached_stamped_retiring_for(cache, agent, root, files, retired, |path, _, _| {
        parse(path)
    })
}

fn collect_cached_stamped_retiring_for<F, R>(
    cache: &mut IngestCache,
    agent: &'static str,
    root: &Path,
    files: &[PathBuf],
    retired: &HashSet<PathBuf>,
    parse: F,
) -> Pass
where
    F: Fn(&Path, i64, u64) -> R + Sync,
    R: IntoParsed,
{
    for path in retired {
        crate::intake::forget(path);
    }
    // Windows walkers transiently omit entries (`read_dir` errors under live writers/AV), so
    // re-add cached paths: only metadata NotFound confirms deletion; other stat errors guard.
    let mut cached_paths: HashMap<String, PathBuf> = cache
        .entries
        .keys()
        .filter_map(|key| source_path_from_key(key).map(|path| (key.clone(), path)))
        .collect();
    let mut reconciled_files: Vec<PathBuf> = Vec::with_capacity(files.len());
    let mut listed_paths = HashSet::with_capacity(files.len());
    for path in files.iter() {
        if listed_paths.insert(path.clone()) {
            reconciled_files.push(path.clone());
        }
    }
    for path in &cache.coverage_expected_paths {
        if source_path_within(path, root) && !listed_paths.contains(path) && !retired.contains(path)
        {
            reconciled_files.push(path.clone());
            listed_paths.insert(path.clone());
        }
    }
    for path in cached_paths.values() {
        if !source_path_within(path, root) || listed_paths.contains(path) || retired.contains(path)
        {
            continue;
        }
        match fs::symlink_metadata(path) {
            Ok(_) => {
                reconciled_files.push(path.clone());
                listed_paths.insert(path.clone());
            }
            // A stat proving absence (ENOENT, a real file where a directory was) is a deletion
            // candidate under the same two-observation protocol as the preflight walk.
            Err(error)
                if crate::ingest::registry::stat_proves_absent(path, &error)
                    && !absence_unproven(root, path) => {}
            Err(_) => {
                reconciled_files.push(path.clone());
                listed_paths.insert(path.clone());
            }
        }
    }
    // One absent walk can be an unmount; only a repeated pending preflight admits deletion.
    let expected_missing_root = cache
        .repair_expected_paths
        .iter()
        .any(|path| source_path_within(path, root));
    let cached_material_root = cache.entries.iter().any(|(key, entry)| {
        cached_paths
            .get(key)
            .is_some_and(|path| source_path_within(path, root))
            && (!entry.msgs.is_empty() || !entry.event_keys.is_empty() || entry.legacy_had_events)
    });
    if reconciled_files.is_empty()
        && (cached_material_root || expected_missing_root)
        && (!root.exists()
            || (!cache.stable_deletions_provable() && (cache.warm || expected_missing_root)))
        && !cache.repeated_absence_covers(agent, root)
    {
        cache.mark_guarded_stale();
        cache.record_source_read_issue(
            agent,
            root,
            "source-root-unavailable",
            "source root disappeared or could not be enumerated",
        );
        if !cached_material_root {
            cache.output_incomplete = true;
        }
        let messages: Vec<Message> = cache
            .entries
            .iter()
            .filter(|(key, _)| {
                cached_paths
                    .get(*key)
                    .is_some_and(|path| source_path_within(path, root))
            })
            .flat_map(|(_, entry)| entry.msgs.iter().map(CMsg::to_msg))
            .collect();
        crate::emit::rows_only(&messages);
        return Pass {
            messages,
            events: Vec::new(),
            parsed: 0,
        };
    }
    let file_count = reconciled_files.len();
    let t_stat = std::time::Instant::now();
    let exact_cached: HashMap<&Path, (&str, &Entry)> = cache
        .entries
        .iter()
        .filter_map(|(key, entry)| {
            cached_paths
                .get(key)
                .map(|path| (path.as_path(), (key.as_str(), entry)))
        })
        .collect();
    let mut cached_identities: HashMap<String, Vec<String>> = HashMap::new();
    #[cfg(windows)]
    let mut cached_aliases: HashMap<String, LogicalPathIndex<String>> = HashMap::new();
    #[cfg(not(windows))]
    for key in cached_paths.keys() {
        if let Some(prefix) = file_identity_prefix(key) {
            cached_identities
                .entry(prefix.to_string())
                .or_default()
                .push(key.clone());
        }
    }
    #[cfg(windows)]
    for (key, path) in &cached_paths {
        if let Some(prefix) = file_identity_prefix(key) {
            cached_identities
                .entry(prefix.to_string())
                .or_default()
                .push(key.clone());
            match cached_aliases.entry(prefix.to_string()) {
                std::collections::hash_map::Entry::Occupied(mut entry) => {
                    entry.get_mut().observe(path.clone(), key.clone());
                }
                std::collections::hash_map::Entry::Vacant(entry) => {
                    entry.insert(LogicalPathIndex::new(path.clone(), key.clone()));
                }
            }
        }
    }
    let observed: Vec<ObservedFile> = reconciled_files
        .into_par_iter()
        .map(|p| {
            let exact = exact_cached.get(p.as_path()).copied();
            match exact {
                Some((key, _entry)) => {
                    let stat = cache
                        .source_stamps
                        .get(&p)
                        .and_then(|stamp| preflight_file_stat(&p, stamp))
                        .map(Some)
                        .map(Ok)
                        .unwrap_or_else(|| identified_file_stat(root, &p));
                    (p, Some(key.to_string()), stat)
                }
                None => {
                    let stat = cache
                        .source_stamps
                        .get(&p)
                        .and_then(|stamp| preflight_file_stat(&p, stamp))
                        .map(Some)
                        .map(Ok)
                        .unwrap_or_else(|| identified_file_stat(root, &p));
                    let fallback = if matches!(&stat, Ok(Some(_))) {
                        None
                    } else {
                        exact_cached.iter().find_map(|(old, cached)| {
                            source_path_eq(old, &p).then(|| cached.0.to_string())
                        })
                    };
                    (p, fallback, stat)
                }
            }
        })
        .collect();
    drop(exact_cached);
    let mut stats: Vec<(PathBuf, String, i64, u64, CacheIdentity)> =
        Vec::with_capacity(observed.len());
    let mut unstatable: Vec<(PathBuf, Option<String>)> = Vec::new();
    let mut discovered = HashSet::new();
    #[cfg(windows)]
    let mut observed_identities: HashMap<String, LogicalPathIndex<usize>> = HashMap::new();
    for (path, cached_key, stat) in observed {
        match stat {
            Ok(Some((key, mtime, size, identity))) => {
                #[cfg(windows)]
                if let Some(prefix) = file_identity_prefix(&key) {
                    let next = stats.len();
                    let duplicate = match observed_identities.entry(prefix.to_string()) {
                        std::collections::hash_map::Entry::Occupied(mut entry) => {
                            entry.get_mut().observe(path.clone(), next)
                        }
                        std::collections::hash_map::Entry::Vacant(entry) => {
                            entry.insert(LogicalPathIndex::new(path.clone(), next));
                            None
                        }
                    };
                    if let Some(index) = duplicate {
                        let prior = stats[index].1.clone();
                        discovered.insert(prior);
                        continue;
                    }
                }
                if let Some(prior) = cached_key.as_ref().filter(|prior| *prior != &key) {
                    if !cache.entries.contains_key(&key) && cache.rename_entry(prior, key.clone()) {
                        cached_paths.remove(prior);
                    }
                }
                #[cfg(windows)]
                if !cache.entries.contains_key(&key) {
                    let alias = file_identity_prefix(&key)
                        .and_then(|prefix| cached_aliases.get(prefix))
                        .and_then(|candidates| candidates.get(&path))
                        .filter(|prior| cache.entries.contains_key(*prior))
                        .cloned();
                    if let Some(prior) = alias {
                        cache.rename_entry(&prior, key.clone());
                        cached_paths.remove(&prior);
                    }
                }
                cached_paths.insert(key.clone(), path.clone());
                if discovered.insert(key.clone()) {
                    stats.push((path, key, mtime, size, identity));
                }
            }
            Ok(None) => {
                crate::ingest::warn_source_skip(agent, &path, "source vanished before stat");
                cache.record_source_read_issue(
                    agent,
                    &path,
                    "source-vanished",
                    "source vanished before stat",
                );
                if let Some(key) = &cached_key {
                    discovered.insert(key.clone());
                }
                unstatable.push((path, cached_key));
            }
            Err(error) => {
                crate::ingest::warn_source_skip(agent, &path, &error);
                // A denial (on the file or a SQLite sidecar the stamp reads) persists until the
                // user grants access: unreadable material, not a stat a retry may clear.
                let denied = error.kind() == std::io::ErrorKind::PermissionDenied;
                cache.record_source_read_issue(
                    agent,
                    &path,
                    if denied { "source-read-failed" } else { "source-stat-failed" },
                    error.to_string(),
                );
                if denied && !cached_key.as_ref().is_some_and(|key| cache.entries.contains_key(key))
                {
                    cache.unreadable_unserved = true;
                }
                if let Some(key) = &cached_key {
                    discovered.insert(key.clone());
                }
                unstatable.push((path, cached_key));
            }
        }
    }
    #[cfg(windows)]
    reconcile_hardlink_identities(root, &mut stats, &cached_identities, cache);
    let stat_ms = t_stat.elapsed().as_millis();
    let t_parse = std::time::Instant::now();
    // Discovery and stat are separate observations. A path returned by discovery whose
    // metadata then fails (common with a live Windows writer/share lock) is not a confirmed
    // deletion: retain and serve its last-good entry, and forbid source-snapshot publication.
    if !unstatable.is_empty() {
        cache.mark_guarded_stale();
    }

    // Partition by moving the stat rows: the common delta is nearly-all hits and one miss,
    // so never clone hit paths/keys.
    let (hits, mut misses): (Vec<_>, Vec<_>) =
        stats
            .into_iter()
            .partition(|(_, key, mtime, size, identity)| {
                matches!(cache.entries.get(key), Some(e)
                if !cache.force_reparse
                    && !e.legacy_needs_reparse
                    && e.mtime == *mtime
                    && e.size == *size
                    && e.identity.as_ref() == Some(identity))
            });
    crate::emit::total(misses.len());
    if crate::emit::on() {
        // Emit-only recency order (newest first); the normal run keeps discovery order
        // (and byte-identical derived files).
        misses.sort_by_key(|(_, _, mtime, _, _)| std::cmp::Reverse(*mtime));
    }

    let miss_parsed: Vec<ParsedMiss> = misses
        .par_iter()
        .map(|(p, key, mtime, size, identity)| {
            let (m, e, healthy, panicked) = parse_isolated(agent, p, || parse(p, *mtime, *size));
            crate::emit::file_done(&m);
            (
                key.clone(),
                p.clone(),
                *mtime,
                *size,
                identity.clone(),
                m,
                e,
                healthy,
                panicked,
            )
        })
        .collect();

    // sessions touched: from the fresh parses AND from the OLD cache entries of changed files
    // (so a session that moved between files is fully refreshed)
    let mut affected: HashSet<std::sync::Arc<str>> = HashSet::new();
    for (_, _, _, _, _, m, events, _, _) in &miss_parsed {
        for msg in m {
            affected.insert(msg.session.clone());
        }
        for event in events {
            affected.insert(std::sync::Arc::from(event.session.as_str()));
        }
    }
    for (_, key, _, _, _) in &misses {
        if let Some(e) = cache.entries.get(key) {
            for cm in &e.msgs {
                affected.insert(cm.session.clone());
            }
            for event in &e.event_keys {
                affected.insert(std::sync::Arc::from(event.session.as_str()));
            }
        }
    }
    // A deleted source has no miss/fresh parse, but every session previously owned by it must
    // be removed from the materialized JSONL files (and any unchanged sibling must contribute
    // its complete event stream). Capture those sessions before the stale cache entry is swept.
    for (key, entry) in &cache.entries {
        if cached_paths
            .get(key)
            .is_some_and(|path| source_path_within(path, root))
            && !discovered.contains(key)
        {
            for cm in &entry.msgs {
                affected.insert(cm.session.clone());
            }
            for event in &entry.event_keys {
                affected.insert(std::sync::Arc::from(event.session.as_str()));
            }
        }
    }
    // record the changed sessions for the corpus delta (accumulates across adapters)
    cache.touched.extend(affected.iter().map(|a| a.to_string()));

    // unchanged sibling files that share an affected session: re-parse them so the session's
    // event file is rebuilt from ALL its sources (no-op for one-file-per-session adapters)
    let siblings: Vec<(PathBuf, String, i64, u64)> = hits
        .iter()
        .filter(|(_, key, _, _, _)| {
            cache
                .entries
                .get(key)
                .map(|e| {
                    e.msgs.iter().any(|cm| affected.contains(&cm.session))
                        || e.event_keys
                            .iter()
                            .any(|event| affected.contains(event.session.as_str()))
                })
                .unwrap_or(false)
        })
        .map(|(path, key, mtime, size, _)| (path.clone(), key.clone(), *mtime, *size))
        .collect();
    let sib_keys: HashSet<String> = siblings.iter().map(|(_, key, _, _)| key.clone()).collect();
    crate::emit::total(siblings.len());
    let sib_parsed: Vec<ParsedSibling> = siblings
        .par_iter()
        .map(|(p, key, mtime, size)| {
            let (m, e, healthy, panicked) = parse_isolated(agent, p, || parse(p, *mtime, *size));
            crate::emit::file_done(&m);
            (key.clone(), p.clone(), m, e, healthy, panicked)
        })
        .collect();

    let mut old_event_keys = Vec::new();
    for (_, key, _, _, _) in &misses {
        if let Some(entry) = cache.entries.get(key) {
            old_event_keys.extend(entry.event_keys.iter().cloned());
        }
    }
    for (_, key, _, _) in &siblings {
        if let Some(entry) = cache.entries.get(key) {
            old_event_keys.extend(entry.event_keys.iter().cloned());
        }
    }
    for (key, entry) in &cache.entries {
        if cached_paths
            .get(key)
            .is_some_and(|path| source_path_within(path, root))
            && !discovered.contains(key)
        {
            old_event_keys.extend(entry.event_keys.iter().cloned());
        }
    }
    cache.touch_event_keys(&old_event_keys);

    let mut messages: Vec<Message> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut blocked_event_sessions: HashSet<std::sync::Arc<str>> = HashSet::new();

    // A discovered-but-unstatable source stays last-known-good. If another changed source
    // shares one of its sessions, do not overwrite that session's event file with an incomplete
    // subset; retaining the old event generation is safer until the guarded retry succeeds.
    for (_, key) in &unstatable {
        if let Some(entry) = key.as_ref().and_then(|key| cache.entries.get(key)) {
            let start = messages.len();
            messages.extend(entry.msgs.iter().map(CMsg::to_msg));
            crate::emit::rows_only(&messages[start..]);
            for cm in &entry.msgs {
                if affected.contains(&cm.session) {
                    blocked_event_sessions.insert(cm.session.clone());
                }
            }
            for event in &entry.event_keys {
                if affected.contains(event.session.as_str()) {
                    blocked_event_sessions.insert(std::sync::Arc::from(event.session.as_str()));
                }
            }
        }
    }

    // Cached-row materialization is the remaining CPU work on a one-file delta. Clone its Arc
    // fields in ordered parallel chunks, then append the chunks in discovery order. `par_chunks`
    // is indexed, so appending chunks in order preserves discovery order byte-for-byte.
    let cached_chunks: Vec<Vec<Message>> = hits
        .par_chunks(256)
        .map(|batch| {
            let mut chunk = Vec::new();
            for (_, key, _, _, _) in batch {
                if sib_keys.contains(key) {
                    continue;
                }
                if let Some(entry) = cache.entries.get(key) {
                    chunk.reserve(entry.msgs.len());
                    chunk.extend(entry.msgs.iter().map(CMsg::to_msg));
                }
            }
            chunk
        })
        .collect();
    messages.reserve(cached_chunks.iter().map(Vec::len).sum());
    for mut chunk in cached_chunks {
        crate::emit::rows_only(&chunk);
        messages.append(&mut chunk);
    }
    // A racy entry keeps no identity, so its unchanged stat key still misses and reparses once.
    let now_ns = epoch_ns(std::time::SystemTime::now()).unwrap_or(i64::MAX);
    let racy_start = cache.racy_scan_start_ns;
    let racy = |mtime: i64| racy_start.is_some_and(|start| racy_stamp(mtime, start, now_ns));
    // fresh: changed files (messages + events) + sibling files (messages cached-equal + events)
    for (key, path, mt, sz, identity, m, e, healthy, panicked) in miss_parsed {
        // only a proven-empty read (see `ReadOutcome::Undone`) differs from a complete one
        let undone = healthy == Some(ReadOutcome::Undone);
        let healthy = healthy.map(ReadOutcome::settled);
        // A failed, skipped or partial reparse retains the prior entry instead of publishing
        // deletion; a partial one that lost nothing the entry held refreshes it instead.
        if healthy.is_some_and(|outcome| !outcome.licenses_replacement()) {
            cache.mark_guarded_stale();
            cache.record_read_issue(failed_read_issue(agent, &path, healthy, panicked));
            if let Some(prev) = cache.entries.get(&key) {
                if healthy == Some(ReadOutcome::Partial) && partial_read_extends(prev, &m, &e) {
                    // Events already stored for its sessions stay as written: this read
                    // may have missed some of them.
                    blocked_event_sessions
                        .extend(prev.msgs.iter().map(|message| message.session.clone()));
                    blocked_event_sessions.extend(
                        prev.event_keys
                            .iter()
                            .map(|event| std::sync::Arc::from(event.session.as_str())),
                    );
                    let mut event_keys = prev.event_keys.clone();
                    event_keys.extend(cached_event_keys(&e));
                    event_keys.sort_unstable();
                    event_keys.dedup();
                    let legacy_needs_reparse = prev.legacy_needs_reparse;
                    cache.put_entry(
                        key,
                        Entry {
                            mtime: mt,
                            size: sz,
                            identity: None,
                            msgs: m.iter().map(CMsg::from).collect(),
                            event_keys,
                            legacy_had_events: false,
                            legacy_needs_reparse,
                        },
                    );
                    messages.extend(m);
                    events.extend(e);
                    continue;
                }
                blocked_event_sessions
                    .extend(prev.msgs.iter().map(|message| message.session.clone()));
                blocked_event_sessions.extend(
                    prev.event_keys
                        .iter()
                        .map(|event| std::sync::Arc::from(event.session.as_str())),
                );
                let start = messages.len();
                messages.extend(prev.msgs.iter().map(CMsg::to_msg));
                crate::emit::rows_only(&messages[start..]);
                // The tally on record is now this failed read's: a source returning to the
                // entry's exact stamp (a store renamed back) must be read again, not cache-hit.
                cache.forget_identity(&key);
                continue;
            }
            cache.hold_unserved_read(healthy);
            if healthy != Some(ReadOutcome::Partial) {
                continue;
            }
        }
        if m.is_empty() && e.is_empty() {
            let keep_prior = cache.entries.get(&key).is_some_and(|prev| {
                prev.legacy_had_events
                    || ((!prev.msgs.is_empty() || !prev.event_keys.is_empty())
                        && !(undone && prev.size < sz)
                        && (healthy != Some(ReadOutcome::Complete)
                            || prev.mtime != mt
                            || prev.size != sz
                            || prev.identity.as_ref() != Some(&identity)))
            });
            if keep_prior {
                cache.mark_guarded_stale();
                cache.record_source_read_issue(
                    agent,
                    &path,
                    "source-empty-after-read",
                    "source produced no material after previously producing rows",
                );
                if let Some(prev) = cache.entries.get(&key) {
                    blocked_event_sessions.extend(prev.msgs.iter().map(|cm| cm.session.clone()));
                    blocked_event_sessions.extend(
                        prev.event_keys
                            .iter()
                            .map(|event| std::sync::Arc::from(event.session.as_str())),
                    );
                    let start = messages.len();
                    messages.extend(prev.msgs.iter().map(CMsg::to_msg));
                    crate::emit::rows_only(&messages[start..]);
                    continue;
                }
            } else if cache.repair_mode
                && healthy != Some(ReadOutcome::Complete)
                && cache
                    .repair_expected_paths
                    .iter()
                    .any(|expected| source_path_eq(expected, &path))
            {
                cache.mark_guarded_stale();
                cache.output_incomplete = true;
                cache.record_source_read_issue(
                    agent,
                    &path,
                    "source-read-failed",
                    "source parser could not complete the read",
                );
                continue;
            }
        }
        if healthy == Some(ReadOutcome::Partial) {
            // Cached so a later failed read serves these rows and the cache names this source as
            // published; no identity, so every pass retries it until a read completes.
            if !m.is_empty() || !e.is_empty() {
                cache.put_entry(
                    key,
                    Entry {
                        mtime: mt,
                        size: sz,
                        identity: None,
                        msgs: m.iter().map(CMsg::from).collect(),
                        event_keys: cached_event_keys(&e),
                        legacy_had_events: false,
                        legacy_needs_reparse: false,
                    },
                );
            }
            messages.extend(m);
            events.extend(e);
            continue;
        }
        let event_keys = cached_event_keys(&e);
        cache.put_entry(
            key,
            Entry {
                mtime: mt,
                size: sz,
                identity: (!racy(mt)).then_some(identity),
                msgs: m.iter().map(CMsg::from).collect(),
                event_keys,
                legacy_had_events: false,
                legacy_needs_reparse: false,
            },
        );
        messages.extend(m);
        events.extend(e);
    }
    for (key, path, m, e, healthy, panicked) in sib_parsed {
        let healthy = healthy.map(ReadOutcome::settled);
        // A sibling read can fail independently after its successful stat. Serving its cached
        // rows mirrors the changed-file last-good guard; its session event files remain untouched
        // because `events` cannot be reconstructed completely without this sibling.
        if healthy.is_some_and(|outcome| !outcome.licenses_replacement()) {
            cache.mark_guarded_stale();
            cache.record_read_issue(failed_read_issue(agent, &path, healthy, panicked));
            if let Some(prev) = cache.entries.get(&key) {
                blocked_event_sessions
                    .extend(prev.msgs.iter().map(|message| message.session.clone()));
                blocked_event_sessions.extend(
                    prev.event_keys
                        .iter()
                        .map(|event| std::sync::Arc::from(event.session.as_str())),
                );
                let start = messages.len();
                messages.extend(prev.msgs.iter().map(CMsg::to_msg));
                crate::emit::rows_only(&messages[start..]);
                continue;
            }
            cache.hold_unserved_read(healthy);
            if healthy != Some(ReadOutcome::Partial) {
                continue;
            }
        }
        if m.is_empty() && e.is_empty() {
            let keep_prior = cache.entries.get(&key).is_some_and(|prev| {
                prev.legacy_had_events
                    || ((!prev.msgs.is_empty() || !prev.event_keys.is_empty())
                        && healthy != Some(ReadOutcome::Complete))
            });
            if keep_prior {
                cache.mark_guarded_stale();
                cache.record_source_read_issue(
                    agent,
                    &path,
                    "source-empty-after-read",
                    "source produced no material after previously producing rows",
                );
                if let Some(prev) = cache.entries.get(&key) {
                    blocked_event_sessions.extend(prev.msgs.iter().map(|cm| cm.session.clone()));
                    blocked_event_sessions.extend(
                        prev.event_keys
                            .iter()
                            .map(|event| std::sync::Arc::from(event.session.as_str())),
                    );
                    let start = messages.len();
                    messages.extend(prev.msgs.iter().map(CMsg::to_msg));
                    crate::emit::rows_only(&messages[start..]);
                    continue;
                }
            }
        }
        let event_keys = cached_event_keys(&e);
        cache.update_entry(&key, |entry| {
            entry.msgs = m.iter().map(CMsg::from).collect();
            entry.event_keys = event_keys;
            entry.legacy_had_events = false;
            entry.legacy_needs_reparse = false;
        });
        messages.extend(m);
        events.extend(e);
    }

    if !blocked_event_sessions.is_empty() {
        events.retain(|event| !blocked_event_sessions.contains(event.session.as_str()));
    }

    // Forget only files confirmed absent from discovery. A discovered path with a transient
    // metadata failure was served above and deliberately remains in the cache for retry.
    let missing_material_source = cache.entries.iter().any(|(key, entry)| {
        cached_paths
            .get(key)
            .is_some_and(|path| source_path_within(path, root))
            && !discovered.contains(key)
            && (!entry.msgs.is_empty() || !entry.event_keys.is_empty() || entry.legacy_had_events)
    });
    let missing_expected_source = cache.repair_mode
        && cache.repair_expected_paths.iter().any(|path| {
            source_path_within(path, root)
                && !listed_paths.contains(path)
                && !listed_paths
                    .iter()
                    .any(|listed| source_path_eq(listed, path))
        });
    // Two agreeing clean-absence preflights - of the agent or of this root - are the repeated
    // witness a discarded base lacks; unrelated churn in other stores must not veto them.
    let repeated_whole_store_absence =
        file_count == 0 && cache.repeated_absence_covers(agent, root);
    if cache.repair_mode
        && (missing_material_source || missing_expected_source)
        && !cache.stable_deletions_provable()
        && !repeated_whole_store_absence
    {
        cache.mark_guarded_stale();
        if missing_expected_source && !cached_material_root {
            cache.output_incomplete = true;
        }
    } else {
        #[cfg(test)]
        if cache.repair_mode && (missing_material_source || missing_expected_source) {
            cache.provisional_deletions = true;
        }
        // Complete preflight omission plus this pass's NotFound is path-local deletion proof.
        cache.remove_entries_where(|key, _| {
            cached_paths
                .get(key)
                .is_some_and(|path| source_path_within(path, root))
                && !discovered.contains(key)
        });
    }

    crate::emit::human_line(format_args!(
        "  [{}] {} files: stat {}ms · parse+materialize {}ms ({} changed, {} siblings, {} stat failures)",
        root.file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default(),
        file_count,
        stat_ms,
        t_parse.elapsed().as_millis(),
        misses.len(),
        siblings.len(),
        unstatable.len()
    ));
    Pass {
        messages,
        events,
        parsed: misses.len() + siblings.len(),
    }
}
