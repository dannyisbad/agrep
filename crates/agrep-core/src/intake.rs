//! Parser-local accounting. Nothing is published outside SQLite.
use std::{
    cell::RefCell,
    path::Path,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
};
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

    pub fn test_record_counts(&self, skip: Skip) -> (u64, u64, u64, u64, u64) {
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

thread_local! { static SCOPES: RefCell<Vec<Vec<Arc<Tally>>>> = const { RefCell::new(Vec::new()) }; }
pub(crate) fn scoped<T>(f: impl FnOnce() -> T) -> (T, Vec<Arc<Tally>>) {
    SCOPES.with(|s| s.borrow_mut().push(vec![]));
    let v = f();
    let t = SCOPES.with(|s| {
        let mut s = s.borrow_mut();
        let t = s.pop().unwrap();
        if let Some(outer) = s.last_mut() {
            outer.extend(t.iter().cloned());
        }
        t
    });
    (v, t)
}
pub(crate) fn discard(_: &[Arc<Tally>]) {}
pub fn forget(_: &Path) {}
pub fn file(a: &'static str, p: &Path) -> Arc<Tally> {
    keyed(a, &p.to_string_lossy(), String::new())
}
pub fn file_stamped(a: &'static str, p: &Path, _: i64, _: u64) -> Arc<Tally> {
    file(a, p)
}
pub fn keyed(_: &'static str, _: &str, _: String) -> Arc<Tally> {
    let t = Arc::new(Tally::default());
    SCOPES.with(|s| {
        if let Some(v) = s.borrow_mut().last_mut() {
            v.push(t.clone())
        }
    });
    t
}
pub fn keyed_token(a: &'static str, p: &Path, s: &str, f: String) -> Arc<Tally> {
    keyed(a, &token_id(p, s), f)
}
pub fn token_id(p: &Path, s: &str) -> String {
    serde_json::to_string(&(p.to_string_lossy(), s)).unwrap()
}
pub fn parse_token_id(s: &str) -> Option<(String, String)> {
    serde_json::from_str(s).ok()
}
pub(crate) fn errors(t: &[Arc<Tally>]) -> bool {
    t.iter().any(|v| v.errors.load(Ordering::Relaxed) > 0)
}

thread_local! {static DIAGNOSTICS:RefCell<Vec<String>>=const {RefCell::new(Vec::new())};}
pub fn diagnostic(message: std::fmt::Arguments<'_>) {
    DIAGNOSTICS.with(|d| d.borrow_mut().push(message.to_string()));
}
pub fn take_diagnostics() -> Vec<String> {
    DIAGNOSTICS.with(|d| std::mem::take(&mut *d.borrow_mut()))
}
