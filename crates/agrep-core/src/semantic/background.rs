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
    /// How it shows the rows covered, the rate and the time left on stderr.
    pub progress: Progress,
}

/// How a drain shows its progress on stderr.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Progress {
    #[default]
    Hidden,
    /// One line, rewritten as batches finish: for a terminal.
    Rewritten,
    /// A line every 30 seconds at most: for a pipe or a log, which would otherwise hear
    /// nothing until a long drain ends.
    Periodic,
}
impl Progress {
    /// The form that suits this process's stderr.
    pub fn for_stderr() -> Self {
        use std::io::IsTerminal;
        if std::io::stderr().is_terminal() {
            Self::Rewritten
        } else {
            Self::Periodic
        }
    }
}

/// When a drain shows its progress, and the text that shows it.
pub(super) struct Meter {
    form: Progress,
    started: Instant,
    shown: Option<Instant>,
}
impl Meter {
    pub(super) fn new(form: Progress, started: Instant) -> Self {
        Self {
            form,
            started,
            shown: None,
        }
    }
    /// The text that shows `line` at `now`, when it is due: a rewritten line at once and
    /// then every [`SHOW_EVERY`], a periodic one [`LINE_EVERY`] after the start or the last.
    pub(super) fn show(&mut self, now: Instant, line: impl FnOnce() -> String) -> Option<String> {
        let due = match self.form {
            Progress::Hidden => false,
            Progress::Rewritten => self.shown.is_none_or(|at| now - at >= SHOW_EVERY),
            Progress::Periodic => now - self.shown.unwrap_or(self.started) >= LINE_EVERY,
        };
        due.then(|| {
            self.shown = Some(now);
            self.render(&line(), "")
        })
    }
    /// The text that shows the final `line`, once any was shown, ended by a newline.
    pub(super) fn finish(&self, line: impl FnOnce() -> String) -> Option<String> {
        self.shown.map(|_| self.render(&line(), "\n"))
    }
    fn render(&self, line: &str, end: &str) -> String {
        match self.form {
            // Erases what is left of a longer earlier line.
            Progress::Rewritten => format!("\r{line}\x1b[K{end}"),
            Progress::Hidden | Progress::Periodic => format!("{line}\n"),
        }
    }
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
    let mut line = format!(
        "embedding messages: {} of {} rows",
        crate::surface::number(covered),
        crate::surface::number(total)
    );
    let seconds = rate.map(|rate| (left as f64 / rate).ceil() as u64);
    if let Some(rate) = rate {
        line += &format!(" · {rate:.0} rows/s");
    }
    line + " · " + &super::estimate(seconds)
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
    let mut meter = Meter::new(how.progress, mark);
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
        if let Some(text) = meter.show(now, || drain_line(&coverage, left, rate.per_second())) {
            crate::surface::note(format_args!("{text}"));
        }
        std::thread::yield_now();
    };
    if let Some(text) = meter.finish(|| drain_line(&coverage, left, rate.per_second())) {
        crate::surface::note(format_args!("{text}"));
    }
    Ok(stopped)
}

const RECORD_EVERY: Duration = Duration::from_secs(5);
const SHOW_EVERY: Duration = Duration::from_millis(250);
const LINE_EVERY: Duration = Duration::from_secs(30);

fn drain_line(
    coverage: &semantic_store::Coverage,
    limit: Option<usize>,
    rate: Option<f64>,
) -> String {
    let pending = coverage.total.saturating_sub(coverage.embedded);
    let left = limit.map_or(pending, |n| n.min(pending));
    progress_line(coverage.embedded, coverage.total, left, rate)
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
