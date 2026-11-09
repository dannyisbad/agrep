//! Background work after indexing: draining the embedding queue in bounded batches and
//! the configured post-index command.
use super::{installed, post_index as hook};
use crate::{data::DataRoot, semantic_model::Embedder, semantic_store, store};
use anyhow::Result;
use std::collections::HashSet;
use std::time::{Duration, Instant};

pub(super) fn background_thread_count(logical: usize, requested: Option<usize>) -> usize {
    let cap = (logical / 2).max(1);
    requested.unwrap_or(cap).clamp(1, cap)
}

/// Called at the start of the dedicated __work process, before any thread pool
/// or BLAS initialization. Parallel heads use Rayon; BLAS stays single-threaded
/// so nested parallelism cannot multiply the CPU allowance.
pub fn configure_background() -> Result<()> {
    let logical = std::thread::available_parallelism().map_or(1, usize::from);
    let requested = std::env::var("RAYON_NUM_THREADS")
        .ok()
        .and_then(|s| s.parse().ok());
    let threads = background_thread_count(logical, requested);
    std::env::set_var("RAYON_NUM_THREADS", threads.to_string());
    std::env::set_var("VECLIB_MAXIMUM_THREADS", "1");
    std::env::set_var("TOKENIZERS_PARALLELISM", "false");
    #[cfg(unix)]
    unsafe {
        if libc::getpriority(libc::PRIO_PROCESS, 0) < 10
            && libc::setpriority(libc::PRIO_PROCESS, 0, 10) < 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::Threading::{
            GetCurrentProcess, SetPriorityClass, BELOW_NORMAL_PRIORITY_CLASS,
        };
        if SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS) == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    Ok(())
}
/// Spawn only for a prepared model. Automatic hybrid never downloads anything.
pub fn schedule(root: &DataRoot) -> Result<()> {
    if crate::data::read_only() {
        return Ok(());
    }
    let embeddings = root.config()?.embeddings != "off"
        && installed(root)
        && !crate::semantic_model::rejected(root);
    let embedding_pending = embeddings && semantic_store::pending(&store::reader(root)?)?;
    let maintenance_pending = post_index_pending(root)?;
    let archive_due = crate::archive::due(root)?;
    crate::semantic_profile::event(serde_json::json!({"work_state":{
        "embedding_pending":embedding_pending,"maintenance_pending":maintenance_pending,
        "archive_due":archive_due
    }}));
    if !(archive_due || embedding_pending || maintenance_pending) {
        return Ok(());
    }
    crate::freshness::spawn(root, &std::env::current_exe()?)?;
    Ok(())
}

/// How one queue drain runs.
#[derive(Clone, Copy, Debug, Default)]
pub struct Drain {
    /// Embeds even with `embeddings` off (`index --semantic`).
    pub explicit: bool,
    /// Background work: stops between batches while a writer or explicit command waits.
    pub background: bool,
    /// The most queued rows this drain embeds, newest first (`reindex --max-new`).
    pub limit: Option<usize>,
    /// Rewrites a line on stderr with the rows covered, the rate and the time left.
    pub progress: bool,
}

/// Rows covered per second of wall time over about the last minute. Rows whose text is
/// already embedded count with the time they took too, so the rate says how fast the queue,
/// and so the remaining count, goes down.
#[derive(Default)]
pub(super) struct DrainRate {
    rows: f64,
    seconds: f64,
}
impl DrainRate {
    const WINDOW_SECONDS: f64 = 60.;
    pub(super) fn observe(&mut self, rows: usize, seconds: f64) {
        let kept = (-seconds / Self::WINDOW_SECONDS).exp();
        self.rows = self.rows * kept + rows as f64;
        self.seconds = self.seconds * kept + seconds;
    }
    pub(super) fn per_second(&self) -> Option<f64> {
        let rate = self.rows / self.seconds;
        (rate.is_finite() && rate > 0.).then_some(rate)
    }
}

/// The line `index --semantic` and `reindex` rewrite while they drain.
pub(super) fn progress_line(
    covered: usize,
    total: usize,
    left: usize,
    rate: Option<f64>,
) -> String {
    let mut line = format!("embedding messages: {covered} of {total} rows");
    if let Some(rate) = rate {
        let seconds = (left as f64 / rate).ceil() as u64;
        line += &format!(
            " · {rate:.0} rows/s · about {} left",
            super::time_left(seconds)
        );
    }
    line
}
/// Embeds the queue in bounded batches, each committed before the next. The caller holds
/// work.lock. Returns whether a background drain stopped for a waiting command.
pub fn drain(root: &DataRoot, how: Drain) -> Result<bool> {
    if !installed(root)
        || (!how.explicit && root.config()?.embeddings == "off")
        || (how.background && crate::semantic_model::rejected(root))
    {
        return Ok(false);
    }
    let mut model = None;
    drain_with(root, how, |texts| {
        let loaded = match model.take() {
            Some(loaded) => loaded,
            None => Embedder::background(root).inspect_err(|e| {
                // Logged by this run only: the next schedule sees the rejection.
                if how.background && e.is::<crate::semantic_model::Rejected>() {
                    let _ = crate::semantic_model::record_rejection(root);
                }
            })?,
        };
        let vectors = loaded.embed(texts);
        model = Some(loaded);
        vectors
    })
}
/// The drain's batches with `embed` producing each batch's vectors; it is called only for
/// a batch holding text no published chunk has.
pub(super) fn drain_with(
    root: &DataRoot,
    how: Drain,
    mut embed: impl FnMut(&[String]) -> Result<Vec<Vec<f32>>>,
) -> Result<bool> {
    let c = store::reader(root)?;
    if !semantic_store::pending(&c)? {
        return Ok(false);
    }
    let mut rate = DrainRate::default();
    let mut coverage = semantic_store::coverage(&c)?;
    drop(c);
    let mut left = how.limit;
    let mut mark = Instant::now();
    let mut loaded = false;
    let mut recorded: Option<Instant> = None;
    let mut shown: Option<Instant> = None;
    let stopped = loop {
        if !how.explicit && root.config()?.embeddings == "off" {
            break false;
        }
        if how.background && root.writer_queued()? {
            break true;
        }
        let size = left.map_or(128, |n| n.min(128));
        if size == 0 {
            break false;
        }
        let c = store::reader(root)?;
        let pending = semantic_store::batch(&c, size)?;
        if pending.is_empty() {
            break false;
        }
        let mut seen = HashSet::new();
        let mut texts = vec![];
        let mut refs = vec![];
        for p in &pending {
            if !seen.insert(p.key.clone()) || semantic_store::known(&c, &p.key)? {
                continue;
            }
            for (n, text) in p.chunks.iter().enumerate() {
                refs.push((p.key.clone(), n));
                texts.push(text.clone());
            }
        }
        drop(c);
        let loading = !texts.is_empty() && !loaded;
        let vectors = if texts.is_empty() {
            Vec::new()
        } else {
            embed(&texts)?
        };
        loaded |= loading;
        let covered = semantic_store::publish(root, &pending, &refs, &vectors)?;
        let now = Instant::now();
        // The first batch that embeds also loads the model, once a drain.
        if !loading {
            rate.observe(covered, (now - mark).as_secs_f64());
        }
        mark = now;
        coverage.embedded += covered;
        if let Some(n) = left.as_mut() {
            *n -= pending.len();
        }
        if let Some(per_second) = rate.per_second() {
            if recorded.is_none_or(|at| now - at >= RECORD_EVERY) {
                recorded = Some(now);
                let _lock = root.lock(true)?;
                store::writer(root)?.execute(
                    "INSERT INTO state VALUES('semantic_rows_per_second',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [per_second],
                )?;
            }
        }
        if how.progress && shown.is_none_or(|at| now - at >= SHOW_EVERY) {
            shown = Some(now);
            show_progress(&coverage, left, rate.per_second());
        }
        std::thread::yield_now();
    };
    if how.progress && shown.is_some() {
        show_progress(&coverage, left, rate.per_second());
        crate::surface::note(format_args!("\n"));
    }
    Ok(stopped)
}

const RECORD_EVERY: Duration = Duration::from_secs(5);
const SHOW_EVERY: Duration = Duration::from_millis(250);

fn show_progress(coverage: &semantic_store::Coverage, limit: Option<usize>, rate: Option<f64>) {
    let pending = coverage.total.saturating_sub(coverage.embedded);
    let left = limit.map_or(pending, |n| n.min(pending));
    // Erases what is left of a longer earlier line.
    crate::surface::note(format_args!(
        "\r{}\x1b[K",
        progress_line(coverage.embedded, coverage.total, left, rate)
    ));
}

fn post_index_pending(root: &DataRoot) -> Result<bool> {
    if hook::command(&root.config()?).is_none() {
        return Ok(false);
    }
    let c = store::reader(root)?;
    Ok(c.query_row(
        "SELECT EXISTS(SELECT 1 FROM state WHERE key='post_index_pending')",
        [],
        |r| r.get(0),
    )?)
}
pub fn mark_index_changed(root: &DataRoot) -> Result<()> {
    if hook::command(&root.config()?).is_some() {
        let _guard = root.lock(true)?;
        store::writer(root)?.execute(
            "INSERT OR REPLACE INTO state VALUES('post_index_pending','1')",
            [],
        )?;
    }
    Ok(())
}
/// Its failure is reported, never returned: the index it follows is already complete.
pub fn post_index(root: &DataRoot) -> Result<()> {
    if !post_index_pending(root)? {
        return Ok(());
    }
    let config = root.config()?;
    let Some(command) = hook::command(&config) else {
        return Ok(());
    };
    {
        let _guard = root.lock(true)?;
        store::writer(root)?.execute("DELETE FROM state WHERE key='post_index_pending'", [])?;
    }
    hook::run_and_report(root, command);
    Ok(())
}
