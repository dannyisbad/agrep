//! Intake accounting: every record an adapter sees lands in exactly one counted
//! bucket - emitted, consumed (agent-side), a named skip, or a parse error. The
//! silent `continue` is banned; `agrep audit` (python) re-censuses the raw stores
//! and holds each file to the identity `seen == rows + agent_rows + Σskips + errors`,
//! making ingest coverage mechanically auditable.
//!
//! Tallies are per file (or per conversation for token-fingerprinted sqlite stores),
//! lock-free on the hot path (atomics behind an Arc), and persisted to
//! `data/intake_stats.json` keyed by a freshness fingerprint - so files served from
//! the parse cache keep the tally from the run that actually parsed them, and the
//! accounting stays complete across incremental runs.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde::{Deserialize, Serialize};

/// Why a seen record did not become an indexed row. Adding a category is cheap;
/// renaming one invalidates saved tallies only cosmetically (audit treats unknown
/// names as explained skips of that name).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Skip {
    /// Command/system wrapper markup, not something a human typed (`is_wrapper`).
    Wrapper,
    /// Store metadata / bookkeeping records (isMeta, session headers, state rows).
    Meta,
    /// Duplicated or internal sidechain content deliberately not indexed (for
    /// example Claude's inline copy of a separately stored child, or Codex's
    /// automatic guardian approval-review rollout).
    Sidechain,
    /// Structural lines that carry no message at all (progress, summaries,
    /// file-history snapshots, non-message record types).
    NonMessage,
    /// A message record that is not a human turn and not consumed as agent-side
    /// (system role, non-external userType, tool plumbing in message form).
    NonHuman,
    /// A human-shaped record whose extracted text was empty.
    EmptyText,
    /// Replayed/duplicated content this parse intentionally drops (e.g. a forked
    /// codex child's inherited parent transcript before the handoff boundary).
    Replay,
    /// A record no live conversation reaches: a SQLite transcript row under no
    /// header, or an entry on a session-tree branch the user abandoned.
    Unreferenced,
    /// A record in a structurally identified ephemeral worker transcript. These
    /// files remain discovered and fingerprinted; only their rows are deliberately
    /// omitted from the searchable corpus.
    Throwaway,
}

impl Skip {
    pub const ALL: [Skip; 9] = [
        Skip::Wrapper,
        Skip::Meta,
        Skip::Sidechain,
        Skip::NonMessage,
        Skip::NonHuman,
        Skip::EmptyText,
        Skip::Replay,
        Skip::Unreferenced,
        Skip::Throwaway,
    ];
    const fn index(self) -> usize {
        match self {
            Skip::Wrapper => 0,
            Skip::Meta => 1,
            Skip::Sidechain => 2,
            Skip::NonMessage => 3,
            Skip::NonHuman => 4,
            Skip::EmptyText => 5,
            Skip::Replay => 6,
            Skip::Unreferenced => 7,
            Skip::Throwaway => 8,
        }
    }
    pub fn key(self) -> &'static str {
        match self {
            Skip::Wrapper => "wrapper",
            Skip::Meta => "meta",
            Skip::Sidechain => "sidechain",
            Skip::NonMessage => "non_message",
            Skip::NonHuman => "non_human",
            Skip::EmptyText => "empty_text",
            Skip::Replay => "replay",
            Skip::Unreferenced => "unreferenced",
            Skip::Throwaway => "throwaway",
        }
    }
}

/// Live counters for one file/conversation being parsed. Increments are relaxed
/// atomics - two rayon lanes never share a tally, but Arc keeps ownership simple.
#[derive(Default)]
pub struct Tally {
    seen: AtomicU64,
    rows: AtomicU64,
    agent_rows: AtomicU64,
    events: AtomicU64,
    skips: [AtomicU64; Skip::ALL.len()],
    errors: AtomicU64,
    first_error: Mutex<Option<String>>,
}

impl Tally {
    /// One source record iterated (non-blank line / db row / array element).
    pub fn seen(&self) {
        self.seen_n(1);
    }
    /// Batch records which took an allocation-free prefilter path. Parsers normally use
    /// `seen`; this keeps exact accounting without paying one atomic RMW per discarded line.
    pub fn seen_n(&self, n: u64) {
        self.seen.fetch_add(n, Ordering::Relaxed);
    }
    /// The record became an indexed user-side message row.
    pub fn row(&self) {
        self.rows.fetch_add(1, Ordering::Relaxed);
    }
    /// The record was consumed into the agent side (reply text, model backfill,
    /// tool events) rather than becoming its own message row.
    pub fn agent_row(&self) {
        self.agent_rows.fetch_add(1, Ordering::Relaxed);
    }
    /// An Event was emitted (informational; not part of the seen identity).
    pub fn event(&self) {
        self.events.fetch_add(1, Ordering::Relaxed);
    }
    pub fn skip(&self, s: Skip) {
        self.skip_n(s, 1);
    }
    /// Batched counterpart to `skip`, used together with `seen_n` by byte prefilters.
    pub fn skip_n(&self, s: Skip, n: u64) {
        self.skips[s.index()].fetch_add(n, Ordering::Relaxed);
    }
    /// Reclassify `n` already-counted rows as replay skips - for parsers that
    /// discard a provisional transcript at a boundary (a forked codex child clears
    /// the inherited parent replay once its own start is proven).
    pub fn discard_rows_as_replay(&self, n: u64) {
        self.rows.fetch_sub(n, Ordering::Relaxed);
        self.skips[Skip::Replay.index()].fetch_add(n, Ordering::Relaxed);
    }
    /// Retract `n` counted events discarded at the same boundary.
    pub fn discard_events(&self, n: u64) {
        self.events.fetch_sub(n, Ordering::Relaxed);
    }
    /// A record the parser could not read. `sample` keeps the FIRST error's context
    /// (capped) so the audit points at a reproducible line, not just a count.
    pub fn error(&self, sample: &str) {
        self.errors.fetch_add(1, Ordering::Relaxed);
        let mut slot = self.first_error.lock().unwrap();
        if slot.is_none() {
            let capped: String = sample.chars().take(160).collect();
            *slot = Some(capped);
        }
    }

    #[cfg(test)]
    pub(crate) fn test_record_counts(&self, skip: Skip) -> (u64, u64, u64, u64, u64) {
        (
            self.seen.load(Ordering::Relaxed),
            self.rows.load(Ordering::Relaxed),
            self.agent_rows.load(Ordering::Relaxed),
            self.skips[skip.index()].load(Ordering::Relaxed),
            self.errors.load(Ordering::Relaxed),
        )
    }
}

/// Truncate at the largest char boundary <= `max_bytes`. Error samples clip raw store
/// bytes at a fixed index; slicing there directly panics (and aborts a release ingest)
/// whenever a multibyte char straddles the cut.
pub(crate) fn clip(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    let mut end = max_bytes;
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// One persisted tally. `key` is the freshness fingerprint of the source at parse
/// time (`s:<mtime_ms>:<size>` for stat files, the registry token for conversations);
/// audit only trusts entries whose key still matches the store.
#[derive(Serialize, Deserialize, Clone)]
pub struct FileEntry {
    pub agent: String,
    pub key: String,
    pub seen: u64,
    pub rows: u64,
    pub agent_rows: u64,
    pub events: u64,
    pub skips: BTreeMap<String, u64>,
    pub errors: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub first_error: Option<String>,
}

#[derive(Serialize, Deserialize, Default)]
struct Book {
    version: u32,
    /// Path or framed token-conversation identity -> entry.
    files: BTreeMap<String, FileEntry>,
}

const BOOK_VERSION: u32 = 1;
pub const TOKEN_ID_PREFIX: &str = "\0agrep-intake-token-v1\0";

type OpenTally = (String, Option<String>, &'static str, String, Arc<Tally>);

struct Run {
    /// Tallies opened this run, with the fingerprint captured at open time.
    open: Mutex<Vec<OpenTally>>,
}

static RUN: OnceLock<Run> = OnceLock::new();

fn run() -> &'static Run {
    RUN.get_or_init(|| Run {
        open: Mutex::new(Vec::new()),
    })
}

/// Stat-file fingerprint. Deliberately weaker than the ingest cache's staleness key: the
/// cache stamp also folds in the filesystem change token (catching preserved-mtime edits),
/// but audit recomputes this key independently in Python, so both sides stay on the plain
/// millisecond-mtime + size shape. A preserved-mtime edit reparses correctly via the cache
/// yet still reads as "fresh" here.
pub fn stat_key(path: &Path) -> String {
    match std::fs::metadata(path) {
        Ok(md) => {
            let mt = md
                .modified()
                .ok()
                .and_then(|m| m.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0);
            format!("s:{mt}:{}", md.len())
        }
        Err(_) => "s:0:0".to_string(),
    }
}

fn stat_key_stamped(mtime_ns: i64, size: u64) -> String {
    format!("s:{}:{size}", mtime_ns.max(0) / 1_000_000)
}

/// Open the tally for one source file (stat-fingerprinted).
pub fn file(agent: &'static str, path: &Path) -> Arc<Tally> {
    keyed(agent, &path.to_string_lossy(), stat_key(path))
}

/// Open a tally from the source stamp already validated by the ingest cache.
pub fn file_stamped(agent: &'static str, path: &Path, mtime_ns: i64, size: u64) -> Arc<Tally> {
    keyed(
        agent,
        &path.to_string_lossy(),
        stat_key_stamped(mtime_ns, size),
    )
}

/// Open the tally for one caller-defined keyed unit.
pub fn keyed(agent: &'static str, id: &str, fingerprint: String) -> Arc<Tally> {
    let t = Arc::new(Tally::default());
    run()
        .open
        .lock()
        .unwrap()
        .push((id.to_string(), None, agent, fingerprint, Arc::clone(&t)));
    t
}

pub fn token_id(path: &Path, session: &str) -> String {
    let path = path.to_string_lossy();
    let payload = serde_json::to_string(&(&*path, session))
        .expect("serializing two strings to JSON cannot fail");
    format!("{TOKEN_ID_PREFIX}{payload}")
}

pub fn parse_token_id(id: &str) -> Option<(String, String)> {
    let payload = id.strip_prefix(TOKEN_ID_PREFIX)?;
    let (path, session): (String, String) = serde_json::from_str(payload).ok()?;
    (!path.is_empty() && !session.is_empty()).then_some((path, session))
}

fn legacy_token_id(path: &Path, session: &str) -> String {
    format!("{}#{session}", path.to_string_lossy())
}

/// Open a token-store tally and retire its ambiguous pre-v1 identity when committed.
pub fn keyed_token(
    agent: &'static str,
    path: &Path,
    session: &str,
    fingerprint: String,
) -> Arc<Tally> {
    let id = token_id(path, session);
    let legacy = legacy_token_id(path, session);
    let tally = Arc::new(Tally::default());
    run()
        .open
        .lock()
        .unwrap()
        .push((id, Some(legacy), agent, fingerprint, Arc::clone(&tally)));
    tally
}

fn replace_entry(book: &mut Book, id: String, legacy_id: Option<String>, entry: FileEntry) {
    if let Some(legacy_id) = legacy_id {
        book.files.remove(&legacy_id);
    }
    book.files.insert(id, entry);
}

/// Per-agent rollup for the ingest stdout summary.
#[derive(Default, Clone)]
pub struct AgentSummary {
    pub files: u64,
    pub seen: u64,
    pub rows: u64,
    pub agent_rows: u64,
    pub events: u64,
    pub skips: u64,
    pub errors: u64,
}

fn publish(path: &Path, book: &Book) -> anyhow::Result<()> {
    let body = serde_json::to_vec(book)?;
    crate::cache::write_bytes_atomic(path, &body)
}

/// Fold this run's tallies into the persisted book at `path`: parsed units replace
/// their old entries, unparsed-but-still-present entries survive (their source was
/// served from the parse cache), entries whose source vanished are dropped by the
/// audit rather than here (path existence is adapter-specific). Returns per-agent
/// rollups of THIS run for the stdout summary.
pub fn commit(path: &Path) -> anyhow::Result<BTreeMap<&'static str, AgentSummary>> {
    let mut book: Book = std::fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .filter(|b: &Book| b.version == BOOK_VERSION)
        .unwrap_or_default();
    book.version = BOOK_VERSION;

    let mut summaries: BTreeMap<&'static str, AgentSummary> = BTreeMap::new();
    let opened = std::mem::take(&mut *run().open.lock().unwrap());
    for (id, legacy_id, agent, fingerprint, t) in opened {
        let mut skips = BTreeMap::new();
        let mut skip_total = 0u64;
        for (i, s) in Skip::ALL.iter().enumerate() {
            let n = t.skips[i].load(Ordering::Relaxed);
            if n > 0 {
                skips.insert(s.key().to_string(), n);
                skip_total += n;
            }
        }
        let entry = FileEntry {
            agent: agent.to_string(),
            key: fingerprint,
            seen: t.seen.load(Ordering::Relaxed),
            rows: t.rows.load(Ordering::Relaxed),
            agent_rows: t.agent_rows.load(Ordering::Relaxed),
            events: t.events.load(Ordering::Relaxed),
            skips,
            errors: t.errors.load(Ordering::Relaxed),
            first_error: t.first_error.lock().unwrap().clone(),
        };
        let s = summaries.entry(agent).or_default();
        s.files += 1;
        s.seen += entry.seen;
        s.rows += entry.rows;
        s.agent_rows += entry.agent_rows;
        s.events += entry.events;
        s.skips += skip_total;
        s.errors += entry.errors;
        replace_entry(&mut book, id, legacy_id, entry);
    }

    publish(path, &book)?;
    Ok(summaries)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn skip_indices_follow_persisted_key_order() {
        for (index, skip) in Skip::ALL.into_iter().enumerate() {
            assert_eq!(skip.index(), index, "{}", skip.key());
        }
        assert_eq!(Skip::Throwaway.key(), "throwaway");
    }

    #[test]
    fn clip_respects_char_boundaries() {
        let three_byte = format!("{}汉tail", "x".repeat(79));
        assert_eq!(clip(&three_byte, 80), "x".repeat(79));
        let four_byte = format!("{}🦀tail", "x".repeat(78));
        assert_eq!(clip(&four_byte, 80), "x".repeat(78));
        assert_eq!(clip("short", 80), "short");
        assert_eq!(clip("aé", 3), "aé"); // boundary exactly at the cap keeps the char
    }

    #[test]
    fn stamped_stat_key_uses_audit_millisecond_shape() {
        assert_eq!(stat_key_stamped(1_234_567_890, 42), "s:1234:42");
        assert_eq!(stat_key_stamped(-1, 9), "s:0:9");
    }

    #[test]
    fn token_ids_are_unambiguous_and_retire_the_exact_legacy_key() {
        let path = Path::new("db#part.sqlite");
        let first = token_id(path, "session#part");
        let second = token_id(Path::new("db"), "part.sqlite#session#part");
        assert_ne!(first, second);
        assert!(first.starts_with(TOKEN_ID_PREFIX));
        assert_eq!(
            parse_token_id(&first),
            Some(("db#part.sqlite".into(), "session#part".into()))
        );
        assert_eq!(parse_token_id("db#part.sqlite#session#part"), None);
        assert_eq!(parse_token_id(&format!("{TOKEN_ID_PREFIX}[\"db\"]")), None);

        let legacy = legacy_token_id(path, "session#part");
        let mut book = Book::default();
        let old = FileEntry {
            agent: "cursor".into(),
            key: "old".into(),
            seen: 1,
            rows: 1,
            agent_rows: 0,
            events: 0,
            skips: BTreeMap::new(),
            errors: 0,
            first_error: None,
        };
        book.files.insert(legacy.clone(), old.clone());
        replace_entry(&mut book, first.clone(), Some(legacy.clone()), old);
        assert!(!book.files.contains_key(&legacy));
        assert!(book.files.contains_key(&first));
    }

    #[test]
    fn identity_and_persistence_round_trip() {
        let t = keyed("testagent", "x.jsonl", "s:1:2".into());
        t.seen_n(4);
        t.seen();
        t.row();
        t.agent_row();
        t.skip_n(Skip::Wrapper, 1);
        t.skip(Skip::NonMessage);
        t.error("bad json {");

        let dir = std::env::temp_dir().join(format!("agrep-intake-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("intake_stats.json");
        let _ = std::fs::remove_file(&p);
        let sums = commit(&p).unwrap();
        let s = &sums["testagent"];
        assert_eq!(
            (s.seen, s.rows, s.agent_rows, s.skips, s.errors),
            (5, 1, 1, 2, 1)
        );
        // the audit identity
        assert_eq!(s.seen, s.rows + s.agent_rows + s.skips + s.errors);

        // second commit with no opened tallies keeps the persisted entry
        let sums2 = commit(&p).unwrap();
        assert!(!sums2.contains_key("testagent"));
        let book: Book = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        assert_eq!(book.files["x.jsonl"].seen, 5);
        assert_eq!(
            book.files["x.jsonl"].first_error.as_deref(),
            Some("bad json {")
        );

        let replacement = keyed("testagent", "x.jsonl", "s:2:2".into());
        replacement.seen();
        replacement.row();
        commit(&p).unwrap();
        let book: Book = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        assert_eq!(book.files["x.jsonl"].seen, 1);
        assert_eq!(book.files["x.jsonl"].rows, 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn publication_failure_is_returned() {
        let dir = std::env::temp_dir().join(format!("agrep-intake-error-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(publish(&dir, &Book::default()).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
