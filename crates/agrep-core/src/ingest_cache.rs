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
//! A schema bump (`CACHE_VERSION`), a missing/corrupt cache, or `--full` fall back to a clean
//! full parse.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use rayon::prelude::*;
use serde::{Deserialize, Serialize};

use crate::model::{Event, Message};

/// Bump when the cached struct layout OR the parse semantics change; old caches are then
/// ignored (full reparse). 4->5: every adapter's timestamp parsing moved to
/// ingest::parse_timestamp, so every store is re-read once through the consolidated path
/// rather than served from an entry parsed by the old scattered sites. 5->6: intern_agent
/// resolves against the registry (cursor/crush cached entries round-tripped as "unknown");
/// the reparse re-materializes every session so the corpus delta carries the relabel. 6->7:
/// Codex forked rollouts gain child-owned boundaries instead of collapsing into the parent.
/// 7->8: entry shape settled as (mtime, size, msgs) - the one legacy wire generation with a
/// migration path; v7 and earlier, plus abandoned v9/v10 caches, cold-reparse instead.
/// 8->9: exact per-source event ownership and nanosecond stat keys; v8 is migrated through an
/// explicit wire type and force-reparsed once before any exact incremental prune is allowed.
/// 9->10: Codex guardian approval-review rollouts become counted internal sidechain skips.
/// 10->11: Stat keys include filesystem change/replacement identity, detecting preserved-mtime
/// edits on Unix and same-name replacements on Windows without reading every source file.
/// 11->12: SQLite Stat keys include committed WAL metadata instead of blessing main-file hits.
/// 12->13: reply source lengths disclose ingest-cap loss in detail and recall views.
/// 13->14: Codex encrypted send_message payloads become non-searchable control events.
/// 14->15: event ownership migrates to collision-safe hashed filenames after one forced reparse.
/// 15->16: cache identities retain exact source/token keys; Windows stat keys use file USNs.
/// 16->17: Windows source keys use stable file IDs and retain exact UTF-16 paths.
/// 17->18: Claude compact-summary provenance replaces recap text-prefix inference.
/// 18->19: token intake identities migrate from ambiguous separators to canonical JSON tuples.
/// 19->20: event outputs retain original UTF-8 byte counts before preview truncation.
/// 20->21: codex human turns require the rollout's own submission log, dropping the
/// harness-composed role:user input (AGENTS.md, environment, catalogs) cached as lived.
/// 21->22: nested oh-my-pi advisor and worker sessions link to their root chat.
pub const CACHE_VERSION: u32 = 22;

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

#[derive(Serialize, Deserialize, Clone)]
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
#[derive(Serialize)]
struct CacheFileRef<'a> {
    version: u32,
    entries: &'a HashMap<String, Entry>,
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

/// Successor takeover: re-own a dead foreign writer's parse cache instead of
/// discarding it. The base decodes through the full validation path (header,
/// length, payload digest, storage version) and only committed journal frames
/// replay, so the adopted entries are exactly what the dead writer last
/// published; a fresh base then lands atomically under the current identity
/// and the foreign journal is retired with it. Only a fully current
/// generation adopts - an older generation reparses anyway, so the caller's
/// discard loses nothing. Any refusal returns Err and the caller falls back
/// to the discard path unchanged.
pub fn adopt_foreign_cache(path: &Path) -> Result<usize, &'static str> {
    let (entries, generation, _backing) = decode_cache_owned(path, WriterBuildId::current(), true)
        .map_err(CacheDecodeRefusal::label)?;
    if generation != CacheGeneration::Current {
        return Err("cache generation predates this format; a reparse is due anyway");
    }
    let adopted = entries.len();
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
    msgs: Vec<CMsg>,
    event_keys: Vec<CEventKey>,
    legacy_had_events: bool,
    legacy_needs_reparse: bool,
}

#[derive(Deserialize, Serialize)]
struct LegacyCacheFileV15 {
    version: u32,
    entries: HashMap<String, LegacyEntryV15>,
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
            side: self.side,
            parent: self.parent.clone(),
        }
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
        }
    }

    /// The bytes were read and this build conclusively cannot decode them, so no retry changes
    /// the verdict. Such a base is a broken derived artifact agrep owns: recovery discards and
    /// reparses it. Missing/unreadable/foreign bases are not this - see the callers' refusals.
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

/// Decode current/reparse-compatible entries or migrate the exact v8 wire shape.
fn decode_cache_payload(
    bytes: &[u8],
    _path: &Path,
) -> Result<(HashMap<String, Entry>, CacheGeneration, bool), CacheDecodeRefusal> {
    let mut refusal = CacheDecodeRefusal::PayloadShape;
    if let Ok(cache) = bincode::deserialize::<CacheFile>(bytes) {
        if cache.version == CACHE_VERSION {
            #[cfg(windows)]
            if cache.entries.keys().any(|key| {
                key.starts_with("\0file\0") && source_path_from_key(key).is_none()
                    || !key.starts_with('\0')
            }) {
                return Err(CacheDecodeRefusal::PayloadShape);
            }
            let generation = if cache
                .entries
                .values()
                .any(|entry| entry.legacy_needs_reparse)
            {
                CacheGeneration::LegacyReparse
            } else {
                CacheGeneration::Current
            };
            return Ok((cache.entries, generation, true));
        }
        if cache.version.checked_add(1) == Some(CACHE_VERSION) {
            let mut entries = cache.entries;
            for entry in entries.values_mut() {
                entry.legacy_needs_reparse = true;
            }
            return Ok((entries, CacheGeneration::LegacyReparse, true));
        }
        if matches!(cache.version, 18..=19) {
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
                                msgs: entry.msgs,
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
                    msgs: entry.msgs,
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
    msgs: Vec<CMsg>,
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
    if semantic_version != CACHE_VERSION
        && semantic_version.checked_add(1) != Some(CACHE_VERSION)
        && !matches!(semantic_version, 18..=19)
    {
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

fn encode_journal_header(instance: [u8; 16]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(JOURNAL_HEADER_LEN);
    bytes.extend_from_slice(CACHE_JOURNAL_MAGIC);
    bytes.extend_from_slice(&CACHE_STORAGE_VERSION.to_le_bytes());
    bytes.extend_from_slice(&instance);
    bytes
}

fn encode_journal_frame(from: u64, to: u64, delta: &JournalDelta) -> anyhow::Result<Vec<u8>> {
    let payload = bincode::serialize(delta)?;
    anyhow::ensure!(
        payload.len() as u64 <= MAX_FRAME_BYTES,
        "ingest cache delta is too large"
    );
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
    Ok(bytes)
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

fn replay_journal(
    path: &Path,
    instance: [u8; 16],
    entries: &mut HashMap<String, Entry>,
) -> JournalCursor {
    let bytes = match read_regular_file(path) {
        Ok(Some(bytes)) => bytes,
        Ok(None) => Vec::new(),
        Err(_) => {
            let observed_len = fs::symlink_metadata(path)
                .map(|metadata| metadata.len())
                .unwrap_or(0);
            return JournalCursor {
                sequence: 0,
                observed_len,
                valid_len: 0,
                reset: true,
                frames: 0,
                upserts: HashSet::new(),
                deletes: HashSet::new(),
            };
        }
    };
    if bytes.is_empty() {
        return JournalCursor {
            sequence: 0,
            observed_len: 0,
            valid_len: 0,
            reset: true,
            frames: 0,
            upserts: HashSet::new(),
            deletes: HashSet::new(),
        };
    };
    let observed_len = bytes.len() as u64;
    if bytes.len() < JOURNAL_HEADER_LEN
        || !bytes.starts_with(CACHE_JOURNAL_MAGIC)
        || read_u32(&bytes, 8) != Some(CACHE_STORAGE_VERSION)
        || read_digest(&bytes, 12) != Some(instance)
    {
        return JournalCursor {
            sequence: 0,
            observed_len,
            valid_len: 0,
            reset: true,
            frames: 0,
            upserts: HashSet::new(),
            deletes: HashSet::new(),
        };
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
        if payload_len_u64 > MAX_FRAME_BYTES
            || from != cursor.sequence
            || from.checked_add(1) != Some(to)
        {
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
        let Ok(delta) = bincode::deserialize::<JournalDelta>(payload) else {
            break;
        };
        for (key, entry) in delta.upserts {
            entries.insert(key.clone(), entry);
            cursor.upserts.insert(key.clone());
            cursor.deletes.remove(&key);
        }
        for key in delta.deletes {
            entries.remove(&key);
            cursor.upserts.remove(&key);
            cursor.deletes.insert(key);
        }
        offset = frame_end;
        cursor.sequence = to;
        cursor.valid_len = offset as u64;
        cursor.frames += 1;
    }
    cursor
}

fn decode_cache_for(
    path: &Path,
    current: WriterBuildId,
) -> Result<(HashMap<String, Entry>, CacheGeneration, CacheBacking), CacheDecodeRefusal> {
    decode_cache_owned(path, current, false)
}

fn decode_cache_owned(
    path: &Path,
    current: WriterBuildId,
    adopt_foreign: bool,
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
        return Ok((entries, generation, CacheBacking::Rewrite));
    }
    let cursor = replay_journal(&cache_journal_path(path), instance, &mut entries);
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
    Noop,
    Base {
        tmp: PathBuf,
        path: PathBuf,
        journal: PathBuf,
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
    pub fn commit(mut self) -> anyhow::Result<()> {
        let _commit = CACHE_COMMIT_LOCK
            .lock()
            .map_err(|_| anyhow::anyhow!("ingest cache commit lock poisoned"))?;
        match &self.kind {
            StagedCacheKind::Noop => {}
            StagedCacheKind::Base {
                tmp,
                path,
                journal,
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
                crate::cache::replace_file(tmp, path)?;
                let _ = fs::remove_file(journal);
                drop(cursor_guard);
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
            }
        }
        self.committed = true;
        Ok(())
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
            StagedCacheKind::Noop => {}
        }
    }
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
            force_reparse: false,
            repair_mode: false,
            allow_stable_deletions: false,
            repeated_absent_agents: HashSet::new(),
            discarded_base: false,
            discarded_base_confirmed: false,
            #[cfg(test)]
            provisional_deletions: false,
            published_material: None,
            published_blind_scopes: HashSet::new(),
            last_good_base: false,
            repair_expected_agents: HashSet::new(),
            repair_expected_paths: HashSet::new(),
            current_snapshot_complete: false,
            current_source_agents: HashSet::new(),
            current_cleanly_absent_agents: HashSet::new(),
            coverage_expected_paths: HashSet::new(),
            coverage_expected_tokens: HashMap::new(),
            source_stamps: HashMap::new(),
            source_read_issues: Vec::new(),
        }
    }

    fn foreign_owned() -> Self {
        IngestCache {
            write_blocked: true,
            ..Self::base(HashMap::new(), CacheBacking::Rewrite)
        }
    }

    fn put_entry(&mut self, key: String, entry: Entry) {
        self.entries.insert(key.clone(), entry);
        self.deleted.remove(&key);
        self.dirty.insert(key);
    }

    fn remove_entry(&mut self, key: &str) -> Option<Entry> {
        let entry = self.entries.remove(key)?;
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
                ..Self::base(entries, backing)
            },
            Err(CacheDecodeRefusal::ForeignOwner) => Self::foreign_owned(),
            Err(_) => Self::base(HashMap::new(), CacheBacking::Rewrite),
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
            Ok((entries, _generation, _backing)) => (
                IngestCache {
                    force_reparse: true,
                    repair_mode: true,
                    last_good_base: true,
                    ..Self::base(entries, CacheBacking::Rewrite)
                },
                None,
            ),
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

    /// Whether recovery began with no last-good rows because the on-disk base was discarded.
    /// Callers use it to keep deletion evidence at the standard a cached generation set.
    pub fn discarded_base(&self) -> bool {
        self.discarded_base
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

    pub fn output_complete(&self) -> bool {
        !self.output_incomplete
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

    fn published_inventory_blind_to(&self, agent: &str, scope: &Path) -> bool {
        self.published_blind_scopes
            .iter()
            .any(|(blind_agent, blind)| {
                blind_agent == agent
                    && (source_path_within(blind, scope) || source_path_within(scope, blind))
            })
    }

    /// Would publishing without `scope` (an unobservable subtree of `agent`'s store) drop
    /// material the last published generation held and this pass cannot serve?
    ///
    /// Absence of the inventory is [`MaterialVerdict::Unknown`], never `Retained`: a guard
    /// whose job is proving absence must not be satisfiable by absence of its own input.
    /// An inventory blind to `scope` is likewise unknown unless retained rows still answer.
    pub fn published_material_under(&self, agent: &str, scope: &Path) -> MaterialVerdict {
        let Some(published) = self.published_material.as_ref() else {
            return MaterialVerdict::Unknown;
        };
        let has_material = |entry: &Entry| {
            !entry.msgs.is_empty() || !entry.event_keys.is_empty() || entry.legacy_had_events
        };
        // An Always adapter's last-good snapshot carries its whole store, path keys and all.
        let snapshot_key = format!("\x00snapshot\x00{agent}");
        let retained_here = self.entries.get(&snapshot_key).is_some_and(has_material)
            || self.entries.iter().any(|(key, entry)| {
                source_path_from_key(key).is_some_and(|path| source_path_within(&path, scope))
                    && has_material(entry)
            });
        if retained_here {
            return MaterialVerdict::Retained;
        }
        if published.iter().any(|path| source_path_within(path, scope)) {
            return MaterialVerdict::Drops;
        }
        // No path here, and nothing retained. Only an inventory that could actually read the
        // scope turns that silence into the positive claim "it held nothing".
        if self.published_inventory_blind_to(agent, scope) && !self.last_good_base {
            return MaterialVerdict::Unknown;
        }
        MaterialVerdict::Retained
    }

    pub(crate) fn guard_epoch(&self) -> u64 {
        self.guard_epoch
    }

    fn mark_guarded_stale(&mut self) {
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
        let issue = SourceReadIssue {
            agent,
            path: path.to_path_buf(),
            kind,
            reason: reason.into(),
        };
        if !self.source_read_issues.iter().any(|prior| prior == &issue) {
            self.source_read_issues.push(issue);
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
        fresh: Vec<Message>,
        fresh_events: &[Event],
        source_outcome: ReadOutcome,
    ) -> (Vec<Message>, bool) {
        let key = format!("\x00snapshot\x00{agent}");
        let fresh_event_keys = cached_event_keys(fresh_events);
        if matches!(source_outcome, ReadOutcome::Invalid | ReadOutcome::Partial) {
            self.mark_guarded_stale();
            if let Some(entry) = self.entries.get(&key) {
                return (entry.msgs.iter().map(CMsg::to_msg).collect(), true);
            }
            if source_outcome == ReadOutcome::Invalid {
                self.output_incomplete = true;
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
                let fresh_sessions: HashSet<String> = fresh
                    .iter()
                    .map(|message| message.session.to_string())
                    .chain(fresh_events.iter().map(|event| event.session.clone()))
                    .collect();
                self.touched.extend(fresh_sessions);
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
        let prior_material = self.entries.get(&key).map(|entry| {
            !entry.msgs.is_empty()
                || !entry.event_keys.is_empty()
                || entry.legacy_had_events
                || entry.legacy_needs_reparse
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
                if !has_fallback {
                    self.output_incomplete = true;
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
                if let Some(entry) = self.entries.get(&key) {
                    let start = messages.len();
                    messages.extend(entry.msgs.iter().map(CMsg::to_msg));
                    crate::emit::file_done(&messages[start..]);
                    continue;
                }
                if healthy == Some(ReadOutcome::Invalid) {
                    self.output_incomplete = true;
                }
                if healthy != Some(ReadOutcome::Partial) {
                    crate::emit::file_done(&[]);
                    continue;
                }
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
            return Ok(StagedCache {
                kind: StagedCacheKind::Noop,
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
                return stage_base(Some(BaseExpectation {
                    instance: *instance,
                    witness: witness.clone(),
                    cursor: cursor.clone(),
                    sequence: expected_sequence,
                    observed_len: expected_observed_len,
                    valid_len: expected_valid_len,
                    reset: expected_reset,
                    frames: expected_frames,
                }));
            }
            let delta = make_delta(&upserts, &deletes);
            let frame = encode_journal_frame(0, 1, &delta)?;
            let mut bytes = encode_journal_header(*instance);
            bytes.extend_from_slice(&frame);
            if bytes.len() > JOURNAL_COMPACT_BUDGET {
                return stage_base(Some(BaseExpectation {
                    instance: *instance,
                    witness: witness.clone(),
                    cursor: cursor.clone(),
                    sequence: expected_sequence,
                    observed_len: expected_observed_len,
                    valid_len: expected_valid_len,
                    reset: expected_reset,
                    frames: expected_frames,
                }));
            }
            Some((bytes, upserts, deletes))
        } else {
            None
        };
        let (bytes, mode, next_sequence, staged_upserts, staged_deletes) = if expected_reset {
            let delta = make_delta(&delta_upserts, &delta_deletes);
            let frame = encode_journal_frame(0, 1, &delta)?;
            let mut bytes = encode_journal_header(*instance);
            bytes.extend_from_slice(&frame);
            (
                bytes,
                JournalWrite::Replace,
                1,
                delta_upserts,
                delta_deletes,
            )
        } else if let Some((bytes, upserts, deletes)) = compacted {
            (bytes, JournalWrite::Replace, 1, upserts, deletes)
        } else {
            let next = expected_sequence
                .checked_add(1)
                .ok_or_else(|| anyhow::anyhow!("ingest cache journal sequence overflow"))?;
            let delta = make_delta(&delta_upserts, &delta_deletes);
            (
                encode_journal_frame(expected_sequence, next, &delta)?,
                JournalWrite::Append,
                next,
                delta_upserts,
                delta_deletes,
            )
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
        self.stage_save(path)?.commit()
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
        cache.guard_never_empty("kimi", Vec::new(), &[], ReadOutcome::Complete);
        cache.set_current_source_agents(HashSet::new());
        assert!(!cache.adapter_required("kimi"));
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
            vec![test_message("good")],
            &[],
            ReadOutcome::Complete,
        );
        assert_eq!(fresh.len(), 1);
        assert!(!guarded);
        assert!(always.source_snapshot_safe());
        let (served, guarded) =
            always.guard_never_empty("gemini", Vec::new(), &[], ReadOutcome::Complete);
        assert_eq!(served.len(), 1);
        assert!(guarded);
        assert!(!always.source_snapshot_safe());

        let mut partial = IngestCache::cold();
        let (readable, guarded) = partial.guard_never_empty(
            "cline",
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
            vec![old_a, old_b],
            &[test_event("a", "old-a"), test_event("b", "old-b")],
            ReadOutcome::Complete,
        );
        let mut new_a = test_message("new a");
        new_a.session = "a".into();
        let (messages, suppress_events) = merged.guard_never_empty(
            "cline",
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
        let (_, suppress_events) =
            merged.guard_never_empty("cline", vec![no_event_a], &[], ReadOutcome::Skipped);
        assert!(!suppress_events);
        assert!(merged
            .event_prune_files(&merged.live_event_files())
            .contains(&crate::cache::event_fname("claude", "a")));

        let mut invalid = IngestCache::cold();
        let (_, guarded) =
            invalid.guard_never_empty("cline", Vec::new(), &[], ReadOutcome::Invalid);
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
        let (messages, guarded) =
            retry.guard_never_empty("cline", Vec::new(), &[], ReadOutcome::Complete);
        assert!(messages.is_empty());
        assert!(!guarded);
        assert!(retry.has_provisional_deletions());
        assert!(retry.source_snapshot_safe());
        assert!(retry.touched.contains("session"));

        // Confirmation is per agent: another agent's repeated absence proves nothing here.
        let (mut other, refusal) = IngestCache::guarded_retry(&cache_path);
        assert!(refusal.is_none());
        other.allow_repeated_missing_roots(HashSet::from(["kimi".to_string()]));
        let (messages, guarded) =
            other.guard_never_empty("cline", Vec::new(), &[], ReadOutcome::Complete);
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
            vec![test_message("old semantics")],
            &[],
            ReadOutcome::Complete,
        );
        let payload = bincode::serialize(&super::CacheFileRef {
            version: CACHE_VERSION - 1,
            entries: &old.entries,
        })
        .unwrap();
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
                    msgs: vec![CMsg::from(&test_message("legacy"))],
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

        let mut reset = IngestCache::load(&cache_path);
        collect_cached(&mut reset, &root, std::slice::from_ref(&source), |_| {
            (vec![test_message("second")], Vec::new())
        });
        reset.stage_save(&cache_path).unwrap().commit().unwrap();
        assert_eq!(fs::read(&victim).unwrap(), b"do not touch");
        let metadata = fs::symlink_metadata(&journal_path).unwrap();
        assert!(!metadata.file_type().is_symlink());
        assert!(metadata.is_file());

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
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
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
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
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
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
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
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
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
                CacheIdentity::Token(_) => false,
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
                CacheIdentity::Token(_) => {}
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceReadIssue {
    pub agent: &'static str,
    pub path: PathBuf,
    pub kind: &'static str,
    pub reason: String,
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
        }
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
}

impl ReadOutcome {
    pub fn merge(self, other: Self) -> Self {
        match (self, other) {
            (Self::Invalid, _) | (_, Self::Invalid) => Self::Invalid,
            (Self::Skipped, _) | (_, Self::Skipped) => Self::Skipped,
            (Self::Partial, _) | (_, Self::Partial) => Self::Partial,
            _ => Self::Complete,
        }
    }

    /// Whether this read may replace a cached entry wholesale (and so publish deletions).
    fn licenses_replacement(self) -> bool {
        self == Self::Complete
    }
}

fn read_issue_reason(outcome: Option<ReadOutcome>) -> &'static str {
    if outcome == Some(ReadOutcome::Partial) {
        "source parser could not cover part of the read"
    } else {
        "source parser could not complete the read"
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
);
type ParsedSibling = (
    String,
    PathBuf,
    Vec<Message>,
    Vec<Event>,
    Option<ReadOutcome>,
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
        if source_path_within(path, root) && !listed_paths.contains(path) {
            reconciled_files.push(path.clone());
            listed_paths.insert(path.clone());
        }
    }
    for path in cached_paths.values() {
        if !source_path_within(path, root) || listed_paths.contains(path) {
            continue;
        }
        match fs::symlink_metadata(path) {
            Ok(_) => {
                reconciled_files.push(path.clone());
                listed_paths.insert(path.clone());
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
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
        && !cache.repeated_absent_agents.contains(agent)
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
                cache.record_source_read_issue(
                    agent,
                    &path,
                    "source-stat-failed",
                    error.to_string(),
                );
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
            let (m, e, healthy) = parse(p, *mtime, *size).into_parsed();
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
            )
        })
        .collect();

    // sessions touched: from the fresh parses AND from the OLD cache entries of changed files
    // (so a session that moved between files is fully refreshed)
    let mut affected: HashSet<std::sync::Arc<str>> = HashSet::new();
    for (_, _, _, _, _, m, events, _) in &miss_parsed {
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
            let (m, e, healthy) = parse(p, *mtime, *size).into_parsed();
            crate::emit::file_done(&m);
            (key.clone(), p.clone(), m, e, healthy)
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
    // fresh: changed files (messages + events) + sibling files (messages cached-equal + events)
    for (key, path, mt, sz, identity, m, e, healthy) in miss_parsed {
        // A failed, skipped or partial reparse retains the prior entry instead of publishing
        // deletion. Only a partial read with no prior entry falls through and publishes.
        if healthy.is_some_and(|outcome| !outcome.licenses_replacement()) {
            cache.mark_guarded_stale();
            cache.record_source_read_issue(
                agent,
                &path,
                "source-read-failed",
                read_issue_reason(healthy),
            );
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
            if healthy == Some(ReadOutcome::Invalid) {
                cache.output_incomplete = true;
            }
            if healthy != Some(ReadOutcome::Partial) {
                continue;
            }
        }
        if m.is_empty() && e.is_empty() {
            let had_material = cache.entries.get(&key).is_some_and(|prev| {
                !prev.msgs.is_empty() || !prev.event_keys.is_empty() || prev.legacy_had_events
            });
            if had_material {
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
                && cache
                    .repair_expected_paths
                    .iter()
                    .any(|expected| source_path_eq(expected, &path))
            {
                // A Complete read that truthfully yields nothing has no rows to
                // lose and nothing to retry - guarding it wedges every repair pass
                // forever. Only a non-Complete outcome is a silent read failure.
                if !matches!(healthy, Some(ReadOutcome::Complete)) {
                    cache.mark_guarded_stale();
                    cache.output_incomplete = true;
                    cache.record_source_read_issue(
                        agent,
                        &path,
                        "source-read-failed",
                        "source parser could not complete the read",
                    );
                }
                continue;
            }
        }
        if healthy == Some(ReadOutcome::Partial) {
            // Publish recoverable cold rows for this pass, but retry until a complete read
            // produces the first cacheable entry.
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
                identity: Some(identity),
                msgs: m.iter().map(CMsg::from).collect(),
                event_keys,
                legacy_had_events: false,
                legacy_needs_reparse: false,
            },
        );
        messages.extend(m);
        events.extend(e);
    }
    for (key, path, m, e, healthy) in sib_parsed {
        // A sibling read can fail independently after its successful stat. Serving its cached
        // rows mirrors the changed-file last-good guard; its session event files remain untouched
        // because `events` cannot be reconstructed completely without this sibling.
        if healthy.is_some_and(|outcome| !outcome.licenses_replacement()) {
            cache.mark_guarded_stale();
            cache.record_source_read_issue(
                agent,
                &path,
                "source-read-failed",
                read_issue_reason(healthy),
            );
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
            if healthy == Some(ReadOutcome::Invalid) {
                cache.output_incomplete = true;
            }
            if healthy != Some(ReadOutcome::Partial) {
                continue;
            }
        }
        if m.is_empty() && e.is_empty() {
            let had_material = cache.entries.get(&key).is_some_and(|prev| {
                !prev.msgs.is_empty() || !prev.event_keys.is_empty() || prev.legacy_had_events
            });
            if had_material {
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
    if cache.repair_mode
        && (missing_material_source || missing_expected_source)
        && !cache.stable_deletions_provable()
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
