//! Atomic publication for normalized message, reply, session, and tool-event artifacts.
//!
//! `write_messages` emits compact JSON Lines with stable `agent:session:turn` identifiers. The
//! Python search layer imports those rows and joins optional enrichment by the same identifier.

use std::collections::{BTreeMap, HashSet};
use std::fs;
#[cfg(windows)]
use std::io;
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Context as _;
use rayon::prelude::*;
use rusqlite::ffi::ErrorCode;
use rusqlite::{
    params, Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior,
};
use serde::{Deserialize, Serialize};

use crate::model::{Event, Message};

fn content_digest(text: &str) -> String {
    let mut digest = 0xcbf29ce484222325u64;
    for byte in text.as_bytes() {
        digest = (digest ^ u64::from(*byte)).wrapping_mul(0x100000001b3);
    }
    format!("{:04x}", digest & 0xffff)
}

/// Write a file atomically: stream into `<path>.tmp`, flush, then rename over `path`.
/// The temp write keeps half-written files out of the published paths; replacement
/// retries cover transient Windows readers that briefly hold the destination open.
fn tmp_path(path: &Path) -> PathBuf {
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy())
        .unwrap_or_else(|| "tmp".into());
    path.with_file_name(format!("{name}.tmp.{pid}.{nanos}"))
}

#[cfg(windows)]
fn replace_existing(tmp: &Path, path: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let src: Vec<u16> = tmp.as_os_str().encode_wide().chain(Some(0)).collect();
    let dst: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let ok = unsafe {
        MoveFileExW(
            src.as_ptr(),
            dst.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

pub(crate) fn replace_file(tmp: &Path, path: &Path) -> anyhow::Result<()> {
    #[cfg(not(windows))]
    {
        fs::rename(tmp, path)?;
        Ok(())
    }

    #[cfg(windows)]
    {
        let mut delay = Duration::from_millis(25);
        let mut last: Option<io::Error> = None;
        for _ in 0..80 {
            match replace_existing(tmp, path) {
                Ok(()) => return Ok(()),
                Err(e) => {
                    last = Some(e);
                    std::thread::sleep(delay);
                    delay = (delay + delay / 2).min(Duration::from_millis(500));
                }
            }
        }
        Err(last
            .unwrap_or_else(|| io::Error::other("replace failed"))
            .into())
    }
}

/// Publish an already-written staged file through the platform's atomic replacement path.
pub fn promote_file_atomic(tmp: &Path, path: &Path) -> anyhow::Result<()> {
    replace_file(tmp, path)
}

fn write_atomic_buffered<F>(path: &Path, capacity: usize, f: F) -> anyhow::Result<()>
where
    F: FnOnce(&mut BufWriter<fs::File>) -> anyhow::Result<()>,
{
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let tmp = tmp_path(path);
    let result = (|| {
        {
            let mut w = BufWriter::with_capacity(capacity, fs::File::create(&tmp)?);
            f(&mut w)?;
            w.flush()?;
        }
        replace_file(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

fn write_atomic<F>(path: &Path, f: F) -> anyhow::Result<()>
where
    F: FnOnce(&mut BufWriter<fs::File>) -> anyhow::Result<()>,
{
    write_atomic_buffered(path, 8 * 1024, f)
}

/// Bound each write so Windows content filters cannot turn a materialized byte buffer into one
/// pathological filesystem request; replacement still publishes the whole file atomically.
pub fn write_bytes_atomic(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    write_atomic_buffered(path, 32 * 1024, |w| {
        for chunk in bytes.chunks(256 * 1024) {
            w.write_all(chunk)?;
        }
        Ok(())
    })
}

/// Atomic invalidation (directory-entry removal) with NotFound treated as the desired state.
pub fn remove_if_exists(path: &Path) -> anyhow::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

/// One cached row. `id` is the stable join key the sidecar/index reference.
#[derive(Serialize)]
struct Record<'a> {
    id: String,
    agent: &'a str,
    project: &'a str,
    session: &'a str,
    ts: i64,
    turn: u32,
    text: &'a str,
    content_digest: String,
    /// User-side row kind. Agent replies live in replies.jsonl and join by id.
    who: &'a str,
    /// Model on the agent's side of this turn ("" when unknown). Tiny, so it rides along
    /// in the hot file; the bulky reply text goes to the replies sidecar instead.
    #[serde(skip_serializing_if = "str::is_empty")]
    model: &'a str,
    /// explicit | session | temporal_session | unknown | control | synthetic |
    /// recap | harness | explicit_harness | ambiguous_session.
    model_source: &'a str,
}

impl<'a> Record<'a> {
    fn from_message(m: &'a Message) -> Self {
        Record {
            id: format!("{}:{}:{}", m.agent, m.session, m.turn),
            agent: m.agent,
            project: &m.project,
            session: &m.session,
            ts: m.ts,
            turn: m.turn,
            text: &m.text,
            content_digest: content_digest(&m.text),
            who: &m.who,
            model: &m.model,
            model_source: &m.model_source,
        }
    }
}

/// Reply text plus ingest-cap provenance stays outside messages.jsonl so hot
/// embed and affect reads remain lean.
#[derive(Serialize)]
struct ReplyRecord<'a> {
    id: String,
    reply: &'a str,
    content_digest: String,
    reply_chars: usize,
    reply_truncated: bool,
}

/// Write `msgs` as JSON Lines to `path` (creating parent dirs as needed). One compact JSON object
/// per line: `{id, agent, project, session, ts, turn, text}`. Overwrites any existing file.
pub fn write_messages(msgs: &[Message], path: &Path) -> anyhow::Result<()> {
    // Large corpus rows otherwise cycle the standard 8KiB buffer thousands of times. This
    // writer is one of only two concurrent large publications, so 256KiB costs little memory
    // and materially reduces write syscalls on both APFS and Windows filesystems.
    write_atomic_buffered(path, 256 * 1024, |w| {
        // JSON escaping dominates the changed-session rewrite even though the final file is
        // only streamed once. Render bounded chunks across the existing rayon pool, then emit
        // in canonical input order. The chunk cap avoids a second corpus-sized allocation.
        for batch in msgs.chunks(256) {
            let lines: Vec<serde_json::Result<Vec<u8>>> = batch
                .par_iter()
                .map(|m| serde_json::to_vec(&Record::from_message(m)))
                .collect();
            for line in lines {
                w.write_all(&line?)?;
                w.write_all(b"\n")?;
            }
        }
        Ok(())
    })
}

/// Write the agent replies as JSON Lines to `path`, skipping turns with no
/// captured reply. Same stable `id` as `messages.jsonl`, so the detail view joins on it.
pub fn write_replies(msgs: &[Message], path: &Path) -> anyhow::Result<()> {
    write_atomic_buffered(path, 256 * 1024, |w| {
        for batch in msgs.chunks(256) {
            let lines: Vec<serde_json::Result<Option<Vec<u8>>>> = batch
                .par_iter()
                .map(|m| {
                    if m.reply.trim().is_empty() {
                        return Ok(None);
                    }
                    serde_json::to_vec(&ReplyRecord {
                        id: format!("{}:{}:{}", m.agent, m.session, m.turn),
                        reply: &m.reply,
                        content_digest: content_digest(&m.reply),
                        reply_chars: m.reply_chars,
                        reply_truncated: m.reply_chars > crate::ingest::REPLY_CAP,
                    })
                    .map(Some)
                })
                .collect();
            for line in lines {
                if let Some(line) = line? {
                    w.write_all(&line)?;
                    w.write_all(b"\n")?;
                }
            }
        }
        Ok(())
    })
}

/// One row per session in `data/sessions.jsonl`, keeping session listings independent of
/// the far larger messages.jsonl.
#[derive(Serialize)]
struct SessionRecord<'a> {
    session: &'a str,
    agent: &'a str,
    project: &'a str,
    n: u32,
    first_ts: i64,
    last_ts: i64,
    /// First real typed message (compaction recaps skipped), one line, capped.
    first_text: &'a str,
    /// Parent session id for side sessions (subagent/Task children); empty for roots.
    #[serde(skip_serializing_if = "str::is_empty")]
    parent: &'a str,
}

pub const SESSION_FAMILY_META_FILE: &str = "session_family.meta.json";
// v2 reuses the existing MD5+FNV64 composite to avoid a dependency. It detects torn local
// publications; the ingest signature remains the generation commit marker.
const SESSION_FAMILY_META_VERSION: u32 = 2;
const SESSION_FAMILY_DIGEST_ALGORITHM: &str = "md5-fnv64-v1";

#[derive(Serialize)]
struct SessionFamilyMeta<'a> {
    version: u32,
    algorithm: &'static str,
    ingest_signature: &'a str,
    count: usize,
    digest: String,
}

struct SessionAggregate<'a> {
    agent: &'a str,
    project: &'a str,
    n: u32,
    first_ts: i64,
    last_ts: i64,
    first_text: String,
    parent: &'a str,
}

fn aggregate_sessions<'a>(
    msgs: &'a [Message],
) -> anyhow::Result<BTreeMap<&'a str, SessionAggregate<'a>>> {
    let mut by: BTreeMap<&str, SessionAggregate> = BTreeMap::new();
    for m in msgs {
        let a = by.entry(&*m.session).or_insert(SessionAggregate {
            agent: m.agent,
            project: &m.project,
            n: 0,
            first_ts: i64::MAX,
            last_ts: 0,
            first_text: String::new(),
            parent: &m.parent,
        });
        if !m.parent.is_empty() {
            if a.parent.is_empty() {
                a.parent = m.parent.as_ref();
            } else {
                anyhow::ensure!(
                    a.parent == m.parent.as_ref(),
                    "session {} has conflicting parents {} and {}",
                    crate::ingest::terminal_safe(&m.session),
                    crate::ingest::terminal_safe(a.parent),
                    crate::ingest::terminal_safe(&m.parent),
                );
            }
        }
        a.n += 1;
        if m.ts > 0 {
            a.first_ts = a.first_ts.min(m.ts);
            a.last_ts = a.last_ts.max(m.ts);
        }
        if a.first_text.is_empty() && !m.text.trim().is_empty() && m.who.as_ref() != "recap" {
            let one_line: String = m.text.split_whitespace().collect::<Vec<_>>().join(" ");
            a.first_text = one_line.chars().take(120).collect();
        }
    }
    Ok(by)
}

fn write_session_family_aggregate(
    by: &BTreeMap<&str, SessionAggregate<'_>>,
    family_meta_path: &Path,
    ingest_signature: &str,
) -> anyhow::Result<()> {
    let mut digest = Md5FnvDigest::new();
    digest.update(b"agrep-session-family-v1\0");
    for (session, aggregate) in by {
        for value in [*session, aggregate.parent] {
            let bytes = value.as_bytes();
            digest.update(&(bytes.len() as u64).to_le_bytes());
            digest.update(bytes);
        }
    }
    let meta = SessionFamilyMeta {
        version: SESSION_FAMILY_META_VERSION,
        algorithm: SESSION_FAMILY_DIGEST_ALGORITHM,
        ingest_signature: ingest_signature.trim(),
        count: by.len(),
        digest: digest.finish(),
    };
    // Meta comes first so the final ingest signature commits changed generations. Same-signature
    // repairs retain the same family mapping; destructive readers still verify the census digest.
    write_bytes_atomic(family_meta_path, &serde_json::to_vec(&meta)?)
}

/// Publish only the parent-census proof for an already edge-proven session index.
///
/// Goal 9's legacy upgrade uses this narrow path so it can generation-bind an
/// unchanged v4 publication without replacing any of that publication's six
/// proved artifacts.
pub fn write_session_family_meta(
    msgs: &[Message],
    family_meta_path: &Path,
    ingest_signature: &str,
) -> anyhow::Result<usize> {
    let by = aggregate_sessions(msgs)?;
    write_session_family_aggregate(&by, family_meta_path, ingest_signature)?;
    Ok(by.len())
}

/// Write the per-session aggregate index. One pass over the already-deduped messages.
pub fn write_session_index(
    msgs: &[Message],
    path: &Path,
    family_meta_path: &Path,
    ingest_signature: &str,
) -> anyhow::Result<usize> {
    let by = aggregate_sessions(msgs)?;
    let n = by.len();
    write_session_family_aggregate(&by, family_meta_path, ingest_signature)?;
    write_atomic(path, |w| {
        for (session, a) in &by {
            let rec = SessionRecord {
                session,
                agent: a.agent,
                project: a.project,
                n: a.n,
                first_ts: if a.first_ts == i64::MAX {
                    0
                } else {
                    a.first_ts
                },
                last_ts: a.last_ts,
                first_text: &a.first_text,
                parent: a.parent,
            };
            serde_json::to_writer(&mut *w, &rec)?;
            w.write_all(b"\n")?;
        }
        Ok(())
    })?;
    Ok(n)
}

/// One event row inside a per-session file. The file name already carries agent+session,
/// so rows hold only what varies per event; empty fields are omitted to keep lines lean.
#[derive(Serialize)]
struct EventRecord<'a> {
    ts: i64,
    kind: &'a str,
    name: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    input: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    output: &'a str,
    input_chars: usize,
    output_chars: usize,
    output_bytes: usize,
    #[serde(skip_serializing_if = "is_false")]
    input_truncated: bool,
    #[serde(skip_serializing_if = "is_false")]
    output_truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    ok: Option<bool>,
    #[serde(skip_serializing_if = "str::is_empty")]
    call_id: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    child: &'a str,
}

fn is_false(value: &bool) -> bool {
    !*value
}

fn readable_name(s: &str, limit: usize) -> String {
    let mut name: String = s
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '_'
            }
        })
        .take(limit)
        .collect();
    if name.is_empty() {
        name.push_str("session");
    }
    name
}

fn event_identity(agent: &str, session: &str) -> String {
    let mut digest = Md5FnvDigest::new();
    digest.update(agent.as_bytes());
    digest.update(&[0]);
    digest.update(session.as_bytes());
    digest.finish()
}

struct Md5FnvDigest {
    md5: md5::Context,
    fnv: u64,
}

impl Md5FnvDigest {
    fn new() -> Self {
        let mut digest = Self {
            md5: md5::Context::new(),
            fnv: 0xcbf29ce484222325,
        };
        digest.update_fnv(1);
        digest
    }

    fn update(&mut self, bytes: &[u8]) {
        self.md5.consume(bytes);
        for &byte in bytes {
            self.update_fnv(byte);
        }
    }

    fn update_fnv(&mut self, byte: u8) {
        self.fnv ^= byte as u64;
        self.fnv = self.fnv.wrapping_mul(0x100000001b3);
    }

    fn finish(self) -> String {
        format!("{:x}{:016x}", self.md5.compute(), self.fnv)
    }
}

/// FNV-1a 64-bit of a byte slice. The SQLite row stores it as a fast content check.
fn content_hash(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

const EVENT_PROOF_VERSION: u32 = 10;
const EVENT_PROOF_MAX_BYTES: u64 = 256 * 1024;
const EVENT_STATS_MANIFEST: &str = ".stats";
pub const EVENT_STORE_NAME: &str = ".store.sqlite3";
pub const EVENT_GENERATION_NAME: &str = ".generation";
const EVENT_STORE_VERSION: i64 = 2;
const EVENT_STORE_MANIFEST: &[u8] = b"db-v2";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct FileStamp {
    len: u64,
    modified_ns: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct FileIdentitySeal {
    device: u64,
    inode: u64,
    size: u64,
    modified_ns: u64,
    changed_ns: u64,
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
struct EventProof {
    version: u32,
    agents: Vec<String>,
    store: FileIdentitySeal,
    wal: Option<FileIdentitySeal>,
    generation: FileIdentitySeal,
    generation_value: Vec<u8>,
    inventory_hash: u64,
    inventory_hash_b: u64,
    inventory_count: u64,
    stats_hash: u64,
    stats_hash_b: u64,
    canaries: Vec<EventCanary>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
struct EventCanary {
    name: String,
    stamp: FileStamp,
    hash: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct EventState {
    agents: Vec<String>,
    store: FileIdentitySeal,
    wal: Option<FileIdentitySeal>,
    generation: FileIdentitySeal,
    generation_value: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct EventPublicationAuthority {
    state: EventState,
    agents: Vec<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum VerifiedEventScanFailure {
    MissingOrUnsupported,
    GenerationMoved,
    Integrity,
}

#[derive(Debug)]
pub(crate) struct VerifiedEventScanError {
    pub kind: VerifiedEventScanFailure,
    pub detail: String,
}

impl std::fmt::Display for VerifiedEventScanError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl std::error::Error for VerifiedEventScanError {}

pub(crate) struct VerifiedEventSession<'a> {
    pub name: &'a str,
    pub agent: &'a str,
    pub session: &'a str,
    pub n_events: u64,
    pub payload: &'a [u8],
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct VerifiedEventScanSummary {
    pub generation: Vec<u8>,
    pub sessions: u64,
    pub events: u64,
    pub bytes: u64,
    pub trusted_proofs: bool,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
struct ToolCounts {
    n: u64,
    fails: u64,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
struct EventFileStats {
    agent: String,
    calls: u64,
    fails: u64,
    known: u64,
    subagents: u64,
    tools: BTreeMap<String, ToolCounts>,
}

type EventMigrationRow = (String, String, String, u64, u64, Vec<u8>, Vec<u8>);
type StoredEventRow = (String, String, u64, u64, Vec<u8>, Vec<u8>);

struct EventAggregateRow<'a> {
    name: &'a str,
    session: &'a str,
    hash: u64,
    n_events: u64,
    digest: &'a [u8],
    stats: &'a EventFileStats,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct EventInventory {
    count: u64,
    root_a: u64,
    root_b: u64,
    stats_root_a: u64,
    stats_root_b: u64,
}

#[derive(Default)]
struct EventAgentAggregate {
    inventory: EventInventory,
    calls: u64,
    fails: u64,
    known: u64,
    subagents: u64,
    tools: BTreeMap<String, ToolCounts>,
}

fn event_digest(payload: &[u8]) -> [u8; 16] {
    md5::compute(payload).0
}

fn event_row_roots(
    name: &str,
    session: &str,
    hash: u64,
    n_events: u64,
    digest: &[u8],
    stats: &[u8],
) -> (u64, u64) {
    let mut context = md5::Context::new();
    context.consume(name.as_bytes());
    context.consume([0]);
    context.consume(session.as_bytes());
    context.consume([0]);
    context.consume(hash.to_le_bytes());
    context.consume(n_events.to_le_bytes());
    context.consume(digest);
    context.consume(stats);
    let bytes = context.compute().0;
    (
        u64::from_le_bytes(bytes[..8].try_into().unwrap()),
        u64::from_le_bytes(bytes[8..].try_into().unwrap()),
    )
}

fn event_inventory(connection: &Connection, agent: &str) -> anyhow::Result<EventInventory> {
    let mut inventory = connection
        .query_row(
            "SELECT row_count,root_a,root_b,calls,fails,known,subagents
             FROM event_agent_state WHERE agent=?1",
            [agent],
            |row| {
                Ok((
                    EventInventory {
                        count: row.get::<_, i64>(0)? as u64,
                        root_a: row.get::<_, i64>(1)? as u64,
                        root_b: row.get::<_, i64>(2)? as u64,
                        ..EventInventory::default()
                    },
                    row.get::<_, i64>(3)? as u64,
                    row.get::<_, i64>(4)? as u64,
                    row.get::<_, i64>(5)? as u64,
                    row.get::<_, i64>(6)? as u64,
                ))
            },
        )
        .optional()?
        .unwrap_or_default();
    let mut tools = BTreeMap::new();
    let mut statement = connection
        .prepare("SELECT name,n,fails FROM event_tool_stats WHERE agent=?1 ORDER BY name")?;
    for row in statement.query_map([agent], |row| {
        Ok((
            row.get::<_, String>(0)?,
            ToolCounts {
                n: row.get::<_, i64>(1)? as u64,
                fails: row.get::<_, i64>(2)? as u64,
            },
        ))
    })? {
        let (name, counts) = row?;
        tools.insert(name, counts);
    }
    let (stats_root_a, stats_root_b) =
        event_stats_roots(inventory.1, inventory.2, inventory.3, inventory.4, &tools);
    inventory.0.stats_root_a = stats_root_a;
    inventory.0.stats_root_b = stats_root_b;
    Ok(inventory.0)
}

fn event_stats_roots(
    calls: u64,
    fails: u64,
    known: u64,
    subagents: u64,
    tools: &BTreeMap<String, ToolCounts>,
) -> (u64, u64) {
    let mut context = md5::Context::new();
    for value in [calls, fails, known, subagents] {
        context.consume(value.to_le_bytes());
    }
    for (name, counts) in tools {
        context.consume(name.as_bytes());
        context.consume([0]);
        context.consume(counts.n.to_le_bytes());
        context.consume(counts.fails.to_le_bytes());
    }
    let bytes = context.compute().0;
    (
        u64::from_le_bytes(bytes[..8].try_into().unwrap()),
        u64::from_le_bytes(bytes[8..].try_into().unwrap()),
    )
}

fn accumulate_event_row(
    aggregates: &mut BTreeMap<String, EventAgentAggregate>,
    name: &str,
    session: &str,
    hash: u64,
    n_events: u64,
    digest: &[u8],
    stats: &EventFileStats,
) -> anyhow::Result<()> {
    let stats_bytes = serde_json::to_vec(stats)?;
    accumulate_event_row_bytes(
        aggregates,
        EventRowProof {
            name,
            session,
            hash,
            n_events,
            digest,
            stats_bytes: &stats_bytes,
        },
        stats,
    );
    Ok(())
}

struct EventRowProof<'a> {
    name: &'a str,
    session: &'a str,
    hash: u64,
    n_events: u64,
    digest: &'a [u8],
    stats_bytes: &'a [u8],
}

fn accumulate_event_row_bytes(
    aggregates: &mut BTreeMap<String, EventAgentAggregate>,
    proof: EventRowProof<'_>,
    stats: &EventFileStats,
) {
    let (root_a, root_b) = event_row_roots(
        proof.name,
        proof.session,
        proof.hash,
        proof.n_events,
        proof.digest,
        proof.stats_bytes,
    );
    let aggregate = aggregates.entry(stats.agent.clone()).or_default();
    aggregate.inventory.count += 1;
    aggregate.inventory.root_a ^= root_a;
    aggregate.inventory.root_b = aggregate.inventory.root_b.wrapping_add(root_b);
    aggregate.calls += stats.calls;
    aggregate.fails += stats.fails;
    aggregate.known += stats.known;
    aggregate.subagents += stats.subagents;
    for (name, counts) in &stats.tools {
        let tool = aggregate.tools.entry(name.clone()).or_default();
        tool.n += counts.n;
        tool.fails += counts.fails;
    }
}

fn replace_event_aggregates(
    transaction: &Transaction<'_>,
    aggregates: &BTreeMap<String, EventAgentAggregate>,
    scoped: Option<&[&str]>,
) -> anyhow::Result<()> {
    if let Some(agents) = scoped {
        for agent in agents {
            transaction.execute("DELETE FROM event_agent_state WHERE agent=?1", [*agent])?;
            transaction.execute("DELETE FROM event_tool_stats WHERE agent=?1", [*agent])?;
        }
    } else {
        transaction.execute("DELETE FROM event_agent_state", [])?;
        transaction.execute("DELETE FROM event_tool_stats", [])?;
    }
    for (agent, aggregate) in aggregates {
        transaction.execute(
            "INSERT INTO event_agent_state
               (agent,row_count,root_a,root_b,calls,fails,known,subagents)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
            params![
                agent,
                aggregate.inventory.count as i64,
                aggregate.inventory.root_a as i64,
                aggregate.inventory.root_b as i64,
                aggregate.calls as i64,
                aggregate.fails as i64,
                aggregate.known as i64,
                aggregate.subagents as i64,
            ],
        )?;
        for (name, counts) in &aggregate.tools {
            transaction.execute(
                "INSERT INTO event_tool_stats(agent,name,n,fails) VALUES(?1,?2,?3,?4)",
                params![agent, name, counts.n as i64, counts.fails as i64],
            )?;
        }
    }
    Ok(())
}

fn rebuild_event_aggregates(
    transaction: &Transaction<'_>,
    scoped: Option<&[&str]>,
) -> anyhow::Result<()> {
    let mut aggregates = BTreeMap::new();
    let mut consume = |agent: Option<&str>| -> anyhow::Result<()> {
        let sql = if agent.is_some() {
            "SELECT name,agent,session,hash,n_events,digest,stats FROM event_sessions
             WHERE agent=?1 ORDER BY name"
        } else {
            "SELECT name,agent,session,hash,n_events,digest,stats
             FROM event_sessions ORDER BY name"
        };
        let mut statement = transaction.prepare(sql)?;
        let mut query = if let Some(agent) = agent {
            statement.query([agent])?
        } else {
            statement.query([])?
        };
        while let Some(row) = query.next()? {
            let name: String = row.get(0)?;
            let owner: String = row.get(1)?;
            let session: String = row.get(2)?;
            let hash = row.get::<_, i64>(3)? as u64;
            let n_events = row.get::<_, i64>(4)? as u64;
            let digest: Vec<u8> = row.get(5)?;
            let stats: Vec<u8> = row.get(6)?;
            let stats = decode_event_stats(&stats, &owner, &name)?;
            accumulate_event_row(
                &mut aggregates,
                &name,
                &session,
                hash,
                n_events,
                &digest,
                &stats,
            )?;
        }
        Ok(())
    };
    if let Some(agents) = scoped {
        for agent in agents {
            consume(Some(agent))?;
        }
    } else {
        consume(None)?;
    }
    replace_event_aggregates(transaction, &aggregates, scoped)
}

fn adjust_event_row(
    transaction: &Transaction<'_>,
    row: EventAggregateRow<'_>,
    add: bool,
) -> anyhow::Result<()> {
    let EventAggregateRow {
        name,
        session,
        hash,
        n_events,
        digest,
        stats,
    } = row;
    let agent = &stats.agent;
    let stats_bytes = serde_json::to_vec(stats)?;
    let (root_a, root_b) = event_row_roots(name, session, hash, n_events, digest, &stats_bytes);
    let mut state = event_inventory(transaction, agent)?;
    if add {
        state.count = state
            .count
            .checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("event count overflow"))?;
        state.root_a ^= root_a;
        state.root_b = state.root_b.wrapping_add(root_b);
    } else {
        anyhow::ensure!(state.count > 0, "event aggregate underflow for {agent}");
        state.count -= 1;
        state.root_a ^= root_a;
        state.root_b = state.root_b.wrapping_sub(root_b);
    }
    let sign = if add { 1i64 } else { -1i64 };
    let counts =
        [stats.calls, stats.fails, stats.known, stats.subagents].map(|value| (value as i64) * sign);
    let prior = transaction
        .query_row(
            "SELECT calls, fails, known, subagents FROM event_agent_state WHERE agent=?1",
            [agent],
            |row| Ok([row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?]),
        )
        .optional()?
        .unwrap_or([0i64; 4]);
    let totals = [
        prior[0] + counts[0],
        prior[1] + counts[1],
        prior[2] + counts[2],
        prior[3] + counts[3],
    ];
    anyhow::ensure!(
        totals.iter().all(|value| *value >= 0),
        "event stats underflow"
    );
    if state.count == 0 {
        transaction.execute("DELETE FROM event_agent_state WHERE agent=?1", [agent])?;
    } else {
        transaction.execute(
            "INSERT INTO event_agent_state
               (agent,row_count,root_a,root_b,calls,fails,known,subagents)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8)
             ON CONFLICT(agent) DO UPDATE SET
               row_count=excluded.row_count, root_a=excluded.root_a,
               root_b=excluded.root_b, calls=excluded.calls, fails=excluded.fails,
               known=excluded.known, subagents=excluded.subagents",
            params![
                agent,
                state.count as i64,
                state.root_a as i64,
                state.root_b as i64,
                totals[0],
                totals[1],
                totals[2],
                totals[3],
            ],
        )?;
    }
    for (tool, counts) in &stats.tools {
        let prior = transaction
            .query_row(
                "SELECT n, fails FROM event_tool_stats WHERE agent=?1 AND name=?2",
                params![agent, tool],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
            )
            .optional()?
            .unwrap_or_default();
        let next = (
            prior.0 + (counts.n as i64) * sign,
            prior.1 + (counts.fails as i64) * sign,
        );
        anyhow::ensure!(next.0 >= 0 && next.1 >= 0, "event tool stats underflow");
        if next == (0, 0) {
            transaction.execute(
                "DELETE FROM event_tool_stats WHERE agent=?1 AND name=?2",
                params![agent, tool],
            )?;
        } else {
            transaction.execute(
                "INSERT INTO event_tool_stats(agent,name,n,fails) VALUES(?1,?2,?3,?4)
                 ON CONFLICT(agent,name) DO UPDATE SET n=excluded.n, fails=excluded.fails",
                params![agent, tool, next.0, next.1],
            )?;
        }
    }
    Ok(())
}

fn open_event_identity_file(path: &Path) -> anyhow::Result<fs::File> {
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        !crate::ingest::registry::metadata_is_link(&metadata) && metadata.is_file(),
        "event path is not a regular file: {}",
        path.display()
    );
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(0x20_000 | 0x800);
    }
    #[cfg(target_os = "macos")]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(0x100 | 0x4);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };
        options
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path)?;
    let opened = file.metadata()?;
    anyhow::ensure!(
        !crate::ingest::registry::metadata_is_link(&opened) && opened.is_file(),
        "event path changed into a link: {}",
        path.display()
    );
    Ok(file)
}

#[cfg(unix)]
fn event_file_identity(file: &fs::File) -> anyhow::Result<FileIdentitySeal> {
    use std::os::unix::fs::MetadataExt;

    let metadata = file.metadata()?;
    let modified = i128::from(metadata.mtime()) * 1_000_000_000 + i128::from(metadata.mtime_nsec());
    let changed = i128::from(metadata.ctime()) * 1_000_000_000 + i128::from(metadata.ctime_nsec());
    anyhow::ensure!(
        modified >= 0 && changed >= 0,
        "event path predates the system epoch"
    );
    Ok(FileIdentitySeal {
        device: metadata.dev(),
        inode: metadata.ino(),
        size: metadata.len(),
        modified_ns: u64::try_from(modified)?,
        changed_ns: u64::try_from(changed)?,
    })
}

#[cfg(windows)]
fn event_file_identity(file: &fs::File) -> anyhow::Result<FileIdentitySeal> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        FileBasicInfo, GetFileInformationByHandle, GetFileInformationByHandleEx,
        BY_HANDLE_FILE_INFORMATION, FILE_BASIC_INFO,
    };

    let handle = file.as_raw_handle();
    let mut legacy = BY_HANDLE_FILE_INFORMATION::default();
    anyhow::ensure!(
        unsafe { GetFileInformationByHandle(handle, &mut legacy) } != 0,
        std::io::Error::last_os_error()
    );
    let mut basic = FILE_BASIC_INFO::default();
    anyhow::ensure!(
        unsafe {
            GetFileInformationByHandleEx(
                handle,
                FileBasicInfo,
                (&mut basic as *mut FILE_BASIC_INFO).cast(),
                std::mem::size_of::<FILE_BASIC_INFO>() as u32,
            )
        } != 0,
        std::io::Error::last_os_error()
    );
    const WINDOWS_EPOCH_100NS: i64 = 116_444_736_000_000_000;
    anyhow::ensure!(
        basic.LastWriteTime >= WINDOWS_EPOCH_100NS && basic.ChangeTime >= 0,
        "event path has an invalid Windows timestamp"
    );
    Ok(FileIdentitySeal {
        device: u64::from(legacy.dwVolumeSerialNumber),
        inode: (u64::from(legacy.nFileIndexHigh) << 32) | u64::from(legacy.nFileIndexLow),
        size: (u64::from(legacy.nFileSizeHigh) << 32) | u64::from(legacy.nFileSizeLow),
        modified_ns: u64::try_from(basic.LastWriteTime - WINDOWS_EPOCH_100NS)?
            .checked_mul(100)
            .ok_or_else(|| anyhow::anyhow!("event modified timestamp overflows nanoseconds"))?,
        changed_ns: u64::try_from(basic.ChangeTime)?
            .checked_mul(100)
            .ok_or_else(|| anyhow::anyhow!("event change timestamp overflows nanoseconds"))?,
    })
}

#[cfg(not(any(unix, windows)))]
fn event_file_identity(file: &fs::File) -> anyhow::Result<FileIdentitySeal> {
    let metadata = file.metadata()?;
    let modified_ns = metadata
        .modified()?
        .duration_since(UNIX_EPOCH)?
        .as_nanos()
        .min(u64::MAX as u128) as u64;
    Ok(FileIdentitySeal {
        device: 0,
        inode: 0,
        size: metadata.len(),
        modified_ns,
        changed_ns: modified_ns,
    })
}

fn file_identity_seal(path: &Path) -> anyhow::Result<FileIdentitySeal> {
    let first = open_event_identity_file(path)?;
    let identity = event_file_identity(&first)?;
    let second = open_event_identity_file(path)?;
    anyhow::ensure!(
        event_file_identity(&second)? == identity,
        "event path changed while reading its identity: {}",
        path.display()
    );
    Ok(identity)
}

fn read_regular_file(path: &Path) -> anyhow::Result<Vec<u8>> {
    let before = file_identity_seal(path)?;
    let mut file = open_event_identity_file(path)?;
    anyhow::ensure!(
        event_file_identity(&file)? == before,
        "event path changed before reading"
    );
    let mut body = Vec::with_capacity(before.size as usize);
    file.read_to_end(&mut body)?;
    anyhow::ensure!(
        event_file_identity(&file)? == before && file_identity_seal(path)? == before,
        "event path changed while reading"
    );
    Ok(body)
}

fn optional_file_identity_seal(path: &Path) -> anyhow::Result<Option<FileIdentitySeal>> {
    match file_identity_seal(path) {
        Ok(stamp) => Ok(Some(stamp)),
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|error| error.kind() == std::io::ErrorKind::NotFound) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn event_store_path(dir: &Path) -> PathBuf {
    dir.join(EVENT_STORE_NAME)
}

fn validate_event_store_paths(dir: &Path) -> anyhow::Result<()> {
    for name in [
        EVENT_STORE_NAME.to_string(),
        format!("{EVENT_STORE_NAME}-wal"),
        format!("{EVENT_STORE_NAME}-shm"),
        format!("{EVENT_STORE_NAME}-journal"),
    ] {
        validate_event_store_path(&dir.join(name))?;
    }
    Ok(())
}

fn validate_event_store_path(path: &Path) -> anyhow::Result<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    anyhow::ensure!(
        !crate::ingest::registry::metadata_is_link(&metadata) && metadata.is_file(),
        "event store is not a regular file: {}",
        path.display()
    );
    Ok(())
}

#[derive(Debug)]
struct EventStoreStructuralError(String);

impl std::fmt::Display for EventStoreStructuralError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for EventStoreStructuralError {}

fn decode_event_stats(bytes: &[u8], owner: &str, name: &str) -> anyhow::Result<EventFileStats> {
    let stats: EventFileStats = serde_json::from_slice(bytes).map_err(|error| {
        EventStoreStructuralError(format!("event row {name} has invalid stats: {error}"))
    })?;
    if stats.agent != owner {
        return Err(EventStoreStructuralError(format!(
            "event row {name} owner mismatch: column={owner:?}, stats={:?}",
            stats.agent
        ))
        .into());
    }
    Ok(stats)
}

struct EventColumnShape {
    declared_type: String,
    not_null: bool,
    default_value: Option<String>,
    primary_key: i64,
    hidden: i64,
}

fn event_table_shape(
    connection: &Connection,
    table: &str,
) -> anyhow::Result<(BTreeMap<String, EventColumnShape>, Vec<String>)> {
    let mut statement = connection.prepare(&format!("PRAGMA table_xinfo({table})"))?;
    let mut columns = BTreeMap::new();
    let mut primary_key = Vec::new();
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(1)?,
            EventColumnShape {
                declared_type: row.get(2)?,
                not_null: row.get::<_, i64>(3)? != 0,
                default_value: row.get(4)?,
                primary_key: row.get(5)?,
                hidden: row.get(6)?,
            },
        ))
    })?;
    for row in rows {
        let (name, shape) = row?;
        let name = name.to_ascii_lowercase();
        if shape.primary_key > 0 {
            primary_key.push((shape.primary_key, name.clone()));
        }
        columns.insert(name, shape);
    }
    primary_key.sort_by_key(|(position, _)| *position);
    Ok((
        columns,
        primary_key.into_iter().map(|(_, name)| name).collect(),
    ))
}

fn event_table_columns(connection: &Connection, table: &str) -> anyhow::Result<HashSet<String>> {
    Ok(event_table_shape(connection, table)?
        .0
        .into_keys()
        .collect())
}

fn event_store_columns(connection: &Connection) -> anyhow::Result<HashSet<String>> {
    event_table_columns(connection, "event_sessions")
}

fn validate_event_table_schema(
    connection: &Connection,
    table: &str,
    required: &[&str],
    expected_primary_key: &[&str],
) -> anyhow::Result<()> {
    let (columns, primary_key) = event_table_shape(connection, table)?;
    let missing: Vec<_> = required
        .iter()
        .filter(|column| !columns.contains_key(**column))
        .collect();
    if !missing.is_empty() {
        return Err(EventStoreStructuralError(format!(
            "event store table {table} is missing columns: {}",
            missing
                .iter()
                .map(|column| **column)
                .collect::<Vec<_>>()
                .join(", ")
        ))
        .into());
    }
    let unexpected: Vec<_> = columns
        .keys()
        .filter(|column| !required.contains(&column.as_str()))
        .collect();
    if !unexpected.is_empty() {
        return Err(EventStoreStructuralError(format!(
            "event store table {table} has unexpected columns: {}",
            unexpected
                .iter()
                .map(|column| column.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ))
        .into());
    }
    if primary_key != expected_primary_key {
        return Err(EventStoreStructuralError(format!(
            "event store table {table} has primary key {:?}, expected {:?}",
            primary_key, expected_primary_key
        ))
        .into());
    }
    for name in required {
        let shape = &columns[*name];
        let expected_type = match *name {
            "name" | "agent" | "session" | "key" => "TEXT",
            "payload" | "digest" | "stats" | "value" => "BLOB",
            _ => "INTEGER",
        };
        let default_is_valid = match (*name, shape.default_value.as_deref()) {
            (_, None) => true,
            ("digest", Some(value)) => value.eq_ignore_ascii_case("X''"),
            ("stats", Some(value)) => value.eq_ignore_ascii_case("X'7b7d'"),
            _ => false,
        };
        if !shape.declared_type.eq_ignore_ascii_case(expected_type)
            || !shape.not_null
            || shape.hidden != 0
            || !default_is_valid
        {
            return Err(EventStoreStructuralError(format!(
                "event store table {table} column {name} has a noncanonical shape"
            ))
            .into());
        }
    }
    Ok(())
}

fn validate_event_index_columns(
    connection: &Connection,
    index: &str,
    expected: &[&str],
) -> anyhow::Result<()> {
    let mut statement = connection
        .prepare("SELECT cid,name,desc,coll,key FROM pragma_index_xinfo(?1) ORDER BY seqno")?;
    let rows = statement.query_map([index], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, i64>(4)?,
        ))
    })?;
    let mut columns = Vec::new();
    for row in rows {
        let (column_id, name, descending, collation, key) = row?;
        if key == 0 {
            continue;
        }
        let Some(name) = name else {
            return Err(EventStoreStructuralError(format!(
                "event store index {index} contains an expression"
            ))
            .into());
        };
        if column_id < 0 || descending != 0 || !collation.eq_ignore_ascii_case("BINARY") {
            return Err(EventStoreStructuralError(format!(
                "event store index {index} has a noncanonical key"
            ))
            .into());
        }
        columns.push(name.to_ascii_lowercase());
    }
    let expected: Vec<_> = expected.iter().map(|name| (*name).to_string()).collect();
    if columns != expected {
        return Err(EventStoreStructuralError(format!(
            "event store index {index} has columns {columns:?}, expected {expected:?}"
        ))
        .into());
    }
    Ok(())
}

fn validate_event_store_indexes(
    connection: &Connection,
    tables: &[&str],
    require_session_index: bool,
) -> anyhow::Result<()> {
    for table in tables {
        let foreign_key = connection
            .query_row(
                "SELECT 1 FROM pragma_foreign_key_list(?1) LIMIT 1",
                [*table],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        if foreign_key {
            return Err(EventStoreStructuralError(format!(
                "event store table {table} has an unexpected foreign key"
            ))
            .into());
        }
        let expected_primary_key: &[&str] = if *table == "event_tool_stats" {
            &["agent", "name"]
        } else if *table == "event_meta" {
            &["key"]
        } else if *table == "event_sessions" {
            &["name"]
        } else {
            &["agent"]
        };
        let mut statement = connection
            .prepare("SELECT name,\"unique\",origin,partial FROM pragma_index_list(?1)")?;
        let rows = statement.query_map([*table], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })?;
        let mut primary_keys = 0;
        let mut session_index = false;
        for row in rows {
            let (name, unique, origin, partial) = row?;
            if origin == "pk" {
                if unique != 1 || partial != 0 {
                    return Err(EventStoreStructuralError(format!(
                        "event store primary index {name} has a noncanonical shape"
                    ))
                    .into());
                }
                validate_event_index_columns(connection, &name, expected_primary_key)?;
                primary_keys += 1;
            } else if *table == "event_sessions"
                && name.eq_ignore_ascii_case("event_sessions_agent")
            {
                if unique != 0 || origin != "c" || partial != 0 {
                    return Err(EventStoreStructuralError(
                        "event store index event_sessions_agent has a noncanonical shape".into(),
                    )
                    .into());
                }
                validate_event_index_columns(connection, &name, &["agent"])?;
                session_index = true;
            } else {
                return Err(EventStoreStructuralError(format!(
                    "event store table {table} has unexpected index {name}"
                ))
                .into());
            }
        }
        if primary_keys != 1
            || (*table == "event_sessions" && require_session_index && !session_index)
        {
            return Err(EventStoreStructuralError(format!(
                "event store table {table} has an incomplete index inventory"
            ))
            .into());
        }
    }
    Ok(())
}

fn validate_event_store_objects(
    connection: &Connection,
    tables: &[&str],
    require_session_index: bool,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        rusqlite::version_number() >= 3_037_000,
        "SQLite {} lacks PRAGMA table_list required for event-store validation",
        rusqlite::version()
    );
    let mut storage = BTreeMap::new();
    let mut table_list = connection.prepare("PRAGMA table_list")?;
    let rows = table_list.query_map([], |row| {
        Ok((
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    for row in rows {
        let (name, kind, without_rowid, strict) = row?;
        if tables.iter().any(|table| name.eq_ignore_ascii_case(table)) {
            storage.insert(name.to_ascii_lowercase(), (kind, without_rowid, strict));
        }
    }
    let mut definitions = BTreeMap::new();
    let mut statement = connection.prepare(
        "SELECT type,name,tbl_name,coalesce(sql,'') FROM sqlite_schema
         WHERE type IN ('table','index','trigger','view')",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
        ))
    })?;
    for row in rows {
        let (kind, name, owner, sql) = row?;
        if name.eq_ignore_ascii_case("event_sessions_agent")
            && (kind != "index" || !owner.eq_ignore_ascii_case("event_sessions"))
        {
            return Err(EventStoreStructuralError(
                "event store index event_sessions_agent is shadowed by another schema object"
                    .into(),
            )
            .into());
        }
        if kind == "trigger" && tables.iter().any(|table| owner.eq_ignore_ascii_case(table)) {
            return Err(EventStoreStructuralError(format!(
                "event store table {owner} has unexpected trigger {name}"
            ))
            .into());
        }
        if kind == "table" && tables.iter().any(|table| name.eq_ignore_ascii_case(table)) {
            definitions.insert(name.to_ascii_lowercase(), sql);
        }
    }
    for table in tables {
        let mode = storage.get(*table).ok_or_else(|| {
            EventStoreStructuralError(format!("event store table {table} is missing"))
        })?;
        if mode != &("table".to_string(), 1, 0) {
            return Err(EventStoreStructuralError(format!(
                "event store table {table} has noncanonical storage mode {mode:?}"
            ))
            .into());
        }
        let sql = definitions.get(*table).ok_or_else(|| {
            EventStoreStructuralError(format!("event store table {table} is missing"))
        })?;
        let tokens: Vec<_> = sql
            .split(|character: char| !character.is_ascii_alphanumeric() && character != '_')
            .filter(|token| !token.is_empty())
            .collect();
        let forbidden = [
            "CHECK",
            "COLLATE",
            "REFERENCES",
            "GENERATED",
            "CONFLICT",
            "STRICT",
        ];
        if let Some(token) = tokens.iter().find(|token| {
            forbidden
                .iter()
                .any(|word| token.eq_ignore_ascii_case(word))
        }) {
            return Err(EventStoreStructuralError(format!(
                "event store table {table} has forbidden schema token {token}"
            ))
            .into());
        }
    }
    validate_event_store_indexes(connection, tables, require_session_index)
}

fn event_table_exists(connection: &Connection, table: &str) -> anyhow::Result<bool> {
    let mut statement = connection.prepare("SELECT name FROM sqlite_schema WHERE type='table'")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        if row.get::<_, String>(0)?.eq_ignore_ascii_case(table) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn validate_event_store_schema(connection: &Connection) -> anyhow::Result<()> {
    let tables: [(&str, &[&str], &[&str]); 4] = [
        (
            "event_sessions",
            &[
                "name", "agent", "session", "hash", "n_events", "payload", "digest", "stats",
            ],
            &["name"],
        ),
        ("event_meta", &["key", "value"], &["key"]),
        (
            "event_agent_state",
            &[
                "agent",
                "row_count",
                "root_a",
                "root_b",
                "calls",
                "fails",
                "known",
                "subagents",
            ],
            &["agent"],
        ),
        (
            "event_tool_stats",
            &["agent", "name", "n", "fails"],
            &["agent", "name"],
        ),
    ];
    for (table, required, primary_key) in tables {
        validate_event_table_schema(connection, table, required, primary_key)?;
    }
    validate_event_store_objects(
        connection,
        &[
            "event_sessions",
            "event_meta",
            "event_agent_state",
            "event_tool_stats",
        ],
        true,
    )
}

fn validate_event_store_migration_schema(
    connection: &Connection,
    version: i64,
) -> anyhow::Result<()> {
    if version >= EVENT_STORE_VERSION {
        return validate_event_store_schema(connection);
    }
    let legacy: [(&str, &[&str], &[&str]); 4] = [
        (
            "event_sessions",
            &["name", "agent", "session", "hash", "n_events", "payload"],
            &["name"],
        ),
        ("event_meta", &["key", "value"], &["key"]),
        (
            "event_agent_state",
            &[
                "agent",
                "row_count",
                "root_a",
                "root_b",
                "calls",
                "fails",
                "known",
                "subagents",
            ],
            &["agent"],
        ),
        (
            "event_tool_stats",
            &["agent", "name", "n", "fails"],
            &["agent", "name"],
        ),
    ];
    let mut present = Vec::new();
    for (table, required, primary_key) in legacy {
        let required_legacy = version > 0 && matches!(table, "event_sessions" | "event_meta");
        if required_legacy || event_table_exists(connection, table)? {
            validate_event_table_schema(connection, table, required, primary_key)?;
            present.push(table);
        }
    }
    validate_event_store_objects(connection, &present, false)
}

fn migrate_event_store(
    connection: &mut Connection,
    dir: &Path,
    version: i64,
) -> anyhow::Result<()> {
    if version >= EVENT_STORE_VERSION {
        return Ok(());
    }
    let columns = event_store_columns(connection)?;
    if !columns.contains("digest") {
        connection.execute(
            "ALTER TABLE event_sessions ADD COLUMN digest BLOB NOT NULL DEFAULT X''",
            [],
        )?;
    }
    if !columns.contains("stats") {
        connection.execute(
            "ALTER TABLE event_sessions ADD COLUMN stats BLOB NOT NULL DEFAULT X'7b7d'",
            [],
        )?;
    }
    let legacy_stats: std::collections::HashMap<String, EventFileStats> =
        read_regular_file(&dir.join(EVENT_STATS_MANIFEST))
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default();
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut aggregates = BTreeMap::new();
    let mut after = String::new();
    loop {
        let rows: Vec<EventMigrationRow> = {
            let mut statement = transaction.prepare(
                "SELECT name,agent,session,hash,n_events,payload,stats FROM event_sessions
                 WHERE name>?1 ORDER BY name LIMIT 128",
            )?;
            let rows = statement
                .query_map([&after], |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get::<_, i64>(3)? as u64,
                        row.get::<_, i64>(4)? as u64,
                        row.get(5)?,
                        row.get(6)?,
                    ))
                })?
                .collect::<rusqlite::Result<_>>()?;
            rows
        };
        if rows.is_empty() {
            break;
        }
        for (name, agent, session, hash, n_events, payload, old_stats) in rows {
            let mut stats = legacy_stats
                .get(&name)
                .cloned()
                .or_else(|| serde_json::from_slice(&old_stats).ok())
                .unwrap_or_default();
            stats.agent = agent;
            let digest = event_digest(&payload);
            let stats_bytes = serde_json::to_vec(&stats)?;
            transaction.execute(
                "UPDATE event_sessions SET digest=?2, stats=?3 WHERE name=?1",
                params![name, digest.as_slice(), stats_bytes],
            )?;
            accumulate_event_row(
                &mut aggregates,
                &name,
                &session,
                hash,
                n_events,
                &digest,
                &stats,
            )?;
            after = name;
        }
    }
    replace_event_aggregates(&transaction, &aggregates, None)?;
    transaction.pragma_update(None, "user_version", EVENT_STORE_VERSION)?;
    transaction.commit()?;
    Ok(())
}

fn open_event_store(dir: &Path) -> anyhow::Result<Connection> {
    validate_event_store_paths(dir)?;
    let open_path = dir.canonicalize()?.join(EVENT_STORE_NAME);
    let mut connection = Connection::open_with_flags(
        open_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    connection.busy_timeout(Duration::from_secs(5))?;
    let version: i64 = connection.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    anyhow::ensure!(
        (0..=EVENT_STORE_VERSION).contains(&version),
        "unsupported event store version {version}"
    );
    validate_event_store_migration_schema(&connection, version)?;
    connection.pragma_update(None, "journal_mode", "WAL")?;
    connection.pragma_update(None, "synchronous", "NORMAL")?;
    connection.execute_batch(
        "PRAGMA foreign_keys=ON;
         CREATE TABLE IF NOT EXISTS event_sessions (
             name TEXT PRIMARY KEY,
             agent TEXT NOT NULL,
             session TEXT NOT NULL,
             hash INTEGER NOT NULL,
             n_events INTEGER NOT NULL,
             payload BLOB NOT NULL,
             digest BLOB NOT NULL,
             stats BLOB NOT NULL
         ) WITHOUT ROWID;
         CREATE TABLE IF NOT EXISTS event_meta (
             key TEXT PRIMARY KEY,
             value BLOB NOT NULL
         ) WITHOUT ROWID;
         CREATE TABLE IF NOT EXISTS event_agent_state (
             agent TEXT PRIMARY KEY,
             row_count INTEGER NOT NULL,
             root_a INTEGER NOT NULL,
             root_b INTEGER NOT NULL,
             calls INTEGER NOT NULL,
             fails INTEGER NOT NULL,
             known INTEGER NOT NULL,
             subagents INTEGER NOT NULL
         ) WITHOUT ROWID;
         CREATE TABLE IF NOT EXISTS event_tool_stats (
             agent TEXT NOT NULL,
             name TEXT NOT NULL,
             n INTEGER NOT NULL,
             fails INTEGER NOT NULL,
             PRIMARY KEY(agent,name)
         ) WITHOUT ROWID;",
    )?;
    migrate_event_store(&mut connection, dir, version)?;
    connection.execute(
        "CREATE INDEX IF NOT EXISTS event_sessions_agent ON event_sessions(agent)",
        [],
    )?;
    if version < EVENT_STORE_VERSION {
        validate_event_store_schema(&connection)?;
    }
    Ok(connection)
}

fn event_store_is_corrupt(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        if cause.downcast_ref::<EventStoreStructuralError>().is_some() {
            return true;
        }
        cause
            .downcast_ref::<rusqlite::Error>()
            .is_some_and(|error| match error {
                rusqlite::Error::InvalidColumnType(..)
                | rusqlite::Error::FromSqlConversionFailure(..)
                | rusqlite::Error::IntegralValueOutOfRange(..)
                | rusqlite::Error::Utf8Error(..) => true,
                rusqlite::Error::SqliteFailure(failure, _) => matches!(
                    failure.code,
                    ErrorCode::DatabaseCorrupt | ErrorCode::NotADatabase
                ),
                _ => false,
            })
    })
}

fn seal_rebuilt_event_store(dir: &Path) -> anyhow::Result<()> {
    let connection = open_event_store(dir).with_context(|| {
        format!(
            "cannot validate rebuilt event store {}",
            event_store_path(dir).display()
        )
    })?;
    let quick_check: String = connection.query_row("PRAGMA quick_check", [], |row| row.get(0))?;
    anyhow::ensure!(
        quick_check == "ok",
        "rebuilt event store failed quick_check"
    );
    connection.pragma_update(None, "wal_checkpoint", "TRUNCATE")?;
    connection.pragma_update(None, "journal_mode", "DELETE")?;
    drop(connection);
    remove_if_exists(&dir.join(format!("{EVENT_STORE_NAME}-wal")))?;
    remove_if_exists(&dir.join(format!("{EVENT_STORE_NAME}-shm")))?;
    remove_if_exists(&dir.join(format!("{EVENT_STORE_NAME}-journal")))
}

fn restore_quarantined_event_store(moved: &[(PathBuf, PathBuf)]) -> anyhow::Result<()> {
    for (original, quarantine) in moved.iter().rev() {
        if quarantine.exists() {
            replace_file(quarantine, original).with_context(|| {
                format!(
                    "event store remains quarantined at {} after publish failure",
                    quarantine.display()
                )
            })?;
        }
    }
    Ok(())
}

fn publish_rebuilt_event_store_with<F>(
    dir: &Path,
    rebuilt_dir: &Path,
    mut replace: F,
) -> anyhow::Result<Vec<(PathBuf, PathBuf)>>
where
    F: FnMut(&Path, &Path) -> anyhow::Result<()>,
{
    validate_event_store_paths(dir)?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let mut moved = Vec::new();
    for suffix in ["", "-wal", "-shm", "-journal"] {
        let original = dir.join(format!("{EVENT_STORE_NAME}{suffix}"));
        if !original.exists() {
            continue;
        }
        let quarantine = dir.join(format!(
            "{EVENT_STORE_NAME}.corrupt.{}.{}{suffix}",
            std::process::id(),
            stamp
        ));
        if let Err(error) = replace(&original, &quarantine) {
            restore_quarantined_event_store(&moved)?;
            return Err(error).with_context(|| {
                format!(
                    "cannot quarantine corrupt event store {}",
                    original.display()
                )
            });
        }
        moved.push((original, quarantine));
    }

    let rebuilt = event_store_path(rebuilt_dir);
    let published = event_store_path(dir);
    if let Err(error) = replace(&rebuilt, &published) {
        restore_quarantined_event_store(&moved)?;
        return Err(error).with_context(|| {
            format!(
                "cannot publish rebuilt event store {}; prior store restored",
                published.display()
            )
        });
    }
    Ok(moved)
}

fn publish_rebuilt_event_store(
    dir: &Path,
    rebuilt_dir: &Path,
) -> anyhow::Result<Vec<(PathBuf, PathBuf)>> {
    publish_rebuilt_event_store_with(dir, rebuilt_dir, replace_file)
}

fn remove_event_quarantines(moved: &[(PathBuf, PathBuf)]) {
    for (_, quarantine) in moved {
        for attempt in 0..8 {
            match fs::remove_file(quarantine) {
                Ok(()) => break,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => break,
                Err(_) if attempt < 7 => std::thread::sleep(Duration::from_millis(25)),
                Err(_) => break,
            }
        }
    }
}

fn finalize_rebuilt_event_store(
    dir: &Path,
    generation: &[u8],
    stats_path: &Path,
) -> anyhow::Result<()> {
    let store_path = event_store_path(dir);
    let connection = open_event_store(dir)
        .with_context(|| format!("cannot reopen rebuilt event store {}", store_path.display()))?;
    write_event_stats_from_store(&connection, stats_path)?;
    write_bytes_atomic(&dir.join(EVENT_GENERATION_NAME), generation)
}

fn open_existing_event_store_with_timeout(
    dir: &Path,
    timeout: Duration,
) -> anyhow::Result<Option<Connection>> {
    let path = event_store_path(dir);
    if !path.exists() {
        return Ok(None);
    }
    validate_event_store_paths(dir)?;
    let open_path = dir.canonicalize()?.join(EVENT_STORE_NAME);
    let connection = Connection::open_with_flags(
        open_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    connection.busy_timeout(timeout)?;
    let version: i64 = connection.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    anyhow::ensure!(
        version == EVENT_STORE_VERSION,
        "unsupported event store version {version}"
    );
    validate_event_store_schema(&connection)?;
    Ok(Some(connection))
}

fn open_existing_event_store(dir: &Path) -> anyhow::Result<Option<Connection>> {
    open_existing_event_store_with_timeout(dir, Duration::from_secs(5))
}

fn event_proof_path(dir: &Path, agent: &str) -> PathBuf {
    dir.parent()
        .unwrap_or(dir)
        .join(format!(".events_complete.{agent}.json"))
}

fn event_cursor_path(dir: &Path, agent: &str) -> PathBuf {
    dir.parent()
        .unwrap_or(dir)
        .join(format!(".events_cursor.{agent}.bin"))
}

fn invalidate_event_proof(dir: &Path, agent: &str) -> anyhow::Result<()> {
    anyhow::ensure!(event_agent_is_path_safe(agent), "unsafe event agent name");
    remove_if_exists(&event_proof_path(dir, agent))?;
    remove_if_exists(&event_cursor_path(dir, agent))
}

fn remove_event_proof(dir: &Path, agent: &str) -> anyhow::Result<()> {
    anyhow::ensure!(event_agent_is_path_safe(agent), "unsafe event agent name");
    remove_if_exists(&event_proof_path(dir, agent))
}

fn read_event_proof_file(dir: &Path, agent: &str) -> anyhow::Result<EventProof> {
    Ok(read_event_proof_snapshot(dir, agent)?.2)
}

fn read_event_proof_snapshot(
    dir: &Path,
    agent: &str,
) -> anyhow::Result<(FileIdentitySeal, Vec<u8>, EventProof)> {
    anyhow::ensure!(event_agent_is_path_safe(agent), "unsafe event agent name");
    let path = event_proof_path(dir, agent);
    let seal = file_identity_seal(&path)?;
    anyhow::ensure!(
        seal.size <= EVENT_PROOF_MAX_BYTES,
        "event proof exceeds its size limit"
    );
    let raw = read_regular_file(&path)?;
    anyhow::ensure!(
        file_identity_seal(&path)? == seal,
        "event proof changed while it was read"
    );
    let proof = serde_json::from_slice(&raw)?;
    Ok((seal, raw, proof))
}

/// Revoke event completeness before another derived generation advances.
pub fn invalidate_events_complete(dir: &Path, agents: &[&str]) -> anyhow::Result<()> {
    remove_if_exists(&dir.parent().unwrap_or(dir).join(".events_complete.json"))?;
    for agent in agents {
        remove_event_proof(dir, agent)?;
    }
    Ok(())
}

fn event_proof_state(dir: &Path, agents: &[&str]) -> anyhow::Result<EventState> {
    let connection =
        open_existing_event_store(dir)?.ok_or_else(|| anyhow::anyhow!("event store is missing"))?;
    event_proof_state_with_connection(dir, agents, &connection)
}
fn publication_wal_identity(identity: Option<FileIdentitySeal>) -> Option<FileIdentitySeal> {
    // SQLite may touch an empty WAL's metadata on a read; it contains no published state.
    identity.filter(|wal| wal.size != 0)
}

fn read_only_event_file_state(dir: &Path) -> anyhow::Result<EventState> {
    validate_event_store_paths(dir)?;
    let store_path = event_store_path(dir);
    let wal_path = dir.join(format!("{EVENT_STORE_NAME}-wal"));
    let shm_path = dir.join(format!("{EVENT_STORE_NAME}-shm"));
    let journal_path = dir.join(format!("{EVENT_STORE_NAME}-journal"));
    let generation_path = dir.join(EVENT_GENERATION_NAME);
    let capture = || {
        Ok::<_, anyhow::Error>((
            file_identity_seal(&store_path)?,
            optional_file_identity_seal(&wal_path)?,
            optional_file_identity_seal(&shm_path)?,
            optional_file_identity_seal(&journal_path)?,
            file_identity_seal(&generation_path)?,
        ))
    };
    let before = capture()?;
    anyhow::ensure!(
        before.1.is_some() == before.2.is_some()
            && before.1.as_ref().is_none_or(|wal| wal.size == 0)
            && before.3.is_none(),
        "event store sidecar family is incomplete"
    );
    let generation = read_regular_file(&generation_path)?;
    anyhow::ensure!(
        generation.len() == 32
            && generation
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)),
        "event generation is malformed"
    );
    anyhow::ensure!(
        capture()? == before,
        "event store changed while sealing its files"
    );
    Ok(EventState {
        agents: Vec::new(),
        store: before.0,
        wal: publication_wal_identity(before.1),
        generation: before.4,
        generation_value: generation,
    })
}

fn event_proof_state_with_connection(
    dir: &Path,
    agents: &[&str],
    connection: &Connection,
) -> anyhow::Result<EventState> {
    let mut selected: Vec<String> = agents.iter().map(|agent| (*agent).to_string()).collect();
    selected.sort();
    let generation_path = dir.join(EVENT_GENERATION_NAME);
    let before = (
        file_identity_seal(&event_store_path(dir))?,
        publication_wal_identity(optional_file_identity_seal(
            &dir.join(format!("{EVENT_STORE_NAME}-wal")),
        )?),
        file_identity_seal(&generation_path)?,
    );
    let generation = read_regular_file(&generation_path)?;
    anyhow::ensure!(
        connection.query_row(
            "SELECT value FROM event_meta WHERE key='generation'",
            [],
            |row| row.get::<_, Vec<u8>>(0),
        )? == generation,
        "event store generation does not match manifest"
    );
    anyhow::ensure!(
        event_generation_from_store(connection)?.as_bytes() == generation,
        "event store inventory does not match its generation"
    );
    let after = (
        file_identity_seal(&event_store_path(dir))?,
        publication_wal_identity(optional_file_identity_seal(
            &dir.join(format!("{EVENT_STORE_NAME}-wal")),
        )?),
        file_identity_seal(&generation_path)?,
    );
    anyhow::ensure!(
        before == after,
        "event store changed while sealing its generation"
    );
    Ok(EventState {
        agents: selected,
        store: before.0,
        wal: before.1,
        generation: before.2,
        generation_value: generation,
    })
}

type ExactEventInventory = Option<(EventState, EventInventory, Vec<EventCanary>)>;

fn event_canary(connection: &Connection, name: &str) -> anyhow::Result<EventCanary> {
    let (stored_hash, n_events, digest, payload): (i64, u64, Vec<u8>, Vec<u8>) = connection
        .query_row(
            "SELECT hash,n_events,digest,payload FROM event_sessions WHERE name=?1",
            [name],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )?;
    let hash = content_hash(&payload);
    anyhow::ensure!(hash == stored_hash as u64, "event payload hash mismatch");
    anyhow::ensure!(
        event_digest(&payload).as_slice() == digest,
        "event digest mismatch"
    );
    Ok(EventCanary {
        name: name.to_string(),
        stamp: FileStamp {
            len: payload.len() as u64,
            modified_ns: n_events,
        },
        hash,
    })
}

fn scoped_canaries(
    connection: &Connection,
    names: &mut Vec<&str>,
) -> anyhow::Result<Vec<EventCanary>> {
    const MAX: usize = 256;
    names.sort_unstable();
    let count = names.len().min(MAX);
    (0..count)
        .map(|sample| {
            let index = if count <= 1 {
                0
            } else {
                sample * (names.len() - 1) / (count - 1)
            };
            let name = names[index];
            let (stored_hash, n_events, payload_bytes): (i64, u64, u64) = connection.query_row(
                "SELECT hash, n_events, length(payload) FROM event_sessions WHERE name=?1",
                [name],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )?;
            Ok(EventCanary {
                name: name.to_string(),
                stamp: FileStamp {
                    len: payload_bytes,
                    modified_ns: n_events,
                },
                hash: stored_hash as u64,
            })
        })
        .collect()
}

fn bounded_canaries(connection: &Connection, agent: &str) -> anyhow::Result<Vec<EventCanary>> {
    let mut statement = connection
        .prepare("SELECT name FROM event_sessions WHERE agent=?1 ORDER BY name LIMIT 256")?;
    let names = statement
        .query_map([agent], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    drop(statement);
    let mut refs: Vec<&str> = names.iter().map(String::as_str).collect();
    scoped_canaries(connection, &mut refs)
}

fn invalid_canaries(dir: &Path, proofs: &[EventProof]) -> (bool, Vec<String>) {
    const MAX: usize = 16;
    let connection = match open_existing_event_store(dir) {
        Ok(Some(connection)) => connection,
        _ => return (false, Vec::new()),
    };
    let mut readable = true;
    let mut corrupt = Vec::new();
    for proof in proofs {
        if proof.canaries.is_empty() {
            continue;
        }
        let count = proof.canaries.len().min(MAX);
        let bucket = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_secs() / 10)
            .unwrap_or_default();
        let mut seed = content_hash(&proof.generation_value) ^ bucket;
        for byte in proof.agents[0].bytes() {
            seed = (seed ^ byte as u64).wrapping_mul(0x100000001b3);
        }
        let cursor = seed as usize % proof.canaries.len();
        for offset in 0..count {
            let expected = &proof.canaries[(cursor + offset) % proof.canaries.len()];
            match event_canary(&connection, &expected.name) {
                Ok(actual) if actual == *expected => {}
                Ok(_) => corrupt.push(expected.name.clone()),
                Err(_) => readable = false,
            }
        }
    }
    (readable, corrupt)
}

/// A physical store change without a logical generation change is suspicious enough to
/// justify hashing the selected agents' payloads before reissuing their proofs.
fn exact_event_inventories(
    dir: &Path,
    agents: &[&str],
) -> anyhow::Result<Option<Vec<ExactEventInventory>>> {
    let before = match event_proof_state(dir, &[]) {
        Ok(state) => state,
        Err(_) => return Ok(None),
    };
    let connection = match open_existing_event_store(dir) {
        Ok(Some(connection)) => connection,
        _ => return Ok(None),
    };
    let mut inventories = Vec::with_capacity(agents.len());
    for agent in agents {
        let mut computed = EventInventory::default();
        let mut calls = 0u64;
        let mut fails = 0u64;
        let mut known = 0u64;
        let mut subagents = 0u64;
        let mut tools: BTreeMap<String, ToolCounts> = BTreeMap::new();
        let mut statement = connection.prepare(
            "SELECT name,session,hash,n_events,digest,stats,payload
             FROM event_sessions WHERE agent=?1",
        )?;
        let rows = statement.query_map([*agent], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)? as u64,
                row.get::<_, i64>(3)? as u64,
                row.get::<_, Vec<u8>>(4)?,
                row.get::<_, Vec<u8>>(5)?,
                row.get::<_, Vec<u8>>(6)?,
            ))
        })?;
        let mut valid = true;
        for row in rows {
            let (name, session, hash, n_events, digest, stats, payload) = row?;
            if content_hash(&payload) != hash || event_digest(&payload).as_slice() != digest {
                valid = false;
                break;
            }
            let parsed = match serde_json::from_slice::<EventFileStats>(&stats) {
                Ok(parsed) if parsed.agent == *agent => parsed,
                _ => {
                    valid = false;
                    break;
                }
            };
            calls += parsed.calls;
            fails += parsed.fails;
            known += parsed.known;
            subagents += parsed.subagents;
            for (name, counts) in parsed.tools {
                let total = tools.entry(name).or_default();
                total.n += counts.n;
                total.fails += counts.fails;
            }
            let (root_a, root_b) =
                event_row_roots(&name, &session, hash, n_events, &digest, &stats);
            computed.count += 1;
            computed.root_a ^= root_a;
            computed.root_b = computed.root_b.wrapping_add(root_b);
        }
        drop(statement);
        (computed.stats_root_a, computed.stats_root_b) =
            event_stats_roots(calls, fails, known, subagents, &tools);
        if !valid || computed != event_inventory(&connection, agent)? {
            inventories.push(None);
            continue;
        }
        let canaries = bounded_canaries(&connection, agent)?;
        inventories.push(Some((
            EventState {
                agents: vec![(*agent).to_string()],
                store: before.store.clone(),
                wal: before.wal.clone(),
                generation: before.generation.clone(),
                generation_value: before.generation_value.clone(),
            },
            computed,
            canaries,
        )));
    }
    if event_proof_state(dir, &[]).ok().as_ref() != Some(&before) {
        return Ok(None);
    }
    Ok(Some(inventories))
}

fn publish_event_proof(
    dir: &Path,
    state: EventState,
    inventory: EventInventory,
    canaries: Vec<EventCanary>,
) -> anyhow::Result<()> {
    let agent = state
        .agents
        .first()
        .ok_or_else(|| anyhow::anyhow!("event proof requires one agent"))?
        .clone();
    anyhow::ensure!(event_agent_is_path_safe(&agent), "unsafe event agent name");
    let proof = EventProof {
        version: EVENT_PROOF_VERSION,
        agents: state.agents,
        store: state.store,
        wal: state.wal,
        generation: state.generation,
        generation_value: state.generation_value,
        inventory_hash: inventory.root_a,
        inventory_hash_b: inventory.root_b,
        inventory_count: inventory.count,
        stats_hash: inventory.stats_root_a,
        stats_hash_b: inventory.stats_root_b,
        canaries,
    };
    write_bytes_atomic(&event_proof_path(dir, &agent), &serde_json::to_vec(&proof)?)?;
    remove_if_exists(&event_cursor_path(dir, &agent))
}

fn event_proof_matches_inventory(proof: &EventProof, inventory: EventInventory) -> bool {
    proof.inventory_count == inventory.count
        && proof.inventory_hash == inventory.root_a
        && proof.inventory_hash_b == inventory.root_b
        && proof.stats_hash == inventory.stats_root_a
        && proof.stats_hash_b == inventory.stats_root_b
}

/// Validate generation-pinned aggregate proofs, hashing payloads only on unexplained DB churn.
pub fn events_complete(dir: &Path, agents: &[&str]) -> anyhow::Result<bool> {
    if agents.is_empty() {
        return Ok(true);
    }
    let mut priors = Vec::with_capacity(agents.len());
    for agent in agents {
        let prior = read_event_proof_file(dir, agent).ok();
        match prior {
            Some(proof)
                if proof.version == EVENT_PROOF_VERSION
                    && proof.agents == vec![(*agent).to_string()] =>
            {
                priors.push(proof)
            }
            _ => {
                invalidate_event_proof(dir, agent)?;
                return Ok(false);
            }
        }
    }

    let current = match event_proof_state(dir, &[]) {
        Ok(state) => state,
        Err(_) => {
            for agent in agents {
                invalidate_event_proof(dir, agent)?;
            }
            return Ok(false);
        }
    };
    let connection = match open_existing_event_store(dir) {
        Ok(Some(connection)) => connection,
        _ => return Ok(false),
    };
    let mut current_inventories = Vec::with_capacity(agents.len());
    for (agent, prior) in agents.iter().zip(&priors) {
        let inventory = match event_inventory(&connection, agent) {
            Ok(inventory) => inventory,
            Err(error) if event_store_is_corrupt(&error) => {
                for agent in agents {
                    invalidate_event_proof(dir, agent)?;
                }
                return Ok(false);
            }
            Err(error) => return Err(error),
        };
        if !event_proof_matches_inventory(prior, inventory) {
            invalidate_event_proof(dir, agent)?;
            return Ok(false);
        }
        current_inventories.push(inventory);
    }
    let (canaries_readable, corrupt_canaries) = invalid_canaries(dir, &priors);
    if !canaries_readable || !corrupt_canaries.is_empty() {
        // A sampled mismatch invalidates the proof; the forced complete repair rewrites its row.
        for agent in agents {
            invalidate_event_proof(dir, agent)?;
        }
        return Ok(false);
    }
    let mut suspicious = Vec::new();
    for (agent, prior) in agents.iter().zip(&priors) {
        if prior.store == current.store
            && prior.wal == current.wal
            && prior.generation == current.generation
            && prior.generation_value == current.generation_value
        {
            continue;
        }
        suspicious.push(*agent);
    }
    if suspicious.is_empty() {
        return Ok(true);
    }

    let Some(inventories) = exact_event_inventories(dir, &suspicious)? else {
        for agent in &suspicious {
            invalidate_event_proof(dir, agent)?;
        }
        return Ok(false);
    };
    let mut complete = true;
    for (agent, inventory) in suspicious.iter().zip(inventories) {
        let prior = &priors[agents
            .iter()
            .position(|candidate| candidate == agent)
            .unwrap()];
        match inventory {
            Some((state, inventory, canaries))
                if prior.agents == state.agents
                    && event_proof_matches_inventory(prior, inventory) =>
            {
                publish_event_proof(dir, state, inventory, canaries)?;
            }
            _ => {
                invalidate_event_proof(dir, agent)?;
                complete = false;
            }
        }
    }
    Ok(complete)
}

pub fn event_publication_authority(
    dir: &Path,
    agents: &[&str],
) -> anyhow::Result<Option<EventPublicationAuthority>> {
    if !events_complete(dir, agents)? {
        return Ok(None);
    }
    let state = event_proof_state(dir, &[])?;
    if !proof_files_match_state(dir, agents, &state)? {
        return Ok(None);
    }
    let mut selected: Vec<String> = agents.iter().map(|agent| (*agent).to_string()).collect();
    selected.sort();
    Ok(Some(EventPublicationAuthority {
        state,
        agents: selected,
    }))
}

/// The unlocked ingest preflight may validate existing authority but cannot repair it.
pub fn read_only_event_publication_authority(
    dir: &Path,
    agents: &[&str],
) -> anyhow::Result<Option<EventPublicationAuthority>> {
    if agents.is_empty() {
        return Ok(None);
    }
    let mut selected: Vec<String> = agents.iter().map(|agent| (*agent).to_string()).collect();
    selected.sort();
    selected.dedup();
    if selected.len() != agents.len() {
        return Ok(None);
    }
    let mut priors = Vec::with_capacity(agents.len());
    for agent in agents {
        let snapshot = match read_event_proof_snapshot(dir, agent) {
            Ok(snapshot)
                if snapshot.2.version == EVENT_PROOF_VERSION
                    && snapshot.2.agents == vec![(*agent).to_string()] =>
            {
                snapshot
            }
            _ => return Ok(None),
        };
        priors.push(snapshot);
    }
    let before = match read_only_event_file_state(dir) {
        Ok(state) => state,
        Err(_) => return Ok(None),
    };
    if agents.iter().zip(&priors).any(|(agent, snapshot)| {
        let proof = &snapshot.2;
        proof.agents != vec![(*agent).to_string()]
            || proof.store != before.store
            || proof.wal != before.wal
            || proof.generation != before.generation
            || proof.generation_value != before.generation_value
    }) {
        return Ok(None);
    }
    let mut aggregate_rows: Vec<_> = priors
        .iter()
        .map(|snapshot| &snapshot.2)
        .filter(|proof| {
            proof.inventory_count != 0 || proof.inventory_hash != 0 || proof.inventory_hash_b != 0
        })
        .collect();
    aggregate_rows.sort_by(|left, right| left.agents[0].cmp(&right.agents[0]));
    let mut generation = md5::Context::new();
    for proof in aggregate_rows {
        generation.consume(proof.agents[0].as_bytes());
        generation.consume([0]);
        generation.consume(proof.inventory_count.to_le_bytes());
        generation.consume(proof.inventory_hash.to_le_bytes());
        generation.consume(proof.inventory_hash_b.to_le_bytes());
    }
    if format!("{:x}", generation.compute()).as_bytes() != before.generation_value.as_slice() {
        return Ok(None);
    }
    let after = match read_only_event_file_state(dir) {
        Ok(state) if state == before => state,
        _ => return Ok(None),
    };
    for (agent, prior) in agents.iter().zip(&priors) {
        if read_event_proof_snapshot(dir, agent).ok().as_ref() != Some(prior) {
            return Ok(None);
        }
    }
    Ok(Some(EventPublicationAuthority {
        state: after,
        agents: selected,
    }))
}

fn verified_scan_error(
    kind: VerifiedEventScanFailure,
    detail: impl Into<String>,
) -> VerifiedEventScanError {
    VerifiedEventScanError {
        kind,
        detail: detail.into(),
    }
}

fn event_agent_is_path_safe(agent: &str) -> bool {
    !agent.is_empty()
        && agent.len() <= 64
        && agent
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
}

fn proof_authorizes_event_state(
    dir: &Path,
    agent: &str,
    state: &EventState,
    connection: &Connection,
) -> bool {
    let proof = read_event_proof_file(dir, agent).ok();
    let Some(proof) = proof else {
        return false;
    };
    proof.version == EVENT_PROOF_VERSION
        && proof.agents == [agent]
        && proof.store == state.store
        && proof.wal == state.wal
        && proof.generation == state.generation
        && proof.generation_value == state.generation_value
        && event_inventory(connection, agent)
            .ok()
            .is_some_and(|inventory| event_proof_matches_inventory(&proof, inventory))
}

fn complete_event_aggregate(aggregate: &mut EventAgentAggregate) {
    (
        aggregate.inventory.stats_root_a,
        aggregate.inventory.stats_root_b,
    ) = event_stats_roots(
        aggregate.calls,
        aggregate.fails,
        aggregate.known,
        aggregate.subagents,
        &aggregate.tools,
    );
}

fn event_scan_open_failure(error: anyhow::Error) -> VerifiedEventScanError {
    let detail = error.to_string();
    let transient = error.chain().any(|cause| {
        cause
            .downcast_ref::<rusqlite::Error>()
            .is_some_and(|sqlite| match sqlite {
                rusqlite::Error::SqliteFailure(failure, _) => matches!(
                    failure.code,
                    ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked
                ),
                _ => false,
            })
    });
    let kind = if transient {
        VerifiedEventScanFailure::GenerationMoved
    } else if detail.contains("unsupported event store version") {
        VerifiedEventScanFailure::MissingOrUnsupported
    } else {
        VerifiedEventScanFailure::Integrity
    };
    verified_scan_error(kind, detail)
}

fn verified_event_scan_sql(
    require_trusted: bool,
    all_agents_selected: bool,
    selected_len: usize,
) -> String {
    let columns = if require_trusted {
        "name,agent,session,n_events,payload"
    } else {
        "name,agent,session,hash,n_events,digest,stats,payload"
    };
    if all_agents_selected {
        return format!("SELECT {columns} FROM event_sessions ORDER BY name");
    }
    let placeholders = (1..=selected_len)
        .map(|index| format!("?{index}"))
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "SELECT {columns} FROM event_sessions \
         WHERE agent IN ({placeholders}) ORDER BY name"
    )
}

/// Scan one immutable event-store snapshot. Callers must withhold visitor output until `Ok`.
#[cfg(test)]
pub(crate) fn scan_verified_event_generation<F>(
    dir: &Path,
    expected_generation: &[u8],
    agents: &[&str],
    visitor: F,
) -> Result<VerifiedEventScanSummary, VerifiedEventScanError>
where
    F: FnMut(VerifiedEventSession<'_>),
{
    scan_verified_event_generation_impl(dir, expected_generation, agents, false, visitor)
}

pub(crate) fn scan_verified_event_generation_candidates<F>(
    dir: &Path,
    expected_generation: &[u8],
    agents: &[&str],
    visitor: F,
) -> Result<VerifiedEventScanSummary, VerifiedEventScanError>
where
    F: FnMut(VerifiedEventSession<'_>),
{
    scan_verified_event_generation_impl(dir, expected_generation, agents, true, visitor)
}

fn scan_verified_event_generation_impl<F>(
    dir: &Path,
    expected_generation: &[u8],
    agents: &[&str],
    require_trusted: bool,
    mut visitor: F,
) -> Result<VerifiedEventScanSummary, VerifiedEventScanError>
where
    F: FnMut(VerifiedEventSession<'_>),
{
    let mut selected: Vec<&str> = agents.to_vec();
    selected.sort_unstable();
    selected.dedup();
    if selected.is_empty()
        || selected
            .iter()
            .any(|agent| !event_agent_is_path_safe(agent))
    {
        return Err(verified_scan_error(
            VerifiedEventScanFailure::MissingOrUnsupported,
            "event scan requires one or more safe agent names",
        ));
    }

    let connection = match open_existing_event_store_with_timeout(dir, Duration::from_millis(25)) {
        Ok(Some(connection)) => connection,
        Ok(None) => {
            return Err(verified_scan_error(
                VerifiedEventScanFailure::MissingOrUnsupported,
                "event store is missing",
            ));
        }
        Err(error) => return Err(event_scan_open_failure(error)),
    };
    #[cfg(not(windows))]
    let _ = connection.pragma_update(None, "mmap_size", 1_073_741_824_i64);
    connection
        .execute_batch("BEGIN DEFERRED")
        .map_err(|error| {
            verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
        })?;

    let result = (|| {
        let before = event_proof_state_with_connection(dir, &[], &connection).map_err(|error| {
            verified_scan_error(VerifiedEventScanFailure::GenerationMoved, error.to_string())
        })?;
        if before.generation_value != expected_generation {
            return Err(verified_scan_error(
                VerifiedEventScanFailure::GenerationMoved,
                "event generation moved before the scan",
            ));
        }
        let trusted_proofs = selected
            .iter()
            .all(|agent| proof_authorizes_event_state(dir, agent, &before, &connection));
        if require_trusted && !trusted_proofs {
            return Err(verified_scan_error(
                VerifiedEventScanFailure::MissingOrUnsupported,
                "event scan requires current trusted proofs",
            ));
        }
        let represented = connection
            .prepare("SELECT DISTINCT agent FROM event_sessions ORDER BY agent")
            .and_then(|mut statement| {
                statement
                    .query_map([], |row| row.get::<_, String>(0))?
                    .collect::<rusqlite::Result<Vec<_>>>()
            })
            .map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
        let all_agents_selected = represented.len() == selected.len()
            && represented
                .iter()
                .zip(&selected)
                .all(|(stored, wanted)| stored == wanted);
        let sql = verified_event_scan_sql(require_trusted, all_agents_selected, selected.len());
        let mut statement = connection.prepare(&sql).map_err(|error| {
            verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
        })?;
        let mut rows = statement
            .query(rusqlite::params_from_iter(
                (!all_agents_selected)
                    .then_some(selected.iter().copied())
                    .into_iter()
                    .flatten(),
            ))
            .map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
        let mut aggregates: BTreeMap<String, EventAgentAggregate> = BTreeMap::new();
        let mut summary = VerifiedEventScanSummary {
            generation: before.generation_value.clone(),
            trusted_proofs,
            ..VerifiedEventScanSummary::default()
        };
        while let Some(row) = rows.next().map_err(|error| {
            verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
        })? {
            let name_value = row.get_ref(0).map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            let name = name_value.as_str().map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            let agent_value = row.get_ref(1).map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            let agent = agent_value.as_str().map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            let session_value = row.get_ref(2).map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            let session = session_value.as_str().map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            let event_index = if require_trusted { 3 } else { 4 };
            let payload_index = if require_trusted { 4 } else { 7 };
            let stored_events = row.get::<_, i64>(event_index).map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            if stored_events < 0 {
                return Err(verified_scan_error(
                    VerifiedEventScanFailure::Integrity,
                    format!("event row {name} has a negative event count"),
                ));
            }
            let n_events = stored_events as u64;
            let payload_value = row.get_ref(payload_index).map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            let payload = payload_value.as_blob().map_err(|error| {
                verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
            })?;
            if !trusted_proofs {
                let stored_hash = row.get::<_, i64>(3).map_err(|error| {
                    verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
                })? as u64;
                let digest_value = row.get_ref(5).map_err(|error| {
                    verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
                })?;
                let digest = digest_value.as_blob().map_err(|error| {
                    verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
                })?;
                let stats_value = row.get_ref(6).map_err(|error| {
                    verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
                })?;
                let stats_bytes = stats_value.as_blob().map_err(|error| {
                    verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
                })?;
                if content_hash(payload) != stored_hash
                    || event_digest(payload).as_slice() != digest
                    || payload
                        .split(|byte| *byte == b'\n')
                        .filter(|line| !line.is_empty())
                        .count() as u64
                        != n_events
                {
                    return Err(verified_scan_error(
                        VerifiedEventScanFailure::Integrity,
                        format!("event row {name} failed payload validation"),
                    ));
                }
                let stats = decode_event_stats(stats_bytes, agent, name).map_err(|error| {
                    verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
                })?;
                accumulate_event_row_bytes(
                    &mut aggregates,
                    EventRowProof {
                        name,
                        session,
                        hash: stored_hash,
                        n_events,
                        digest,
                        stats_bytes,
                    },
                    &stats,
                );
            }
            summary.sessions = summary.sessions.saturating_add(1);
            summary.events = summary.events.saturating_add(n_events);
            summary.bytes = summary.bytes.saturating_add(payload.len() as u64);
            visitor(VerifiedEventSession {
                name,
                agent,
                session,
                n_events,
                payload,
            });
        }
        drop(rows);
        drop(statement);

        if !trusted_proofs {
            for agent in &selected {
                let aggregate = aggregates.entry((*agent).to_string()).or_default();
                complete_event_aggregate(aggregate);
                let stored = event_inventory(&connection, agent).map_err(|error| {
                    verified_scan_error(VerifiedEventScanFailure::Integrity, error.to_string())
                })?;
                if aggregate.inventory != stored {
                    return Err(verified_scan_error(
                        VerifiedEventScanFailure::Integrity,
                        format!("event inventory for {agent} failed validation"),
                    ));
                }
            }
        }

        let after = event_proof_state_with_connection(dir, &[], &connection).map_err(|error| {
            verified_scan_error(VerifiedEventScanFailure::GenerationMoved, error.to_string())
        })?;
        if after != before || after.generation_value != expected_generation {
            return Err(verified_scan_error(
                VerifiedEventScanFailure::GenerationMoved,
                "event generation moved during the scan",
            ));
        }
        if trusted_proofs
            && !selected
                .iter()
                .all(|agent| proof_authorizes_event_state(dir, agent, &after, &connection))
        {
            return Err(verified_scan_error(
                VerifiedEventScanFailure::GenerationMoved,
                "event proof family moved during the scan",
            ));
        }
        Ok(summary)
    })();
    let rollback = connection.execute_batch("ROLLBACK");
    match (result, rollback) {
        (Ok(summary), Ok(())) => Ok(summary),
        (Err(error), _) => Err(error),
        (Ok(_), Err(error)) => Err(verified_scan_error(
            VerifiedEventScanFailure::Integrity,
            error.to_string(),
        )),
    }
}

fn publish_events_complete_trusted(dir: &Path, agents: &[&str]) -> anyhow::Result<bool> {
    let current = match event_proof_state(dir, &[]) {
        Ok(state) => state,
        Err(_) => {
            for agent in agents {
                invalidate_event_proof(dir, agent)?;
            }
            return Ok(false);
        }
    };
    let connection = match open_existing_event_store(dir) {
        Ok(Some(connection)) => connection,
        _ => {
            for agent in agents {
                invalidate_event_proof(dir, agent)?;
            }
            return Ok(false);
        }
    };
    for agent in agents {
        let inventory = event_inventory(&connection, agent)?;
        let canaries = bounded_canaries(&connection, agent)?;
        let state = EventState {
            agents: vec![(*agent).to_string()],
            store: current.store.clone(),
            wal: current.wal.clone(),
            generation: current.generation.clone(),
            generation_value: current.generation_value.clone(),
        };
        publish_event_proof(dir, state, inventory, canaries)?;
    }
    Ok(true)
}

/// Reuse a current proof or validate every payload once before establishing authority.
pub fn publish_events_complete(dir: &Path, agents: &[&str]) -> anyhow::Result<bool> {
    if events_complete(dir, agents)? {
        return Ok(true);
    }
    let Some(inventories) = exact_event_inventories(dir, agents)? else {
        return Ok(false);
    };
    for (agent, inventory) in agents.iter().zip(inventories) {
        let Some((mut state, inventory, canaries)) = inventory else {
            invalidate_event_proof(dir, agent)?;
            return Ok(false);
        };
        state.agents = vec![(*agent).to_string()];
        publish_event_proof(dir, state, inventory, canaries)?;
    }
    events_complete(dir, agents)
}

fn event_file_stats(agent: &str, events: &[&Event]) -> EventFileStats {
    let mut stats = EventFileStats {
        agent: agent.to_string(),
        ..EventFileStats::default()
    };
    for event in events {
        if matches!(event.kind, "tool" | "control") {
            stats.calls += 1;
            let tool = stats.tools.entry(event.name.clone()).or_default();
            tool.n += 1;
            if event.ok.is_some() {
                stats.known += 1;
            }
            if event.ok == Some(false) {
                stats.fails += 1;
                tool.fails += 1;
            }
        } else {
            stats.subagents += 1;
        }
    }
    stats
}

fn write_event_stats_map<'a>(
    stats: impl Iterator<Item = &'a EventFileStats>,
    path: &Path,
) -> anyhow::Result<()> {
    let body = event_stats_body(stats)?;
    if fs::read_to_string(path).ok().as_deref() != Some(body.as_str()) {
        write_atomic(path, |writer| {
            writer.write_all(body.as_bytes())?;
            Ok(())
        })?;
    }
    Ok(())
}

fn event_stats_body<'a>(stats: impl Iterator<Item = &'a EventFileStats>) -> anyhow::Result<String> {
    #[derive(Default, Serialize)]
    struct AgentStat {
        calls: u64,
        fails: u64,
        known: u64,
        subagents: u64,
    }
    #[derive(Serialize)]
    struct ToolStat {
        name: String,
        n: u64,
        fails: u64,
    }
    #[derive(Serialize)]
    struct Stats {
        total: u64,
        fails: u64,
        subagents: u64,
        by_agent: BTreeMap<String, AgentStat>,
        by_tool: Vec<ToolStat>,
    }

    let mut total = 0;
    let mut fails = 0;
    let mut subagents = 0;
    let mut by_agent: BTreeMap<String, AgentStat> = BTreeMap::new();
    let mut by_tool: BTreeMap<String, ToolCounts> = BTreeMap::new();
    for file in stats {
        total += file.calls;
        fails += file.fails;
        subagents += file.subagents;
        let agent = by_agent.entry(file.agent.clone()).or_default();
        agent.calls += file.calls;
        agent.fails += file.fails;
        agent.known += file.known;
        agent.subagents += file.subagents;
        for (name, counts) in &file.tools {
            let tool = by_tool.entry(name.clone()).or_default();
            tool.n += counts.n;
            tool.fails += counts.fails;
        }
    }
    let mut tools: Vec<_> = by_tool.into_iter().collect();
    tools.sort_by(|left, right| right.1.n.cmp(&left.1.n).then_with(|| left.0.cmp(&right.0)));
    tools.truncate(14);
    Ok(serde_json::to_string_pretty(&Stats {
        total,
        fails,
        subagents,
        by_agent,
        by_tool: tools
            .into_iter()
            .map(|(name, counts)| ToolStat {
                name,
                n: counts.n,
                fails: counts.fails,
            })
            .collect(),
    })?)
}

fn event_generation_from_store(connection: &Connection) -> anyhow::Result<String> {
    let mut context = md5::Context::new();
    let mut statement = connection
        .prepare("SELECT agent,row_count,root_a,root_b FROM event_agent_state ORDER BY agent")?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    for row in rows {
        let (agent, count, root_a, root_b) = row?;
        context.consume(agent.as_bytes());
        context.consume([0]);
        context.consume(count.to_le_bytes());
        context.consume(root_a.to_le_bytes());
        context.consume(root_b.to_le_bytes());
    }
    Ok(format!("{:x}", context.compute()))
}

fn event_row_count(connection: &Connection) -> anyhow::Result<usize> {
    let count: i64 = connection.query_row(
        "SELECT coalesce(sum(row_count),0) FROM event_agent_state",
        [],
        |row| row.get(0),
    )?;
    Ok(count.max(0) as usize)
}

fn event_stats_from_store(
    connection: &Connection,
) -> anyhow::Result<BTreeMap<String, EventFileStats>> {
    let mut by_agent: BTreeMap<String, EventFileStats> = BTreeMap::new();
    {
        let mut statement = connection.prepare(
            "SELECT agent,calls,fails,known,subagents FROM event_agent_state ORDER BY agent",
        )?;
        let rows = statement.query_map([], |row| {
            Ok(EventFileStats {
                agent: row.get(0)?,
                calls: row.get::<_, i64>(1)? as u64,
                fails: row.get::<_, i64>(2)? as u64,
                known: row.get::<_, i64>(3)? as u64,
                subagents: row.get::<_, i64>(4)? as u64,
                tools: BTreeMap::new(),
            })
        })?;
        for row in rows {
            let stats = row?;
            by_agent.insert(stats.agent.clone(), stats);
        }
    }
    {
        let mut statement = connection
            .prepare("SELECT agent,name,n,fails FROM event_tool_stats ORDER BY agent,name")?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                ToolCounts {
                    n: row.get::<_, i64>(2)? as u64,
                    fails: row.get::<_, i64>(3)? as u64,
                },
            ))
        })?;
        for row in rows {
            let (agent, name, counts) = row?;
            if let Some(stats) = by_agent.get_mut(&agent) {
                stats.tools.insert(name, counts);
            }
        }
    }
    Ok(by_agent)
}

fn write_event_stats_from_store(connection: &Connection, path: &Path) -> anyhow::Result<()> {
    let by_agent = event_stats_from_store(connection)?;
    write_event_stats_map(by_agent.values(), path)
}

struct RenderedEventSession {
    fname: String,
    agent: String,
    session: String,
    bytes: Vec<u8>,
    hash: u64,
    digest: [u8; 16],
    n_events: usize,
    stats: EventFileStats,
}

struct StoredEventSession {
    agent: String,
    session: String,
    hash: u64,
    n_events: u64,
    digest: Vec<u8>,
    stats: Vec<u8>,
    payload_len: u64,
    payload: Option<Vec<u8>>,
}

type EventGroup<'a> = ((&'a str, &'a str), Vec<&'a Event>);

fn event_groups(events: &[Event]) -> Vec<EventGroup<'_>> {
    let mut by: BTreeMap<(&str, &str), Vec<&Event>> = BTreeMap::new();
    for event in events {
        by.entry((event.agent, event.session.as_str()))
            .or_default()
            .push(event);
    }
    by.into_iter().collect()
}

fn render_event_group(group: &mut EventGroup<'_>) -> anyhow::Result<Option<RenderedEventSession>> {
    let ((agent, session), events) = group;
    if session.is_empty() {
        return Ok(None);
    }
    #[cfg(test)]
    match *session {
        COUNT_MISMATCH_WATCH_SESSION => {
            COUNT_MISMATCH_RENDER_COUNT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
        SAME_COUNT_WATCH_SESSION => {
            SAME_COUNT_RENDER_COUNT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
        _ => {}
    }
    events.sort_unstable_by(|left, right| {
        (
            left.ts,
            left.kind,
            left.call_id.as_str(),
            left.name.as_str(),
            left.input.as_str(),
            left.output.as_str(),
            left.child_session.as_str(),
            left.ok,
            left.input_chars,
            left.output_chars,
            left.output_bytes,
        )
            .cmp(&(
                right.ts,
                right.kind,
                right.call_id.as_str(),
                right.name.as_str(),
                right.input.as_str(),
                right.output.as_str(),
                right.child_session.as_str(),
                right.ok,
                right.input_chars,
                right.output_chars,
                right.output_bytes,
            ))
    });
    let fname = event_fname(agent, session);
    let mut bytes = Vec::new();
    for event in events.iter() {
        let record = EventRecord {
            ts: event.ts,
            kind: event.kind,
            name: &event.name,
            input: &event.input,
            output: &event.output,
            input_chars: event.input_chars,
            output_chars: event.output_chars,
            output_bytes: event.output_bytes,
            input_truncated: event.input_chars > crate::ingest::EVENT_CAP,
            output_truncated: event.output_chars > crate::ingest::EVENT_CAP,
            ok: event.ok,
            call_id: &event.call_id,
            child: &event.child_session,
        };
        serde_json::to_writer(&mut bytes, &record)?;
        bytes.push(b'\n');
    }
    Ok(Some(RenderedEventSession {
        hash: content_hash(&bytes),
        digest: event_digest(&bytes),
        fname,
        agent: (*agent).to_string(),
        session: (*session).to_string(),
        bytes,
        n_events: events.len(),
        stats: event_file_stats(agent, events),
    }))
}

fn stored_event_session(
    connection: &Connection,
    name: &str,
    include_payload: bool,
) -> anyhow::Result<Option<StoredEventSession>> {
    let sql = if include_payload {
        "SELECT agent,session,hash,n_events,digest,stats,length(payload),payload
         FROM event_sessions WHERE name=?1"
    } else {
        "SELECT agent,session,hash,n_events,digest,stats,length(payload),NULL
         FROM event_sessions WHERE name=?1"
    };
    connection
        .query_row(sql, [name], |row| {
            Ok(StoredEventSession {
                agent: row.get(0)?,
                session: row.get(1)?,
                hash: row.get::<_, i64>(2)? as u64,
                n_events: row.get(3)?,
                digest: row.get(4)?,
                stats: row.get(5)?,
                payload_len: row.get(6)?,
                payload: row.get(7)?,
            })
        })
        .optional()
        .map_err(Into::into)
}

fn rendered_event_matches(
    rendered: &RenderedEventSession,
    stored: Option<&StoredEventSession>,
    stats: &[u8],
    verify_payload: bool,
) -> bool {
    stored.is_some_and(|stored| {
        stored.agent == rendered.agent
            && stored.session == rendered.session
            && stored.hash == rendered.hash
            && stored.n_events == rendered.n_events as u64
            && stored.digest.as_slice() == rendered.digest
            && stored.stats == stats
            && stored.payload_len == rendered.bytes.len() as u64
            && (!verify_payload
                || stored.payload.as_ref().is_some_and(|payload| {
                    content_hash(payload) == rendered.hash
                        && event_digest(payload) == rendered.digest
                }))
    })
}

fn proof_files_match_state(
    dir: &Path,
    agents: &[&str],
    state: &EventState,
) -> anyhow::Result<bool> {
    for agent in agents {
        let proof = read_event_proof_file(dir, agent)?;
        if proof.version != EVENT_PROOF_VERSION
            || proof.agents != vec![(*agent).to_string()]
            || proof.store != state.store
            || proof.wal != state.wal
            || proof.generation != state.generation
            || proof.generation_value != state.generation_value
        {
            return Ok(false);
        }
    }
    Ok(true)
}

fn event_derived_artifacts_match(connection: &Connection, dir: &Path) -> anyhow::Result<bool> {
    let manifest = connection
        .query_row(
            "SELECT value FROM event_meta WHERE key='manifest'",
            [],
            |row| row.get::<_, Vec<u8>>(0),
        )
        .optional()?;
    if manifest.as_deref() != Some(EVENT_STORE_MANIFEST) {
        return Ok(false);
    }

    let generation = event_generation_from_store(connection)?;
    let stored_generation: Vec<u8> = connection.query_row(
        "SELECT value FROM event_meta WHERE key='generation'",
        [],
        |row| row.get(0),
    )?;
    if stored_generation != generation.as_bytes()
        || read_regular_file(&dir.join(EVENT_GENERATION_NAME))? != stored_generation
    {
        return Ok(false);
    }

    let stats = event_stats_from_store(connection)?;
    let expected_stats = event_stats_body(stats.values())?;
    let stats_path = dir.parent().unwrap_or(dir).join("event_stats.json");
    Ok(read_regular_file(&stats_path)? == expected_stats.as_bytes())
}

fn check_event_delta_matches_proven_store(
    events: &[Event],
    dir: &Path,
    keep: &HashSet<String>,
    agents: &[&str],
    prune_files: &HashSet<String>,
) -> anyhow::Result<Option<(usize, usize, usize)>> {
    let Some(connection) = open_existing_event_store(dir)? else {
        return Ok(None);
    };
    let before = event_proof_state_with_connection(dir, &[], &connection)?;
    if !proof_files_match_state(dir, agents, &before)? {
        return Ok(None);
    }
    if !event_derived_artifacts_match(&connection, dir)? {
        return Ok(None);
    }
    let mut groups = event_groups(events);
    let mut n_events = 0usize;
    for batch in groups.chunks_mut(16) {
        let mut stored_rows = Vec::with_capacity(batch.len());
        for ((agent, session), events) in batch.iter() {
            if session.is_empty() {
                stored_rows.push(None);
                continue;
            }
            if !agents.contains(agent) {
                return Ok(None);
            }
            let fname = event_fname(agent, session);
            let stored = stored_event_session(&connection, &fname, false)?;
            if !stored.as_ref().is_some_and(|stored| {
                stored.agent == *agent
                    && stored.session == *session
                    && stored.n_events == events.len() as u64
            }) {
                return Ok(None);
            }
            stored_rows.push(stored);
        }
        let rendered: Vec<anyhow::Result<Option<RenderedEventSession>>> =
            batch.par_iter_mut().map(render_event_group).collect();
        for (rendered, stored) in rendered.into_iter().zip(stored_rows) {
            let Some(rendered) = rendered? else {
                continue;
            };
            let stats = serde_json::to_vec(&rendered.stats)?;
            if !rendered_event_matches(&rendered, stored.as_ref(), &stats, false) {
                return Ok(None);
            }
            n_events += rendered.n_events;
        }
    }
    for name in prune_files {
        if keep.contains(name) {
            continue;
        }
        let owner = connection
            .query_row(
                "SELECT agent FROM event_sessions WHERE name=?1",
                [name],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        if owner
            .as_deref()
            .is_some_and(|owner| agents.contains(&owner))
        {
            return Ok(None);
        }
    }
    let n_files = event_row_count(&connection)?;
    let after = event_proof_state_with_connection(dir, &[], &connection)?;
    if after != before || !event_derived_artifacts_match(&connection, dir)? {
        return Ok(None);
    }
    Ok(Some((n_files, n_events, 0)))
}

/// Compare an incremental delta after `events_complete` proved the current store.
/// Any mismatch or read uncertainty returns `None` for the normal writer.
pub fn event_delta_matches_proven_store(
    events: &[Event],
    dir: &Path,
    keep: &HashSet<String>,
    agents: &[&str],
    prune_files: &HashSet<String>,
) -> Option<(usize, usize, usize)> {
    check_event_delta_matches_proven_store(events, dir, keep, agents, prune_files)
        .ok()
        .flatten()
}

/// Write canonically sorted per-session JSON Lines blobs to the transactional event store.
///
/// INCREMENTAL on two axes:
///  - content: each session's events are built in memory and compared with the SQLite digest;
///    rows are rewritten only when content changed or the store is being repaired.
///  - coverage: `events` may cover only the sessions touched this run (the parse cache hands
///    back events for changed sessions only). `keep` is the exact ownership union from every
///    live cache entry. On incremental runs, only names in `prune_files` (ownership touched by
///    changed/deleted sources and absent from that union) may be removed; a complete run may
///    sweep every scoped name absent from `keep`.
///
/// `agents` scopes the DELETE pass: `keep` is built from this run's messages, so on a
/// single-agent run it contains only that agent's sessions - an unscoped sweep would
/// silently delete every other agent's event streams. Only names whose
/// `{agent}-` prefix belongs to an adapter actually ingested this run are eligible.
/// `full_prune` is true only for a complete source parse. Exact event identities—not message
/// presence—keep unchanged event-only sessions safe on both complete and incremental passes.
///
/// Returns (n_files, n_events_written, n_rewritten).
fn write_events_with_authority(
    events: &[Event],
    dir: &Path,
    keep: &HashSet<String>,
    agents: &[&str],
    full_prune: bool,
    prune_files: &HashSet<String>,
    authority: Option<&EventPublicationAuthority>,
) -> anyhow::Result<(usize, usize, usize)> {
    fs::create_dir_all(dir)?;
    let mut connection = open_event_store(dir)?;
    let seeded = connection
        .query_row("SELECT 1 FROM event_meta WHERE key='manifest'", [], |_| {
            Ok(())
        })
        .optional()?
        .is_some();

    let mut groups = event_groups(events);
    let mut n_events = 0usize;
    let mut n_written = 0usize;
    let mut store_changed = false;
    let rebuild_aggregate = full_prune || !seeded;
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut selected: Vec<String> = agents.iter().map(|agent| (*agent).to_string()).collect();
    selected.sort();
    let authorized_transition = if let Some(authority) = authority {
        let current = event_proof_state_with_connection(dir, &[], &transaction)?;
        authority.agents == selected && authority.state == current
    } else {
        false
    };
    invalidate_events_complete(dir, agents)?;
    // Bounded rendering keeps the transaction to one commit without retaining a corpus-sized
    // second copy of large tool payloads.
    for batch in groups.chunks_mut(16) {
        let rendered: Vec<anyhow::Result<Option<RenderedEventSession>>> =
            batch.par_iter_mut().map(render_event_group).collect();
        for item in rendered {
            let Some(rendered) = item? else {
                continue;
            };
            let stats_bytes = serde_json::to_vec(&rendered.stats)?;
            let stored = stored_event_session(&transaction, &rendered.fname, full_prune)?;
            let current_matches =
                rendered_event_matches(&rendered, stored.as_ref(), &stats_bytes, full_prune);
            if !current_matches {
                if let Some(stored) = &stored {
                    let old_stats =
                        decode_event_stats(&stored.stats, &stored.agent, &rendered.fname)?;
                    if !rebuild_aggregate {
                        adjust_event_row(
                            &transaction,
                            EventAggregateRow {
                                name: &rendered.fname,
                                session: &stored.session,
                                hash: stored.hash,
                                n_events: stored.n_events,
                                digest: &stored.digest,
                                stats: &old_stats,
                            },
                            false,
                        )?;
                    }
                }
                transaction.execute(
                    "INSERT INTO event_sessions
                       (name,agent,session,hash,n_events,payload,digest,stats)
                     VALUES(?1,?2,?3,?4,?5,?6,?7,?8)
                     ON CONFLICT(name) DO UPDATE SET
                       agent=excluded.agent,
                       session=excluded.session,
                       hash=excluded.hash,
                       n_events=excluded.n_events,
                       payload=excluded.payload,
                       digest=excluded.digest,
                       stats=excluded.stats",
                    params![
                        rendered.fname,
                        rendered.agent,
                        rendered.session,
                        rendered.hash as i64,
                        rendered.n_events as u64,
                        rendered.bytes,
                        rendered.digest.as_slice(),
                        stats_bytes,
                    ],
                )?;
                if !rebuild_aggregate {
                    adjust_event_row(
                        &transaction,
                        EventAggregateRow {
                            name: &rendered.fname,
                            session: &rendered.session,
                            hash: rendered.hash,
                            n_events: rendered.n_events as u64,
                            digest: &rendered.digest,
                            stats: &rendered.stats,
                        },
                        true,
                    )?;
                }
                n_written += 1;
                store_changed = true;
            }
            n_events += rendered.n_events;
        }
    }

    if full_prune {
        let mut after = String::new();
        loop {
            let stored: Vec<(String, String)> = {
                let mut statement = transaction.prepare(
                    "SELECT name,agent FROM event_sessions
                     WHERE name>?1 ORDER BY name LIMIT 512",
                )?;
                let rows = statement
                    .query_map([&after], |row| Ok((row.get(0)?, row.get(1)?)))?
                    .collect::<rusqlite::Result<_>>()?;
                rows
            };
            if stored.is_empty() {
                break;
            }
            for (name, owner) in stored {
                after = name.clone();
                if agents.contains(&owner.as_str()) && !keep.contains(&name) {
                    store_changed |= transaction
                        .execute("DELETE FROM event_sessions WHERE name=?1", [&name])?
                        > 0;
                }
            }
        }
    } else {
        for name in prune_files {
            if keep.contains(name) {
                continue;
            }
            let stored: Option<StoredEventRow> = transaction
                .query_row(
                    "SELECT agent,session,hash,n_events,digest,stats
                     FROM event_sessions WHERE name=?1",
                    [name],
                    |row| {
                        Ok((
                            row.get(0)?,
                            row.get(1)?,
                            row.get::<_, i64>(2)? as u64,
                            row.get::<_, i64>(3)? as u64,
                            row.get(4)?,
                            row.get(5)?,
                        ))
                    },
                )
                .optional()?;
            if let Some((owner, session, hash, n_events, digest, stats)) = stored {
                if agents.contains(&owner.as_str()) {
                    let stats = decode_event_stats(&stats, &owner, name)?;
                    if !rebuild_aggregate {
                        adjust_event_row(
                            &transaction,
                            EventAggregateRow {
                                name,
                                session: &session,
                                hash,
                                n_events,
                                digest: &digest,
                                stats: &stats,
                            },
                            false,
                        )?;
                    }
                    store_changed |= transaction
                        .execute("DELETE FROM event_sessions WHERE name=?1", [name])?
                        > 0;
                }
            }
        }
    }

    if rebuild_aggregate {
        rebuild_event_aggregates(&transaction, if seeded { Some(agents) } else { None })?;
    }
    let generation = event_generation_from_store(&transaction)?;
    transaction.execute(
        "INSERT INTO event_meta(key,value) VALUES('manifest',?1)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [EVENT_STORE_MANIFEST],
    )?;
    transaction.execute(
        "INSERT INTO event_meta(key,value) VALUES('generation',?1)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [generation.as_bytes()],
    )?;
    transaction.commit()?;
    write_event_stats_from_store(
        &connection,
        &dir.parent().unwrap_or(dir).join("event_stats.json"),
    )?;
    let n_files = event_row_count(&connection)?;
    let generation_path = dir.join(EVENT_GENERATION_NAME);
    let published_generation = read_regular_file(&generation_path).ok();
    if published_generation.as_deref() != Some(generation.as_bytes()) || store_changed {
        write_atomic(&generation_path, |writer| {
            writer.write_all(generation.as_bytes())?;
            Ok(())
        })?;
    }
    if full_prune || authorized_transition {
        anyhow::ensure!(
            publish_events_complete_trusted(dir, agents)?,
            "event proof publication failed after a trusted transition"
        );
    }
    Ok((n_files, n_events, n_written))
}

pub fn write_events(
    events: &[Event],
    dir: &Path,
    keep: &HashSet<String>,
    agents: &[&str],
    full_prune: bool,
    prune_files: &HashSet<String>,
) -> anyhow::Result<(usize, usize, usize)> {
    write_events_with_authority(events, dir, keep, agents, full_prune, prune_files, None)
}

/// Rebuild a corrupt container only when the caller proved a complete all-agent source parse.
fn legacy_event_artifacts(dir: &Path) -> anyhow::Result<bool> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("cannot inspect event directory {}", dir.display()))
        }
    };
    for entry in entries {
        let entry = entry?;
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            return Ok(true);
        };
        if name.ends_with(".jsonl") || matches!(name.as_str(), ".manifest" | ".stats") {
            return Ok(true);
        }
    }
    Ok(false)
}

#[derive(Clone, Copy)]
pub struct EventRecovery<'a> {
    pub recover_corrupt: bool,
    pub authority: Option<&'a EventPublicationAuthority>,
}

pub fn write_events_recovering_authorized(
    events: &[Event],
    dir: &Path,
    keep: &HashSet<String>,
    agents: &[&str],
    full_prune: bool,
    prune_files: &HashSet<String>,
    recovery: EventRecovery<'_>,
) -> anyhow::Result<(usize, usize, usize)> {
    let EventRecovery {
        recover_corrupt,
        authority,
    } = recovery;
    let store_path = event_store_path(dir);
    let marker_exists = dir.join(EVENT_GENERATION_NAME).exists();
    let legacy_exists = legacy_event_artifacts(dir)?;
    let seeded = open_existing_event_store(dir)
        .ok()
        .flatten()
        .and_then(|connection| {
            connection
                .query_row("SELECT 1 FROM event_meta WHERE key='manifest'", [], |_| {
                    Ok(())
                })
                .optional()
                .ok()
                .flatten()
        })
        .is_some();
    if !seeded && (marker_exists || legacy_exists) && (!full_prune || !recover_corrupt) {
        anyhow::bail!(
            "event store {} needs a complete migration; run `agrep index --full` without `--agent`",
            store_path.display()
        );
    }
    let error = match write_events_with_authority(
        events,
        dir,
        keep,
        agents,
        full_prune,
        prune_files,
        authority,
    ) {
        Ok(result) => return Ok(result),
        Err(error) => error,
    };
    if !event_store_is_corrupt(&error) {
        return Err(error)
            .with_context(|| format!("cannot update event store {}", store_path.display()));
    }
    if !full_prune || !recover_corrupt {
        anyhow::bail!(
            "event store {} is corrupt; run `agrep index --full` without `--agent` to rebuild it",
            store_path.display()
        );
    }

    let rebuild_root = tmp_path(&dir.join(".store-rebuild"));
    let rebuilt_dir = rebuild_root.join("events");
    let result: anyhow::Result<(usize, usize, usize)> = (|| {
        let metrics = write_events_with_authority(
            events,
            &rebuilt_dir,
            keep,
            agents,
            true,
            &HashSet::new(),
            None,
        )?;
        seal_rebuilt_event_store(&rebuilt_dir)?;
        let generation = read_regular_file(&rebuilt_dir.join(EVENT_GENERATION_NAME))?;
        let quarantines = publish_rebuilt_event_store(dir, &rebuilt_dir)?;
        let post_publish = finalize_rebuilt_event_store(
            dir,
            &generation,
            &dir.parent().unwrap_or(dir).join("event_stats.json"),
        );
        if let Err(error) = post_publish {
            let retained = quarantines
                .first()
                .map(|(_, path)| path.display().to_string())
                .unwrap_or_else(|| "none".to_string());
            return Err(error).with_context(|| {
                format!("prior event store retained at {retained} after publish failure")
            });
        }
        remove_event_quarantines(&quarantines);
        Ok(metrics)
    })();
    let _ = fs::remove_dir_all(&rebuild_root);
    result.with_context(|| {
        format!(
            "cannot rebuild corrupt event store {}",
            store_path.display()
        )
    })
}

pub fn write_events_recovering(
    events: &[Event],
    dir: &Path,
    keep: &HashSet<String>,
    agents: &[&str],
    full_prune: bool,
    prune_files: &HashSet<String>,
    recover_corrupt: bool,
) -> anyhow::Result<(usize, usize, usize)> {
    write_events_recovering_authorized(
        events,
        dir,
        keep,
        agents,
        full_prune,
        prune_files,
        EventRecovery {
            recover_corrupt,
            authority: None,
        },
    )
}

/// The per-session event filename for a session (agent + sanitized id). Lets the caller build
/// the `keep` set passed to [`write_events`].
pub fn event_fname(agent: &str, session: &str) -> String {
    format!(
        "{}-{}--{}.jsonl",
        readable_name(agent, 20),
        readable_name(session, 40),
        event_identity(agent, session)
    )
}

/// Aggregate per-agent call/fail counts, tool mix, and subagent totals while events are
/// already in memory at index time.
pub fn write_event_stats(events: &[Event], path: &Path) -> anyhow::Result<()> {
    let mut grouped: BTreeMap<(&str, &str), Vec<&Event>> = BTreeMap::new();
    for event in events {
        grouped
            .entry((event.agent, event.session.as_str()))
            .or_default()
            .push(event);
    }
    let stats: Vec<EventFileStats> = grouped
        .into_iter()
        .map(|((agent, _), events)| event_file_stats(agent, &events))
        .collect();
    write_event_stats_map(stats.iter(), path)
}

#[cfg(test)]
const COUNT_MISMATCH_WATCH_SESSION: &str = "event-count-mismatch-render-watch";
#[cfg(test)]
const SAME_COUNT_WATCH_SESSION: &str = "event-same-count-render-watch";
#[cfg(test)]
static COUNT_MISMATCH_RENDER_COUNT: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);
#[cfg(test)]
static SAME_COUNT_RENDER_COUNT: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

#[cfg(test)]
mod tests {
    use super::*;

    fn test_event() -> Event {
        Event {
            agent: "codex",
            session: "session-1".into(),
            ts: 1_700_000_000_000,
            kind: "tool",
            name: "shell".into(),
            input: "printf test".into(),
            output: "test".into(),
            input_chars: 11,
            output_chars: 4,
            output_bytes: 4,
            ok: Some(true),
            call_id: "call-1".into(),
            child_session: String::new(),
        }
    }

    fn stored_event_body(dir: &Path, name: &str) -> Vec<u8> {
        let connection = open_existing_event_store(dir).unwrap().unwrap();
        connection
            .query_row(
                "SELECT payload FROM event_sessions WHERE name=?1",
                [name],
                |row| row.get(0),
            )
            .unwrap()
    }

    fn event_row_exists(dir: &Path, name: &str) -> bool {
        let connection = open_existing_event_store(dir).unwrap().unwrap();
        connection
            .query_row("SELECT 1 FROM event_sessions WHERE name=?1", [name], |_| {
                Ok(())
            })
            .optional()
            .unwrap()
            .is_some()
    }

    fn assert_complete_event_rebuild(events: &[Event], dir: &Path, keep: &HashSet<String>) {
        let error =
            write_events_recovering(events, dir, keep, &["codex"], true, &HashSet::new(), false)
                .unwrap_err();
        assert!(error.to_string().contains("agrep index --full"));
        write_events_recovering(events, dir, keep, &["codex"], true, &HashSet::new(), true)
            .unwrap();
        assert!(keep.iter().all(|name| event_row_exists(dir, name)));
    }

    fn delete_event_row(dir: &Path, name: &str) {
        open_event_store(dir)
            .unwrap()
            .execute("DELETE FROM event_sessions WHERE name=?1", [name])
            .unwrap();
    }

    fn replace_event_payload(dir: &Path, name: &str, payload: &[u8]) {
        open_event_store(dir)
            .unwrap()
            .execute(
                "UPDATE event_sessions SET payload=?2 WHERE name=?1",
                params![name, payload],
            )
            .unwrap();
    }

    fn insert_event_row(dir: &Path, name: &str, agent: &str, payload: &[u8]) {
        let mut connection = open_event_store(dir).unwrap();
        let transaction = connection.transaction().unwrap();
        let stats = EventFileStats {
            agent: agent.to_string(),
            ..EventFileStats::default()
        };
        let hash = content_hash(payload);
        let digest = event_digest(payload);
        transaction
            .execute(
                "INSERT INTO event_sessions
                   (name,agent,session,hash,n_events,payload,digest,stats)
                 VALUES(?1,?2,'extra',?3,1,?4,?5,?6)",
                params![
                    name,
                    agent,
                    hash as i64,
                    payload,
                    digest.as_slice(),
                    serde_json::to_vec(&stats).unwrap(),
                ],
            )
            .unwrap();
        adjust_event_row(
            &transaction,
            EventAggregateRow {
                name,
                session: "extra",
                hash,
                n_events: 1,
                digest: &digest,
                stats: &stats,
            },
            true,
        )
        .unwrap();
        transaction.commit().unwrap();
    }

    fn event_name_at(dir: &Path, agent: &str, index: usize) -> String {
        open_existing_event_store(dir)
            .unwrap()
            .unwrap()
            .query_row(
                "SELECT name FROM event_sessions WHERE agent=?1 ORDER BY name LIMIT 1 OFFSET ?2",
                params![agent, index as u64],
                |row| row.get(0),
            )
            .unwrap()
    }

    #[test]
    fn control_tool_events_remain_in_diagnostic_counts() {
        let mut event = test_event();
        event.kind = "control";
        event.name = "send_message".into();
        let path = tmp_path(&std::env::temp_dir().join("agrep-event-stats-control"))
            .join("event_stats.json");
        write_event_stats(&[event], &path).unwrap();
        let stats: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(stats["total"], 1);
        assert_eq!(stats["subagents"], 0);
        assert_eq!(stats["by_agent"]["codex"]["calls"], 1);
        assert_eq!(stats["by_tool"][0]["name"], "send_message");
        fs::remove_dir_all(path.parent().unwrap()).ok();
    }

    #[test]
    fn event_filenames_are_bounded_and_preserve_distinct_identities() {
        let slash = event_fname("gemini", "a/b");
        let question = event_fname("gemini", "a?b");
        let upper = event_fname("gemini", "Session");
        let lower = event_fname("gemini", "session");
        assert_ne!(slash, question);
        assert_ne!(upper.to_ascii_lowercase(), lower.to_ascii_lowercase());
        assert!(slash.starts_with("gemini-a_b--"));
        assert!(event_fname("codex", &"x".repeat(10_000)).len() <= 117);
        assert_eq!(
            event_fname("gemini", "a/β?Session"),
            "gemini-a___Session--0cda50f02df0c11ed4b2e40486c8fdb4c12efbdb2bbe07be.jsonl"
        );

        let root = tmp_path(&std::env::temp_dir().join("agrep-event-name-collision"));
        let dir = root.join("events");
        let mut first = test_event();
        first.agent = "gemini";
        first.session = "a/b".into();
        first.call_id = "first".into();
        let mut second = test_event();
        second.agent = "gemini";
        second.session = "a?b".into();
        second.call_id = "second".into();
        let keep = HashSet::from([slash.clone(), question.clone()]);
        assert_eq!(
            write_events(
                &[first, second],
                &dir,
                &keep,
                &["gemini"],
                true,
                &HashSet::new(),
            )
            .unwrap()
            .0,
            2
        );
        assert!(String::from_utf8(stored_event_body(&dir, &slash))
            .unwrap()
            .contains("first"));
        assert!(String::from_utf8(stored_event_body(&dir, &question))
            .unwrap()
            .contains("second"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn incremental_event_stats_keep_untouched_sessions() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-stats-delta"));
        let dir = root.join("events");
        let first = test_event();
        let mut second = test_event();
        second.session = "session-2".into();
        second.call_id = "call-2".into();
        second.name = "grep".into();
        let first_name = event_fname("codex", "session-1");
        let second_name = event_fname("codex", "session-2");
        let keep = HashSet::from([first_name.clone(), second_name.clone()]);

        write_events(
            std::slice::from_ref(&first),
            &dir,
            &HashSet::from([first_name]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        write_events(
            std::slice::from_ref(&second),
            &dir,
            &keep,
            &["codex"],
            false,
            &HashSet::new(),
        )
        .unwrap();

        let stats: serde_json::Value =
            serde_json::from_slice(&fs::read(root.join("event_stats.json")).unwrap()).unwrap();
        assert_eq!(stats["total"], 2);
        assert_eq!(stats["by_agent"]["codex"]["calls"], 2);
        assert_eq!(stats["by_tool"].as_array().unwrap().len(), 2);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn restoring_missing_event_row_refreshes_generation() {
        let dir = tmp_path(&std::env::temp_dir().join("agrep-event-manifest-test"));
        let events = [test_event()];
        let fname = event_fname("codex", "session-1");
        let keep = HashSet::from([fname.clone()]);

        assert_eq!(
            write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap(),
            (1, 1, 1)
        );
        let generation_path = dir.join(EVENT_GENERATION_NAME);
        let generation_body = fs::read(&generation_path).unwrap();

        // Pin the clock so the test distinguishes a true no-op from a repaired DB generation.
        let generation = fs::OpenOptions::new()
            .write(true)
            .open(&generation_path)
            .unwrap();
        generation
            .set_times(fs::FileTimes::new().set_modified(UNIX_EPOCH))
            .unwrap();
        drop(generation);
        let pinned_mtime = fs::metadata(&generation_path).unwrap().modified().unwrap();

        assert_eq!(
            write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap(),
            (1, 1, 0)
        );
        assert_eq!(
            fs::metadata(&generation_path).unwrap().modified().unwrap(),
            pinned_mtime
        );

        delete_event_row(&dir, &fname);
        assert_eq!(
            write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap(),
            (1, 1, 1)
        );
        assert!(event_row_exists(&dir, &fname));
        assert_eq!(fs::read(&generation_path).unwrap(), generation_body);
        assert!(fs::metadata(&generation_path).unwrap().modified().unwrap() > pinned_mtime);

        fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn event_rows_disclose_original_lengths_and_truncation() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-length-test"));
        let dir = root.join("events");
        let mut event = test_event();
        let raw = "x".repeat(crate::ingest::EVENT_CAP + 37);
        event.input = crate::ingest::cap_str(&raw, crate::ingest::EVENT_CAP);
        event.input_chars = raw.chars().count();
        let raw_output = "é".repeat(crate::ingest::EVENT_CAP + 37);
        (event.output, event.output_chars, event.output_bytes) =
            crate::ingest::cap_event_output(&raw_output);
        let fname = event_fname("codex", "session-1");
        let keep = HashSet::from([fname.clone()]);
        write_events(&[event], &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();

        let body = String::from_utf8(stored_event_body(&dir, &fname)).unwrap();
        let row: serde_json::Value = serde_json::from_str(body.trim()).unwrap();
        assert_eq!(row["input_chars"], crate::ingest::EVENT_CAP + 37);
        assert_eq!(row["output_chars"], crate::ingest::EVENT_CAP + 37);
        assert_eq!(row["output_bytes"], 2 * (crate::ingest::EVENT_CAP + 37));
        assert_eq!(row["input_truncated"], true);
        assert_eq!(row["output_truncated"], true);
        assert_eq!(
            row["output"].as_str().unwrap().chars().count(),
            crate::ingest::EVENT_CAP + 1
        );
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn equal_timestamp_events_publish_in_canonical_order() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-order-test"));
        let dir = root.join("events");
        let mut first = test_event();
        first.call_id = "call-z".into();
        first.name = "zeta".into();
        let mut second = test_event();
        second.call_id = "call-a".into();
        second.name = "alpha".into();
        let fname = event_fname("codex", "session-1");
        let keep = HashSet::from([fname.clone()]);

        assert_eq!(
            write_events(
                &[first.clone(), second.clone()],
                &dir,
                &keep,
                &["codex"],
                true,
                &HashSet::new(),
            )
            .unwrap()
            .2,
            1
        );
        let body = stored_event_body(&dir, &fname);
        assert!(!dir.join(".manifest").exists());
        assert_eq!(
            write_events(
                &[second, first],
                &dir,
                &keep,
                &["codex"],
                true,
                &HashSet::new(),
            )
            .unwrap()
            .2,
            0
        );
        assert_eq!(stored_event_body(&dir, &fname), body);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn parallel_event_batches_publish_complete_deterministic_generation() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-parallel-batches"));
        let dir = root.join("events");
        let mut events: Vec<Event> = (0..64)
            .map(|index| {
                let mut event = test_event();
                event.session = format!("session-{index:03}");
                event.call_id = format!("call-{index:03}");
                event.output = format!("output-{index:03}");
                event
            })
            .collect();
        let keep: HashSet<String> = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();

        assert_eq!(
            write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap(),
            (64, 64, 64)
        );
        let generation = fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap();
        assert!(!dir.join(".manifest").exists());
        for event in &events {
            let body = String::from_utf8(stored_event_body(
                &dir,
                &event_fname(event.agent, &event.session),
            ))
            .unwrap();
            assert!(body.contains(&event.call_id));
        }

        events.reverse();
        assert_eq!(
            write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap(),
            (64, 64, 0)
        );
        assert_eq!(
            fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap(),
            generation
        );
        fs::remove_dir_all(root).ok();
    }
    #[test]
    fn partial_first_event_store_publishes_complete() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-partial-first"));
        let dir = root.join("events");
        let events = [test_event()];
        let keep = HashSet::from([event_fname("codex", "session-1")]);

        write_events(&events, &dir, &keep, &["codex"], false, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(events_complete(&dir, &["codex"]).unwrap());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn proven_unchanged_event_delta_preserves_store_and_proof() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proven-noop"));
        let dir = root.join("events");
        let events = [test_event()];
        let name = event_fname("codex", "session-1");
        let keep = HashSet::from([name]);
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        let proof_path = event_proof_path(&dir, "codex");
        let proof = fs::read(&proof_path).unwrap();
        let state = event_proof_state(&dir, &[]).unwrap();
        let stats = fs::read(root.join("event_stats.json")).unwrap();
        assert_eq!(
            event_delta_matches_proven_store(&events, &dir, &keep, &["codex"], &HashSet::new(),),
            Some((1, 1, 0))
        );
        assert_eq!(event_proof_state(&dir, &[]).unwrap(), state);
        assert_eq!(fs::read(proof_path).unwrap(), proof);
        assert_eq!(fs::read(root.join("event_stats.json")).unwrap(), stats);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_count_mismatch_skips_precheck_render_and_publishes_exact_bytes() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-count-shortcut"));
        let dir = root.join("events");
        let expected_root = tmp_path(&std::env::temp_dir().join("agrep-event-count-expected"));
        let expected_dir = expected_root.join("events");
        let mut first = test_event();
        first.session = COUNT_MISMATCH_WATCH_SESSION.to_string();
        let name = event_fname(first.agent, &first.session);
        let keep = HashSet::from([name.clone()]);
        write_events(
            std::slice::from_ref(&first),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        let authority = event_publication_authority(&dir, &["codex"])
            .unwrap()
            .unwrap();

        let mut appended = first.clone();
        appended.ts += 1;
        appended.call_id = "call-2".into();
        appended.output = "next".into();
        let events = [first, appended];
        COUNT_MISMATCH_RENDER_COUNT.store(0, std::sync::atomic::Ordering::SeqCst);
        assert!(event_delta_matches_proven_store(
            &events,
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        assert_eq!(
            COUNT_MISMATCH_RENDER_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            0
        );
        assert_eq!(
            write_events_recovering_authorized(
                &events,
                &dir,
                &keep,
                &["codex"],
                false,
                &HashSet::new(),
                EventRecovery {
                    recover_corrupt: false,
                    authority: Some(&authority),
                },
            )
            .unwrap(),
            (1, 2, 1)
        );
        assert_eq!(
            COUNT_MISMATCH_RENDER_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            1
        );
        assert!(events_complete(&dir, &["codex"]).unwrap());

        write_events(
            &events,
            &expected_dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert_eq!(
            stored_event_body(&dir, &name),
            stored_event_body(&expected_dir, &name)
        );
        assert_eq!(
            fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap(),
            fs::read(expected_dir.join(EVENT_GENERATION_NAME)).unwrap()
        );
        assert_eq!(
            fs::read(root.join("event_stats.json")).unwrap(),
            fs::read(expected_root.join("event_stats.json")).unwrap()
        );
        fs::remove_dir_all(root).ok();
        fs::remove_dir_all(expected_root).ok();
    }

    #[test]
    fn same_count_event_mutation_still_renders_compares_and_rewrites() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-same-count-change"));
        let dir = root.join("events");
        let expected_root = tmp_path(&std::env::temp_dir().join("agrep-event-same-count-expected"));
        let expected_dir = expected_root.join("events");
        let mut original = test_event();
        original.session = SAME_COUNT_WATCH_SESSION.to_string();
        let name = event_fname(original.agent, &original.session);
        let keep = HashSet::from([name.clone()]);
        write_events(
            std::slice::from_ref(&original),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        let authority = event_publication_authority(&dir, &["codex"])
            .unwrap()
            .unwrap();

        let mut changed = original;
        changed.output = "next".into();
        let events = [changed];
        SAME_COUNT_RENDER_COUNT.store(0, std::sync::atomic::Ordering::SeqCst);
        assert!(event_delta_matches_proven_store(
            &events,
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        assert_eq!(
            SAME_COUNT_RENDER_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            1
        );
        assert_eq!(
            write_events_recovering_authorized(
                &events,
                &dir,
                &keep,
                &["codex"],
                false,
                &HashSet::new(),
                EventRecovery {
                    recover_corrupt: false,
                    authority: Some(&authority),
                },
            )
            .unwrap(),
            (1, 1, 1)
        );
        assert_eq!(
            SAME_COUNT_RENDER_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            2
        );
        assert!(events_complete(&dir, &["codex"]).unwrap());

        write_events(
            &events,
            &expected_dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert_eq!(
            stored_event_body(&dir, &name),
            stored_event_body(&expected_dir, &name)
        );
        assert_eq!(
            fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap(),
            fs::read(expected_dir.join(EVENT_GENERATION_NAME)).unwrap()
        );
        fs::remove_dir_all(root).ok();
        fs::remove_dir_all(expected_root).ok();
    }

    #[test]
    fn event_delta_precheck_rejects_missing_or_tampered_stats() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proven-stats"));
        let dir = root.join("events");
        let events = [test_event()];
        let keep = HashSet::from([event_fname("codex", "session-1")]);
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        let stats_path = root.join("event_stats.json");
        let expected_stats = fs::read(&stats_path).unwrap();
        let proof_path = event_proof_path(&dir, "codex");
        let proof = fs::read(&proof_path).unwrap();
        let state = event_proof_state(&dir, &[]).unwrap();

        fs::remove_file(&stats_path).unwrap();
        assert!(event_delta_matches_proven_store(
            &events,
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        assert!(!stats_path.exists());
        assert_eq!(fs::read(&proof_path).unwrap(), proof);
        assert_eq!(event_proof_state(&dir, &[]).unwrap(), state);

        write_bytes_atomic(&stats_path, b"{}").unwrap();
        assert!(event_delta_matches_proven_store(
            &events,
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        assert_eq!(fs::read(&stats_path).unwrap(), b"{}");
        assert_eq!(fs::read(&proof_path).unwrap(), proof);
        assert_eq!(event_proof_state(&dir, &[]).unwrap(), state);

        write_bytes_atomic(&stats_path, &expected_stats).unwrap();
        assert!(event_delta_matches_proven_store(
            &events,
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_some());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_delta_precheck_rejects_missing_or_wrong_manifest() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proven-manifest"));
        let dir = root.join("events");
        let events = [test_event()];
        let keep = HashSet::from([event_fname("codex", "session-1")]);
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();

        for manifest in [None, Some(b"wrong".as_slice())] {
            {
                let connection = open_event_store(&dir).unwrap();
                if let Some(manifest) = manifest {
                    connection
                        .execute(
                            "INSERT INTO event_meta(key,value) VALUES('manifest',?1)
                             ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            [manifest],
                        )
                        .unwrap();
                } else {
                    connection
                        .execute("DELETE FROM event_meta WHERE key='manifest'", [])
                        .unwrap();
                }
            }
            assert!(publish_events_complete(&dir, &["codex"]).unwrap());
            let proof_path = event_proof_path(&dir, "codex");
            let proof = fs::read(&proof_path).unwrap();
            let state = event_proof_state(&dir, &[]).unwrap();
            assert!(event_delta_matches_proven_store(
                &events,
                &dir,
                &keep,
                &["codex"],
                &HashSet::new(),
            )
            .is_none());
            assert_eq!(fs::read(proof_path).unwrap(), proof);
            assert_eq!(event_proof_state(&dir, &[]).unwrap(), state);
        }
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_delta_precheck_rejects_generation_not_derived_from_aggregates() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proven-generation"));
        let dir = root.join("events");
        let events = [test_event()];
        let keep = HashSet::from([event_fname("codex", "session-1")]);
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();

        {
            let connection = open_event_store(&dir).unwrap();
            connection
                .execute(
                    "UPDATE event_meta SET value=?1 WHERE key='generation'",
                    [b"stale"],
                )
                .unwrap();
        }
        write_bytes_atomic(&dir.join(EVENT_GENERATION_NAME), b"stale").unwrap();
        assert!(!publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(!event_proof_path(&dir, "codex").exists());
        assert!(event_delta_matches_proven_store(
            &events,
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_delta_precheck_rejects_events_outside_proof_scope() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proven-scope"));
        let dir = root.join("events");
        let codex = test_event();
        let mut claude = codex.clone();
        claude.agent = "claude";
        claude.session = "session-2".into();
        let events = [codex, claude.clone()];
        let keep = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(
            &events,
            &dir,
            &keep,
            &["codex", "claude"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        assert!(event_delta_matches_proven_store(
            &[claude],
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_delta_precheck_falls_through_on_mismatch_prune_or_uncertainty() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proven-fallback"));
        let dir = root.join("events");
        let event = test_event();
        let name = event_fname("codex", "session-1");
        let keep = HashSet::from([name.clone()]);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        let mut changed = event.clone();
        changed.output = "changed".into();
        assert!(event_delta_matches_proven_store(
            &[changed],
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        assert!(event_delta_matches_proven_store(
            std::slice::from_ref(&event),
            &dir,
            &HashSet::new(),
            &["codex"],
            &HashSet::from([name]),
        )
        .is_none());
        assert_eq!(
            event_delta_matches_proven_store(
                std::slice::from_ref(&event),
                &dir,
                &keep,
                &["codex"],
                &HashSet::from(["missing.jsonl".to_string()]),
            ),
            Some((1, 1, 0))
        );
        fs::remove_file(event_proof_path(&dir, "codex")).unwrap();
        assert!(event_delta_matches_proven_store(
            &[event],
            &dir,
            &keep,
            &["codex"],
            &HashSet::new(),
        )
        .is_none());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_proof_detects_missing_manifest_rows_and_scoped_extras() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-test"));
        let dir = root.join("events");
        let events = [test_event()];
        let fname = event_fname("codex", "session-1");
        let keep = HashSet::from([fname.clone()]);

        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        // A complete source reconstruction may publish its proof with the transaction.
        assert!(events_complete(&dir, &["codex"]).unwrap());
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(events_complete(&dir, &["codex"]).unwrap());
        assert!(event_proof_path(&dir, "codex").exists());

        delete_event_row(&dir, &fname);
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        // A smaller external generation must not agree with the transaction-pinned DB marker.
        delete_event_row(&dir, &fname);
        write_bytes_atomic(&dir.join(".manifest"), b"{}").unwrap();
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        insert_event_row(&dir, "codex-extra.jsonl", "codex", b"{}\n");
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(!event_row_exists(&dir, "codex-extra.jsonl"));
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        // The canary rejects payload corruption even when the stored logical hash is unchanged.
        replace_event_payload(&dir, &fname, b"{");
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(event_row_exists(&dir, &fname));
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(events_complete(&dir, &["codex"]).unwrap());

        open_event_store(&dir)
            .unwrap()
            .execute(
                "UPDATE event_sessions SET session='misrouted' WHERE name=?1",
                [&fname],
            )
            .unwrap();
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        let route: (String, String) = open_existing_event_store(&dir)
            .unwrap()
            .unwrap()
            .query_row(
                "SELECT agent,session FROM event_sessions WHERE name=?1",
                [&fname],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(route, ("codex".into(), "session-1".into()));

        fs::remove_file(dir.join(".manifest")).unwrap();
        assert!(events_complete(&dir, &["codex"]).unwrap());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_proof_detects_unsampled_physical_corruption_without_cursor_writes() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-rotation"));
        let dir = root.join("events");
        let events: Vec<Event> = (0..20)
            .map(|index| {
                let mut event = test_event();
                event.session = format!("session-{index:02}");
                event.call_id = format!("call-{index:02}");
                event
            })
            .collect();
        let keep: HashSet<String> = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        let target = event_name_at(&dir, "codex", 16);
        let mut body = stored_event_body(&dir, &target);
        let middle = body.len() / 2;
        body[middle] ^= 1;
        replace_event_payload(&dir, &target, &body);
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        assert!(!event_cursor_path(&dir, "codex").exists());
        assert!(event_row_exists(&dir, &target));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_proof_deltas_do_not_write_rotation_cursors() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-delta-rotation"));
        let dir = root.join("events");
        let events: Vec<Event> = (0..40)
            .map(|index| {
                let mut event = test_event();
                event.session = format!("session-{index:02}");
                event.call_id = format!("call-{index:02}");
                event
            })
            .collect();
        let keep: HashSet<String> = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());

        for (index, event) in events.iter().take(2).enumerate() {
            assert!(events_complete(&dir, &["codex"]).unwrap());
            let mut changed = event.clone();
            changed.output = format!("delta-{index}");
            write_events(&[changed], &dir, &keep, &["codex"], false, &HashSet::new()).unwrap();
            assert!(publish_events_complete(&dir, &["codex"]).unwrap());
            assert!(!event_cursor_path(&dir, "codex").exists());
        }
        assert!(events_complete(&dir, &["codex"]).unwrap());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_proof_rebinds_unchanged_agents_after_cross_agent_churn() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-agent-churn"));
        let dir = root.join("events");
        let mut events: Vec<Event> = (0..40)
            .map(|index| {
                let mut event = test_event();
                event.session = format!("session-{index:02}");
                event.call_id = format!("call-{index:02}");
                event
            })
            .collect();
        let mut claude = test_event();
        claude.agent = "claude";
        claude.session = "claude-session".into();
        claude.call_id = "claude-call".into();
        events.push(claude.clone());
        let keep: HashSet<String> = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(
            &events,
            &dir,
            &keep,
            &["codex", "claude"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex", "claude"]).unwrap());

        assert!(events_complete(&dir, &["codex"]).unwrap());
        claude.output = "churn-1".into();
        write_events(
            &[claude.clone()],
            &dir,
            &HashSet::from([event_fname("claude", "claude-session")]),
            &["claude"],
            false,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["claude"]).unwrap());
        assert!(events_complete(&dir, &["codex"]).unwrap());
        claude.output = "churn-2".into();
        write_events(
            &[claude],
            &dir,
            &HashSet::from([event_fname("claude", "claude-session")]),
            &["claude"],
            false,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["claude"]).unwrap());
        assert!(events_complete(&dir, &["codex"]).unwrap());
        assert!(!event_cursor_path(&dir, "codex").exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn cross_agent_generation_cannot_reseal_an_unsampled_corrupt_payload() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-cross-agent-tamper"));
        let dir = root.join("events");
        let mut events: Vec<Event> = (0..300)
            .map(|index| {
                let mut event = test_event();
                event.session = format!("codex-session-{index:03}");
                event.call_id = format!("codex-call-{index:03}");
                event
            })
            .collect();
        let mut claude = test_event();
        claude.agent = "claude";
        claude.session = "claude-session".into();
        claude.call_id = "claude-call".into();
        events.push(claude.clone());
        let keep: HashSet<String> = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(
            &events,
            &dir,
            &keep,
            &["codex", "claude"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(events_complete(&dir, &["codex", "claude"]).unwrap());

        let target = event_name_at(&dir, "codex", 299);
        let mut corrupt = stored_event_body(&dir, &target);
        let middle = corrupt.len() / 2;
        corrupt[middle] ^= 1;
        replace_event_payload(&dir, &target, &corrupt);

        let authority = event_publication_authority(&dir, &["claude"])
            .unwrap()
            .expect("the unchanged Claude scope remains independently provable");
        claude.output = "legitimate generation advance".into();
        write_events_recovering_authorized(
            std::slice::from_ref(&claude),
            &dir,
            &HashSet::from([event_fname("claude", "claude-session")]),
            &["claude"],
            false,
            &HashSet::new(),
            EventRecovery {
                recover_corrupt: false,
                authority: Some(&authority),
            },
        )
        .unwrap();
        assert!(events_complete(&dir, &["claude"]).unwrap());
        assert!(!publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(!event_proof_path(&dir, "codex").exists());
        assert!(event_row_exists(&dir, &target));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn verified_event_scan_validates_unsealed_rows_in_name_order() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-verified-unsealed"));
        let dir = root.join("events");
        let events: Vec<Event> = ["zeta", "alpha", "middle"]
            .into_iter()
            .map(|session| {
                let mut event = test_event();
                event.session = session.into();
                event.call_id = format!("call-{session}");
                event
            })
            .collect();
        let keep = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        invalidate_events_complete(&dir, &["codex"]).unwrap();
        let generation = fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap();
        let mut names = Vec::new();
        let summary = scan_verified_event_generation(&dir, &generation, &["codex"], |row| {
            names.push(row.name.to_string())
        })
        .unwrap();
        let mut sorted = names.clone();
        sorted.sort();
        assert_eq!(names, sorted);
        assert_eq!(summary.sessions, 3);
        assert_eq!(summary.events, 3);
        assert!(!summary.trusted_proofs);
        assert!(!event_proof_path(&dir, "codex").exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn all_agent_candidate_scan_keeps_primary_key_order_without_a_temp_sort() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-all-agent-plan"));
        let dir = root.join("events");
        let event = test_event();
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &HashSet::from([event_fname(event.agent, &event.session)]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        let connection = open_existing_event_store(&dir).unwrap().unwrap();
        let sql = format!(
            "EXPLAIN QUERY PLAN {}",
            verified_event_scan_sql(true, true, 1)
        );
        let mut statement = connection.prepare(&sql).unwrap();
        let details = statement
            .query_map([], |row| row.get::<_, String>(3))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert!(details.iter().all(|detail| !detail.contains("TEMP B-TREE")));
        assert!(details
            .iter()
            .any(|detail| detail.contains("event_sessions")));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn verified_event_scan_rejects_unsealed_payload_corruption() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-verified-corrupt"));
        let dir = root.join("events");
        let event = test_event();
        let name = event_fname(event.agent, &event.session);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &HashSet::from([name.clone()]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        invalidate_events_complete(&dir, &["codex"]).unwrap();
        let generation = fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap();
        let mut payload = stored_event_body(&dir, &name);
        let middle = payload.len() / 2;
        payload[middle] ^= 1;
        replace_event_payload(&dir, &name, &payload);
        let error =
            scan_verified_event_generation(&dir, &generation, &["codex"], |_| {}).unwrap_err();
        assert_eq!(error.kind, VerifiedEventScanFailure::Integrity);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn verified_event_scan_rejects_wrong_expected_generation_before_visiting() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-verified-generation"));
        let dir = root.join("events");
        let event = test_event();
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &HashSet::from([event_fname(event.agent, &event.session)]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        let mut visits = 0;
        let error = scan_verified_event_generation(&dir, b"wrong", &["codex"], |_| visits += 1)
            .unwrap_err();
        assert_eq!(error.kind, VerifiedEventScanFailure::GenerationMoved);
        assert_eq!(visits, 0);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn verified_event_scan_discards_visitor_work_after_a_proof_race() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-verified-proof-race"));
        let dir = root.join("events");
        let event = test_event();
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &HashSet::from([event_fname(event.agent, &event.session)]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(events_complete(&dir, &["codex"]).unwrap());
        let generation = fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap();
        let proof_path = event_proof_path(&dir, "codex");
        let mut visits = 0;
        let error = scan_verified_event_generation(&dir, &generation, &["codex"], |_| {
            visits += 1;
            if visits == 1 {
                write_bytes_atomic(&proof_path, b"{}").unwrap();
            }
        })
        .unwrap_err();
        assert_eq!(visits, 1);
        assert_eq!(error.kind, VerifiedEventScanFailure::GenerationMoved);
        fs::remove_dir_all(root).ok();
    }

    #[cfg(unix)]
    #[test]
    fn event_proof_symlink_is_removed_without_reading_its_target() {
        use std::os::unix::fs::symlink;

        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-symlink"));
        let dir = root.join("events");
        let event = test_event();
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &HashSet::from([event_fname(event.agent, &event.session)]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        let proof_path = event_proof_path(&dir, "codex");
        let outside = root.join("outside-proof.json");
        let outside_body = fs::read(&proof_path).unwrap();
        fs::write(&outside, &outside_body).unwrap();
        fs::remove_file(&proof_path).unwrap();
        symlink(&outside, &proof_path).unwrap();

        assert!(!events_complete(&dir, &["codex"]).unwrap());
        assert!(!proof_path.exists());
        assert_eq!(fs::read(outside).unwrap(), outside_body);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn single_agent_event_write_preserves_other_agents_store() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-scope-test"));
        let dir = root.join("events");
        let codex = test_event();
        let codex_name = event_fname("codex", "session-1");
        write_events(
            std::slice::from_ref(&codex),
            &dir,
            &HashSet::from([codex_name.clone()]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(!dir.join(".manifest").exists());

        let mut claude = test_event();
        claude.agent = "claude";
        claude.session = "claude-session".into();
        claude.call_id = "claude-call".into();
        let claude_name = event_fname("claude", "claude-session");
        write_events(
            std::slice::from_ref(&claude),
            &dir,
            &HashSet::from([claude_name.clone()]),
            &["claude"],
            true,
            &HashSet::new(),
        )
        .unwrap();

        assert!(!dir.join(".manifest").exists());
        assert!(event_row_exists(&dir, &codex_name));
        assert!(event_row_exists(&dir, &claude_name));
        assert!(publish_events_complete(&dir, &["codex", "claude"]).unwrap());
        assert!(events_complete(&dir, &["codex", "claude"]).unwrap());

        // A later single-agent write moves the shared database generation. Publishing
        // Codex must not force a Claude source repair when `all` is selected next: one batched
        // exact scan should refresh Claude's unchanged scoped proof.
        let mut changed_codex = codex;
        changed_codex.call_id = "codex-call-2".into();
        write_events(
            std::slice::from_ref(&changed_codex),
            &dir,
            &HashSet::from([event_fname("codex", "session-1")]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(events_complete(&dir, &["codex", "claude"]).unwrap());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn incremental_event_prune_removes_only_named_scoped_ownership() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-targeted-prune-test"));
        let dir = root.join("events");
        let mut codex_a = test_event();
        codex_a.session = "codex-a".into();
        codex_a.call_id = "codex-a-call".into();
        let mut codex_b = test_event();
        codex_b.session = "codex-b".into();
        codex_b.call_id = "codex-b-call".into();
        let mut claude = test_event();
        claude.agent = "claude";
        claude.session = "claude-a".into();
        claude.call_id = "claude-a-call".into();
        let codex_a_name = event_fname("codex", "codex-a");
        let codex_b_name = event_fname("codex", "codex-b");
        let claude_name = event_fname("claude", "claude-a");
        let keep = HashSet::from([
            codex_a_name.clone(),
            codex_b_name.clone(),
            claude_name.clone(),
        ]);
        write_events(
            &[codex_a, codex_b, claude],
            &dir,
            &keep,
            &["codex", "claude"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        let generation = fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap();

        // An incremental Codex pass has no event payload for unchanged sessions. Even with an
        // intentionally empty keep-set, only the exact touched/dead ownership may be removed.
        write_events(
            &[],
            &dir,
            &HashSet::new(),
            &["codex"],
            false,
            &HashSet::from([codex_b_name.clone()]),
        )
        .unwrap();
        assert!(event_row_exists(&dir, &codex_a_name));
        assert!(!event_row_exists(&dir, &codex_b_name));
        assert!(event_row_exists(&dir, &claude_name));
        assert_ne!(
            fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap(),
            generation
        );

        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn missing_event_store_is_rebuilt_from_a_complete_generation() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-missing-store"));
        let dir = root.join("events");
        let event = test_event();
        let name = event_fname(event.agent, &event.session);
        let keep = HashSet::from([name.clone()]);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        fs::remove_file(event_store_path(&dir)).unwrap();
        remove_if_exists(&dir.join(format!("{EVENT_STORE_NAME}-wal"))).unwrap();
        remove_if_exists(&dir.join(format!("{EVENT_STORE_NAME}-shm"))).unwrap();
        assert!(!events_complete(&dir, &["codex"]).unwrap());

        write_events(&[event], &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(event_row_exists(&dir, &name));
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(events_complete(&dir, &["codex"]).unwrap());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn legacy_event_generation_requires_complete_all_agent_migration() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-legacy-seed"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let legacy_name = event_fname("claude", "legacy-session");
        let legacy_body = b"{\"ts\":1,\"kind\":\"tool\",\"name\":\"legacy\"}\n";
        fs::write(dir.join(&legacy_name), legacy_body).unwrap();
        write_bytes_atomic(&dir.join(".manifest"), b"not authoritative").unwrap();

        let event = test_event();
        let codex_name = event_fname(event.agent, &event.session);
        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &HashSet::from([codex_name.clone()]),
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("complete migration"));
        assert!(!event_store_path(&dir).exists());
        assert_eq!(fs::read(dir.join(&legacy_name)).unwrap(), legacy_body);

        let mut claude = test_event();
        claude.agent = "claude";
        claude.session = "legacy-session".into();
        let both = [event, claude];
        let keep = both
            .iter()
            .map(|row| event_fname(row.agent, &row.session))
            .collect();
        write_events_recovering(
            &both,
            &dir,
            &keep,
            &["codex", "claude"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        assert!(event_row_exists(&dir, &codex_name));
        assert!(event_row_exists(&dir, &legacy_name));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn version_one_store_migrates_digests_stats_and_aggregates() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-v1-migration"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let name = event_fname("codex", "legacy-session");
        let payload = b"{\"ts\":1,\"kind\":\"tool\",\"name\":\"legacy\"}\n";
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE event_sessions (
                   name TEXT PRIMARY KEY, agent TEXT NOT NULL, session TEXT NOT NULL,
                   hash INTEGER NOT NULL, n_events INTEGER NOT NULL, payload BLOB NOT NULL
                 ) WITHOUT ROWID;
                 CREATE TABLE event_meta (
                   key TEXT PRIMARY KEY, value BLOB NOT NULL
                 ) WITHOUT ROWID;
                 PRAGMA user_version=1;",
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO event_sessions VALUES(?1,'codex','legacy-session',?2,1,?3)",
                params![name, content_hash(payload) as i64, payload],
            )
            .unwrap();
        drop(connection);
        let stats = EventFileStats {
            agent: "codex".into(),
            calls: 1,
            ..EventFileStats::default()
        };
        write_bytes_atomic(
            &dir.join(EVENT_STATS_MANIFEST),
            &serde_json::to_vec(&BTreeMap::from([(name.clone(), stats)])).unwrap(),
        )
        .unwrap();

        let connection = open_event_store(&dir).unwrap();
        assert_eq!(
            connection
                .query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            EVENT_STORE_VERSION
        );
        let (digest, stats): (Vec<u8>, Vec<u8>) = connection
            .query_row(
                "SELECT digest,stats FROM event_sessions WHERE name=?1",
                [&name],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(digest, event_digest(payload));
        assert_eq!(
            serde_json::from_slice::<EventFileStats>(&stats)
                .unwrap()
                .calls,
            1
        );
        assert_eq!(event_inventory(&connection, "codex").unwrap().count, 1);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn event_proof_size_is_independent_of_session_count() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-size"));
        let dir = root.join("events");
        let events: Vec<Event> = (0..256)
            .map(|index| {
                let mut event = test_event();
                event.session = format!("session-{index:03}");
                event.call_id = format!("call-{index:03}");
                event
            })
            .collect();
        let keep = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        let proof = fs::read(event_proof_path(&dir, "codex")).unwrap();
        let decoded: EventProof = serde_json::from_slice(&proof).unwrap();
        assert_eq!(decoded.canaries.len(), 256);
        assert!(proof.len() < 64 * 1_024);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn exact_reproof_rejects_corruption_outside_the_canary_sample() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-proof-unsampled"));
        let dir = root.join("events");
        let events: Vec<Event> = (0..300)
            .map(|index| {
                let mut event = test_event();
                event.session = format!("session-{index:03}");
                event.call_id = format!("call-{index:03}");
                event
            })
            .collect();
        let keep = events
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        write_events(&events, &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        let proof: EventProof =
            serde_json::from_slice(&fs::read(event_proof_path(&dir, "codex")).unwrap()).unwrap();
        let sampled: HashSet<&str> = proof
            .canaries
            .iter()
            .map(|canary| canary.name.as_str())
            .collect();
        let target = (0..300)
            .map(|index| event_fname("codex", &format!("session-{index:03}")))
            .find(|name| !sampled.contains(name.as_str()))
            .unwrap();
        replace_event_payload(&dir, &target, b"corrupt");
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        fs::remove_dir_all(root).ok();
    }

    #[cfg(unix)]
    #[test]
    fn event_store_symlink_is_never_opened() {
        use std::os::unix::fs::symlink;

        let root = tmp_path(&std::env::temp_dir().join("agrep-event-store-symlink"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let outside = root.join("outside.sqlite3");
        Connection::open(&outside).unwrap();
        symlink(&outside, event_store_path(&dir)).unwrap();
        let error = write_events(
            &[test_event()],
            &dir,
            &HashSet::from([event_fname("codex", "session-1")]),
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap_err();
        assert!(error.to_string().contains("not a regular file"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn corrupt_store_rebuild_requires_complete_all_agent_proof() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-corrupt-rebuild"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let store = event_store_path(&dir);
        fs::write(&store, b"not a sqlite database").unwrap();
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);

        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("agrep index --full"));
        assert_eq!(fs::read(&store).unwrap(), b"not a sqlite database");

        write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        assert!(event_row_exists(&dir, keep.iter().next().unwrap()));
        assert!(dir.join(EVENT_GENERATION_NAME).exists());
        assert!(!dir.join("event_stats.json").exists());
        assert!(!fs::read_dir(&dir)
            .unwrap()
            .filter_map(Result::ok)
            .any(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".store.sqlite3.corrupt.")
            }));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn logical_event_row_corruption_rebuilds_only_when_complete() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-row-corruption"));
        let dir = root.join("events");
        let event = test_event();
        let name = event_fname(event.agent, &event.session);
        let keep = HashSet::from([name.clone()]);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        let expected = stored_event_body(&dir, &name);
        let corruptions = [
            "stats=X'00'",
            "stats=X'7b7d'",
            "hash='wrong'",
            "n_events='wrong'",
            "n_events=-1",
            "digest=123",
            "payload=123",
            "agent='other'",
            "session=123",
        ];
        for (index, corruption) in corruptions.into_iter().enumerate() {
            let connection = Connection::open(event_store_path(&dir)).unwrap();
            connection
                .execute(
                    &format!("UPDATE event_sessions SET {corruption} WHERE name=?1"),
                    [&name],
                )
                .unwrap();
            drop(connection);
            if index == 0 {
                let error = write_events_recovering(
                    std::slice::from_ref(&event),
                    &dir,
                    &keep,
                    &["codex"],
                    true,
                    &HashSet::new(),
                    false,
                )
                .unwrap_err();
                assert!(error.to_string().contains("agrep index --full"));
            }
            write_events_recovering(
                std::slice::from_ref(&event),
                &dir,
                &keep,
                &["codex"],
                true,
                &HashSet::new(),
                true,
            )
            .unwrap();
            assert_eq!(stored_event_body(&dir, &name), expected);
        }
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn transient_sqlite_failures_are_not_classified_as_corruption() {
        for code in [
            rusqlite::ffi::SQLITE_BUSY,
            rusqlite::ffi::SQLITE_LOCKED,
            rusqlite::ffi::SQLITE_IOERR,
        ] {
            let error = anyhow::Error::new(rusqlite::Error::SqliteFailure(
                rusqlite::ffi::Error::new(code),
                None,
            ));
            assert!(!event_store_is_corrupt(&error));
        }
    }

    #[test]
    fn malformed_version_one_schema_rebuilds_only_when_complete() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-v1-malformed"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let store = event_store_path(&dir);
        let connection = Connection::open(&store).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE event_sessions(name TEXT PRIMARY KEY) WITHOUT ROWID;
                 CREATE TABLE event_meta(
                   key TEXT PRIMARY KEY, value BLOB NOT NULL
                 ) WITHOUT ROWID;
                 PRAGMA user_version=1;",
            )
            .unwrap();
        drop(connection);
        let before = fs::read(&store).unwrap();
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);

        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("agrep index --full"));
        assert_eq!(fs::read(&store).unwrap(), before);
        write_events_recovering(
            &[event],
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        assert!(event_row_exists(&dir, keep.iter().next().unwrap()));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn legacy_optional_table_names_are_case_insensitively_validated() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE event_sessions (
                   name TEXT PRIMARY KEY,
                   agent TEXT NOT NULL,
                   session TEXT NOT NULL,
                   hash INTEGER NOT NULL,
                   n_events INTEGER NOT NULL,
                   payload BLOB NOT NULL
                 ) WITHOUT ROWID;
                 CREATE TABLE event_meta (
                   key TEXT PRIMARY KEY, value BLOB NOT NULL
                 ) WITHOUT ROWID;
                 CREATE TABLE \"EVENT_AGENT_STATE\" (
                   agent TEXT PRIMARY KEY
                 ) WITHOUT ROWID;
                 PRAGMA user_version=1;",
            )
            .unwrap();
        let error = validate_event_store_migration_schema(&connection, 1).unwrap_err();
        assert!(error.to_string().contains("missing columns"));
    }

    #[test]
    fn current_schema_without_primary_keys_rebuilds_only_when_complete() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-v2-no-primary-key"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE event_sessions(
                   name TEXT, agent TEXT, session TEXT, hash INTEGER, n_events INTEGER,
                   payload BLOB, digest BLOB, stats BLOB
                 );
                 CREATE TABLE event_meta(key TEXT, value BLOB);
                 CREATE TABLE event_agent_state(
                   agent TEXT, row_count INTEGER, root_a INTEGER, root_b INTEGER,
                   calls INTEGER, fails INTEGER, known INTEGER, subagents INTEGER
                 );
                 CREATE TABLE event_tool_stats(
                   agent TEXT, name TEXT, n INTEGER, fails INTEGER
                 );
                 PRAGMA user_version=2;",
            )
            .unwrap();
        drop(connection);
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);
        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("agrep index --full"));
        write_events_recovering(
            &[event],
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        assert!(event_row_exists(&dir, keep.iter().next().unwrap()));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn unexpected_trigger_and_check_rebuild_only_when_complete() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-schema-constraints"));
        let dir = root.join("events");
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "CREATE TRIGGER sabotage BEFORE INSERT ON \"EVENT_AGENT_STATE\"
                 BEGIN SELECT RAISE(ABORT,'blocked'); END;",
            )
            .unwrap();
        drop(connection);
        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("agrep index --full"));
        write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        let triggers: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type='trigger'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(triggers, 0);
        connection
            .execute_batch(
                "BEGIN;
                 ALTER TABLE event_agent_state RENAME TO event_agent_state_old;
                 CREATE TABLE event_agent_state (
                   agent TEXT PRIMARY KEY,
                   row_count INTEGER NOT NULL,
                   root_a INTEGER NOT NULL,
                   root_b INTEGER NOT NULL,
                   calls INTEGER NOT NULL CHECK(calls >= 0),
                   fails INTEGER NOT NULL,
                   known INTEGER NOT NULL,
                   subagents INTEGER NOT NULL
                 ) WITHOUT ROWID;
                 INSERT INTO event_agent_state SELECT * FROM event_agent_state_old;
                 DROP TABLE event_agent_state_old;
                 COMMIT;",
            )
            .unwrap();
        drop(connection);
        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("agrep index --full"));
        write_events_recovering(
            &[event],
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        let schema: String = connection
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE name='event_agent_state'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(!schema.to_ascii_uppercase().contains("CHECK"));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn schema_metadata_bypasses_rebuild_only_when_complete() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-schema-metadata"));
        let dir = root.join("events");
        let event = test_event();
        let name = event_fname(event.agent, &event.session);
        let keep = HashSet::from([name]);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();

        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "BEGIN;
                 ALTER TABLE event_agent_state RENAME TO event_agent_state_old;
                 CREATE TABLE event_agent_state (
                   agent TEXT PRIMARY KEY,
                   row_count INTEGER NOT NULL,
                   root_a INTEGER NOT NULL,
                   root_b INTEGER NOT NULL,
                   calls INTEGER NOT NULL,
                   fails INTEGER NOT NULL,
                   known INTEGER NOT NULL,
                   subagents INTEGER NOT NULL /* WITHOUT ROWID */
                 );
                 INSERT INTO event_agent_state SELECT * FROM event_agent_state_old;
                 DROP TABLE event_agent_state_old;
                 COMMIT;",
            )
            .unwrap();
        let definition: String = connection
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE name='event_agent_state'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(definition.contains("/* WITHOUT ROWID */"));
        drop(connection);
        assert_complete_event_rebuild(std::slice::from_ref(&event), &dir, &keep);

        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "BEGIN;
                 ALTER TABLE event_agent_state RENAME TO event_agent_state_old;
                 CREATE TABLE event_agent_state (
                   agent TEXT PRIMARY KEY,
                   row_count INTEGER NOT NULL,
                   root_a INTEGER NOT NULL,
                   root_b INTEGER NOT NULL,
                   calls INTEGER GENERATED ALWAYS AS (0) STORED,
                   fails INTEGER NOT NULL,
                   known INTEGER NOT NULL,
                   subagents INTEGER NOT NULL
                 ) WITHOUT ROWID;
                 INSERT INTO event_agent_state(
                   agent,row_count,root_a,root_b,fails,known,subagents
                 ) SELECT agent,row_count,root_a,root_b,fails,known,subagents
                   FROM event_agent_state_old;
                 DROP TABLE event_agent_state_old;
                 COMMIT;",
            )
            .unwrap();
        drop(connection);
        assert_complete_event_rebuild(std::slice::from_ref(&event), &dir, &keep);

        let mut second = event.clone();
        second.session = "session-2".into();
        second.call_id = "call-2".into();
        let two_sessions = [event.clone(), second];
        let two_keep = two_sessions
            .iter()
            .map(|event| event_fname(event.agent, &event.session))
            .collect();
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "DROP INDEX event_sessions_agent;
                 CREATE UNIQUE INDEX \"EVENT_SESSIONS_AGENT\" ON event_sessions(agent);",
            )
            .unwrap();
        drop(connection);
        assert_complete_event_rebuild(&two_sessions, &dir, &two_keep);

        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "BEGIN;
                 ALTER TABLE event_tool_stats RENAME TO event_tool_stats_old;
                 CREATE TABLE event_tool_stats (
                   agent TEXT NOT NULL,
                   name TEXT NOT NULL,
                   n INTEGER NOT NULL,
                   fails INTEGER NOT NULL,
                   PRIMARY KEY(agent,name),
                   FOREIGN KEY(agent) REFERENCES event_agent_state(agent)
                 ) WITHOUT ROWID;
                 INSERT INTO event_tool_stats SELECT * FROM event_tool_stats_old;
                 DROP TABLE event_tool_stats_old;
                 COMMIT;",
            )
            .unwrap();
        drop(connection);
        assert_complete_event_rebuild(&two_sessions, &dir, &two_keep);

        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "DROP INDEX event_sessions_agent;
                 CREATE VIEW \"EVENT_SESSIONS_AGENT\" AS SELECT agent FROM event_sessions;",
            )
            .unwrap();
        drop(connection);
        assert_complete_event_rebuild(&two_sessions, &dir, &two_keep);

        let mut upper = event.clone();
        upper.name = "SHELL".into();
        upper.call_id = "call-upper".into();
        upper.ts += 1;
        let colliding_tools = [event, upper];
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "BEGIN;
                 ALTER TABLE event_tool_stats RENAME TO event_tool_stats_old;
                 CREATE TABLE event_tool_stats (
                   agent TEXT NOT NULL,
                   name TEXT NOT NULL COLLATE NOCASE,
                   n INTEGER NOT NULL,
                   fails INTEGER NOT NULL,
                   PRIMARY KEY(agent,name)
                 ) WITHOUT ROWID;
                 INSERT INTO event_tool_stats SELECT * FROM event_tool_stats_old;
                 DROP TABLE event_tool_stats_old;
                 COMMIT;",
            )
            .unwrap();
        drop(connection);
        assert_complete_event_rebuild(&colliding_tools, &dir, &keep);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn current_version_malformed_schema_is_rebuilt_only_when_complete() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-schema-rebuild"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let connection = Connection::open(event_store_path(&dir)).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE event_sessions(name TEXT PRIMARY KEY); PRAGMA user_version=2;",
            )
            .unwrap();
        drop(connection);
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);
        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("agrep index --full"));
        write_events_recovering(
            &[event],
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        assert!(event_row_exists(&dir, keep.iter().next().unwrap()));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn future_event_store_version_is_rejected_without_mutation() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-future-version"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        let store = event_store_path(&dir);
        let connection = Connection::open(&store).unwrap();
        connection
            .execute_batch("CREATE TABLE future_only(value TEXT); PRAGMA user_version=3;")
            .unwrap();
        drop(connection);
        let before = fs::read(&store).unwrap();

        let error = open_event_store(&dir).unwrap_err();
        assert!(error
            .to_string()
            .contains("unsupported event store version 3"));
        assert_eq!(fs::read(&store).unwrap(), before);
        assert!(!dir.join(format!("{EVENT_STORE_NAME}-wal")).exists());
        assert!(!dir.join(format!("{EVENT_STORE_NAME}-shm")).exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn missing_published_store_cannot_be_rebuilt_by_one_agent() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-missing-scoped"));
        let dir = root.join("events");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(EVENT_GENERATION_NAME), b"prior").unwrap();
        fs::write(dir.join("other-agent.jsonl"), b"legacy\n").unwrap();
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);

        let error = write_events_recovering(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            false,
        )
        .unwrap_err();
        assert!(error.to_string().contains("complete migration"));
        assert!(!event_store_path(&dir).exists());
        write_events_recovering(
            &[event],
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
            true,
        )
        .unwrap();
        assert!(event_row_exists(&dir, keep.iter().next().unwrap()));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn failed_rebuild_publish_restores_or_retains_prior_store() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-publish-rollback"));
        let dir = root.join("events");
        let rebuilt = root.join("rebuilt");
        fs::create_dir_all(&dir).unwrap();
        fs::create_dir_all(&rebuilt).unwrap();
        for suffix in ["", "-wal", "-shm", "-journal"] {
            fs::write(
                dir.join(format!("{EVENT_STORE_NAME}{suffix}")),
                suffix.as_bytes(),
            )
            .unwrap();
        }
        let error = publish_rebuilt_event_store(&dir, &rebuilt).unwrap_err();
        assert!(error.to_string().contains("prior store restored"));
        for suffix in ["", "-wal", "-shm", "-journal"] {
            assert_eq!(
                fs::read(dir.join(format!("{EVENT_STORE_NAME}{suffix}"))).unwrap(),
                suffix.as_bytes()
            );
        }
        fs::remove_dir_all(root).ok();
    }

    #[cfg(windows)]
    #[test]
    fn held_windows_reader_leaves_published_store_and_generation_untouched() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-held-reader"));
        let dir = root.join("events");
        let rebuilt = root.join("rebuilt");
        fs::create_dir_all(&dir).unwrap();
        fs::create_dir_all(&rebuilt).unwrap();
        let store = event_store_path(&dir);
        let connection = Connection::open(&store).unwrap();
        connection
            .execute_batch("CREATE TABLE prior(value TEXT); PRAGMA user_version=2;")
            .unwrap();
        drop(connection);
        let generation = b"prior-generation";
        fs::write(dir.join(EVENT_GENERATION_NAME), generation).unwrap();
        let before = fs::read(&store).unwrap();
        let held = Connection::open_with_flags(&store, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
        held.query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
            .unwrap();
        fs::write(event_store_path(&rebuilt), b"rebuilt").unwrap();

        let error = publish_rebuilt_event_store_with(&dir, &rebuilt, |source, destination| {
            replace_existing(source, destination).map_err(anyhow::Error::from)
        })
        .unwrap_err();
        assert!(error
            .to_string()
            .contains("cannot quarantine corrupt event store"));
        assert!(error.to_string().contains(&store.display().to_string()));
        assert_eq!(fs::read(&store).unwrap(), before);
        assert_eq!(
            fs::read(dir.join(EVENT_GENERATION_NAME)).unwrap(),
            generation
        );
        assert!(!fs::read_dir(&dir)
            .unwrap()
            .filter_map(Result::ok)
            .any(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .contains(".store.sqlite3.corrupt.")
            }));
        drop(held);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn post_swap_failure_keeps_the_prior_store_quarantined() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-post-swap-failure"));
        let dir = root.join("events");
        let rebuilt = root.join("rebuilt");
        fs::create_dir_all(&dir).unwrap();
        fs::write(event_store_path(&dir), b"prior corrupt store").unwrap();
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);
        write_events(&[event], &rebuilt, &keep, &["codex"], true, &HashSet::new()).unwrap();
        seal_rebuilt_event_store(&rebuilt).unwrap();
        let generation = fs::read(rebuilt.join(EVENT_GENERATION_NAME)).unwrap();
        let quarantines = publish_rebuilt_event_store(&dir, &rebuilt).unwrap();
        let bad_stats_path = root.join("stats-is-a-directory");
        fs::create_dir(&bad_stats_path).unwrap();

        assert!(finalize_rebuilt_event_store(&dir, &generation, &bad_stats_path).is_err());
        assert!(event_store_path(&dir).exists());
        assert!(quarantines.iter().all(|(_, path)| path.exists()));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn aggregate_stats_corruption_invalidates_the_proof() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-aggregate-proof"));
        let dir = root.join("events");
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        let connection = open_event_store(&dir).unwrap();
        connection
            .execute(
                "UPDATE event_agent_state SET calls=999 WHERE agent='codex'",
                [],
            )
            .unwrap();
        connection
            .execute("UPDATE event_tool_stats SET n=999 WHERE agent='codex'", [])
            .unwrap();
        drop(connection);
        assert!(!events_complete(&dir, &["codex"]).unwrap());
        write_events(&[event], &dir, &keep, &["codex"], true, &HashSet::new()).unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        assert!(events_complete(&dir, &["codex"]).unwrap());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn aggregate_row_type_corruption_reaches_complete_repair() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-event-aggregate-types"));
        let dir = root.join("events");
        let event = test_event();
        let keep = HashSet::from([event_fname(event.agent, &event.session)]);
        write_events(
            std::slice::from_ref(&event),
            &dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(publish_events_complete(&dir, &["codex"]).unwrap());
        for sql in [
            "UPDATE event_agent_state SET calls='wrong' WHERE agent='codex'",
            "UPDATE event_tool_stats SET n='wrong' WHERE agent='codex'",
            "UPDATE event_tool_stats SET name=CAST(X'80' AS TEXT) WHERE agent='codex'",
        ] {
            let connection = Connection::open(event_store_path(&dir)).unwrap();
            connection.execute(sql, []).unwrap();
            drop(connection);
            assert!(!events_complete(&dir, &["codex"]).unwrap());
            write_events_recovering(
                std::slice::from_ref(&event),
                &dir,
                &keep,
                &["codex"],
                true,
                &HashSet::new(),
                true,
            )
            .unwrap();
            assert!(publish_events_complete(&dir, &["codex"]).unwrap());
            assert!(events_complete(&dir, &["codex"]).unwrap());
        }
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn writes_one_compact_line_per_message() {
        let msgs = vec![
            Message {
                agent: "claude",
                project: "myproj".into(),
                session: "sess-1".into(),
                ts: 1_700_000_000_000,
                turn: 0,
                text: "first".into(),
                who: "user".into(),
                model: "claude-opus-4-8".into(),
                model_source: "explicit".into(),
                reply: "an answer".into(),
                reply_chars: 9,
                side: false,
                parent: "".into(),
            },
            Message {
                agent: "opencode",
                project: "myproj".into(),
                session: "sess-2".into(),
                ts: 0,
                turn: 7,
                text: "with \"quotes\" and \n newline".into(),
                who: "user".into(),
                model: "".into(),
                model_source: "unknown".into(),
                reply: "excerpt…".into(),
                reply_chars: crate::ingest::REPLY_CAP + 7,
                side: false,
                parent: "".into(),
            },
        ];
        let mut path = std::env::temp_dir();
        path.push(format!("agrep-cache-test-{}.jsonl", std::process::id()));
        write_messages(&msgs, &path).unwrap();
        write_messages(&msgs, &path).unwrap();

        let data = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = data.lines().collect();
        assert_eq!(lines.len(), 2);

        let v0: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(v0["id"], "claude:sess-1:0");
        assert_eq!(v0["agent"], "claude");
        assert_eq!(v0["project"], "myproj");
        assert_eq!(v0["session"], "sess-1");
        assert_eq!(v0["ts"], 1_700_000_000_000i64);
        assert_eq!(v0["turn"], 0);
        assert_eq!(v0["text"], "first");
        assert_eq!(v0["content_digest"], "1d41");
        assert_eq!(v0["who"], "user");
        assert_eq!(v0["model_source"], "explicit");

        let v1: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(v1["id"], "opencode:sess-2:7");
        assert_eq!(v1["text"], "with \"quotes\" and \n newline");
        assert_eq!(v1["who"], "user");
        assert_eq!(v1["model_source"], "unknown");

        let reply_path = path.with_extension("replies.jsonl");
        write_replies(&msgs, &reply_path).unwrap();
        let replies = std::fs::read_to_string(&reply_path).unwrap();
        let replies: Vec<serde_json::Value> = replies
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(replies[0]["reply"], "an answer");
        assert_eq!(replies[0]["content_digest"], "dd24");
        assert_eq!(replies[0]["reply_chars"], 9);
        assert_eq!(replies[0]["reply_truncated"], false);
        assert_eq!(replies[1]["reply_chars"], crate::ingest::REPLY_CAP + 7);
        assert_eq!(replies[1]["reply_truncated"], true);

        std::fs::remove_file(&path).ok();
        std::fs::remove_file(&reply_path).ok();
    }

    #[test]
    fn content_digest_matches_the_saved_handle_contract() {
        assert_eq!(content_digest("hello world"), "d2e7");
        assert_eq!(content_digest(""), "2325");
        assert_eq!(content_digest("café 🧡"), "3072");
    }

    #[test]
    fn session_family_proof_tracks_only_the_parent_census() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-family-meta"));
        fs::create_dir_all(&root).unwrap();
        let sessions = root.join("sessions.jsonl");
        let meta = root.join(SESSION_FAMILY_META_FILE);
        let mut messages = vec![Message {
            agent: "codex",
            project: "project".into(),
            session: "child".into(),
            ts: 1,
            turn: 1,
            text: "first".into(),
            who: "user".into(),
            model: "".into(),
            model_source: "unknown".into(),
            reply: "".into(),
            reply_chars: 0,
            side: true,
            parent: "root".into(),
        }];
        write_session_index(&messages, &sessions, &meta, "1:first").unwrap();
        let first: serde_json::Value = serde_json::from_slice(&fs::read(&meta).unwrap()).unwrap();
        assert_eq!(
            first["digest"],
            "c1c7707327949139ce23522fdb772b08d9e8ff0345f42200"
        );
        assert_eq!(first["algorithm"], SESSION_FAMILY_DIGEST_ALGORITHM);

        messages[0].text = "different prose".into();
        messages[0].project = "different project".into();
        write_session_index(&messages, &sessions, &meta, "1:second").unwrap();
        let second: serde_json::Value = serde_json::from_slice(&fs::read(&meta).unwrap()).unwrap();
        assert_eq!(first["digest"], second["digest"]);
        assert_eq!(second["ingest_signature"], "1:second");

        messages[0].parent = "other-root".into();
        write_session_index(&messages, &sessions, &meta, "1:third").unwrap();
        let third: serde_json::Value = serde_json::from_slice(&fs::read(&meta).unwrap()).unwrap();
        assert_ne!(second["digest"], third["digest"]);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn session_family_meta_precedes_the_session_file() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-family-order"));
        fs::create_dir_all(&root).unwrap();
        let blocked = root.join("blocked");
        fs::write(&blocked, b"not a directory").unwrap();
        let sessions = blocked.join("sessions.jsonl");
        let meta = root.join(SESSION_FAMILY_META_FILE);
        let messages = vec![Message {
            agent: "codex",
            project: "project".into(),
            session: "session".into(),
            ts: 1,
            turn: 1,
            text: "text".into(),
            who: "user".into(),
            model: "".into(),
            model_source: "unknown".into(),
            reply: "".into(),
            reply_chars: 0,
            side: false,
            parent: "".into(),
        }];
        assert!(write_session_index(&messages, &sessions, &meta, "1:order").is_err());
        let published: serde_json::Value =
            serde_json::from_slice(&fs::read(&meta).unwrap()).unwrap();
        assert_eq!(published["ingest_signature"], "1:order");
        assert!(!sessions.exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn session_parent_uses_the_first_non_empty_value_in_any_order() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-family-parent-order"));
        fs::create_dir_all(&root).unwrap();
        let message = |parent: &str| Message {
            agent: "codex",
            project: "project".into(),
            session: "child".into(),
            ts: 1,
            turn: 1,
            text: "same".into(),
            who: "user".into(),
            model: "".into(),
            model_source: "unknown".into(),
            reply: "".into(),
            reply_chars: 0,
            side: true,
            parent: parent.into(),
        };
        let first_sessions = root.join("first.jsonl");
        let first_meta = root.join("first.meta.json");
        let first = vec![message(""), message("root")];
        write_session_index(&first, &first_sessions, &first_meta, "2:first").unwrap();

        let second_sessions = root.join("second.jsonl");
        let second_meta = root.join("second.meta.json");
        let second = vec![message("root"), message("")];
        write_session_index(&second, &second_sessions, &second_meta, "2:second").unwrap();

        let first_row: serde_json::Value =
            serde_json::from_slice(&fs::read(&first_sessions).unwrap()).unwrap();
        let second_row: serde_json::Value =
            serde_json::from_slice(&fs::read(&second_sessions).unwrap()).unwrap();
        let first_proof: serde_json::Value =
            serde_json::from_slice(&fs::read(&first_meta).unwrap()).unwrap();
        let second_proof: serde_json::Value =
            serde_json::from_slice(&fs::read(&second_meta).unwrap()).unwrap();
        assert_eq!(first_row["parent"], "root");
        assert_eq!(second_row["parent"], "root");
        assert_eq!(first_proof["digest"], second_proof["digest"]);
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn session_parent_conflicts_abort_before_publication() {
        let root = tmp_path(&std::env::temp_dir().join("agrep-family-parent-conflict"));
        fs::create_dir_all(&root).unwrap();
        let message = |parent: &str| Message {
            agent: "codex",
            project: "project".into(),
            session: "child".into(),
            ts: 1,
            turn: 1,
            text: "same".into(),
            who: "user".into(),
            model: "".into(),
            model_source: "unknown".into(),
            reply: "".into(),
            reply_chars: 0,
            side: true,
            parent: parent.into(),
        };
        let sessions = root.join("sessions.jsonl");
        let meta = root.join(SESSION_FAMILY_META_FILE);
        let messages = vec![message("root-a\u{1b}[31m"), message("root-b")];
        let error = write_session_index(&messages, &sessions, &meta, "2:conflict")
            .unwrap_err()
            .to_string();
        assert!(error.contains("conflicting parents"));
        assert!(error.contains(r"\u001b"));
        assert!(!error.contains('\u{1b}'));
        assert!(!sessions.exists());
        assert!(!meta.exists());
        fs::remove_dir_all(root).ok();
    }
}
