//! Background work after indexing: draining the embedding queue in bounded batches and
//! the configured post-index command.
use super::installed;
use crate::{data::DataRoot, semantic_model::Embedder, semantic_store, store};
use anyhow::{bail, Result};
use std::collections::HashSet;

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
    let embeddings = root.config()?.embeddings != "off" && installed(root);
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
}
/// Embeds the queue in bounded batches, each committed before the next. The caller holds
/// work.lock. Returns whether a background drain stopped for a waiting command.
pub fn drain(root: &DataRoot, how: Drain) -> Result<bool> {
    if !installed(root) || (!how.explicit && root.config()?.embeddings == "off") {
        return Ok(false);
    }
    let mut model = None;
    drain_with(root, how, |texts| {
        let loaded = match model.take() {
            Some(loaded) => loaded,
            None => Embedder::background(root)?,
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
    if !semantic_store::pending(&store::reader(root)?)? {
        return Ok(false);
    }
    let mut left = how.limit;
    loop {
        if !how.explicit && root.config()?.embeddings == "off" {
            break;
        }
        if how.background && root.writer_queued()? {
            return Ok(true);
        }
        let size = left.map_or(128, |n| n.min(128));
        if size == 0 {
            break;
        }
        let c = store::reader(root)?;
        let pending = semantic_store::batch(&c, size)?;
        if pending.is_empty() {
            break;
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
        let started = std::time::Instant::now();
        let vectors = if texts.is_empty() {
            Vec::new()
        } else {
            embed(&texts)?
        };
        semantic_store::publish(root, &pending, &refs, &vectors)?;
        if !texts.is_empty() {
            let rate = pending.len() as f64 / started.elapsed().as_secs_f64();
            let _lock = root.lock(true)?;
            store::writer(root)?.execute(
                "INSERT INTO state VALUES('semantic_rows_per_second',?1) ON CONFLICT(key) DO UPDATE SET value=0.75*CAST(value AS REAL)+0.25*?1",
                [rate],
            )?;
        }
        if let Some(n) = left.as_mut() {
            *n -= pending.len();
        }
        std::thread::yield_now();
    }
    Ok(false)
}

fn post_index_pending(root: &DataRoot) -> Result<bool> {
    if root.config()?.post_index.is_none() {
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
    if root.config()?.post_index.is_some() {
        let _guard = root.lock(true)?;
        store::writer(root)?.execute(
            "INSERT OR REPLACE INTO state VALUES('post_index_pending','1')",
            [],
        )?;
    }
    Ok(())
}
pub fn post_index(root: &DataRoot) -> Result<()> {
    if !post_index_pending(root)? {
        return Ok(());
    }
    let Some(command) = root.config()?.post_index else {
        return Ok(());
    };
    {
        let _guard = root.lock(true)?;
        store::writer(root)?.execute("DELETE FROM state WHERE key='post_index_pending'", [])?;
    }
    #[cfg(windows)]
    let status = {
        use std::os::windows::process::CommandExt;
        // std quotes arguments for the C runtime, escaping inner quotes in a form cmd.exe does
        // not parse; /S makes cmd strip exactly the outer quotes, so the line arrives verbatim.
        std::process::Command::new("cmd")
            .args(["/D", "/S", "/C"])
            .raw_arg(format!("\"{command}\""))
            .status()?
    };
    #[cfg(not(windows))]
    let status = std::process::Command::new("sh")
        .args(["-c", &command])
        .status()?;
    if !status.success() {
        bail!("post_index command failed: {status}");
    }
    Ok(())
}
