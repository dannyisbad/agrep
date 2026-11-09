use agrep_core::{data::DataRoot, semantic, store};
use anyhow::{bail, Result};

const TASKS: &[&str] = &[
    "refresh",
    "merge",
    "semantic",
    "reclaim",
    "archive",
    "post_index",
];

pub(crate) fn run(tasks: &[String]) -> Result<i32> {
    semantic::configure_background()?;
    let root = DataRoot::discover()?;
    if !root.path().exists() {
        return Ok(0);
    }
    root.validate(false)?;
    let drain = semantic::Drain {
        background: true,
        ..Default::default()
    };
    execute(&root, tasks, false, drain)
}

/// What an explicit index or reindex finishes before it exits. Merging and archive
/// captures are left to detached workers so that the command waits for neither: a first
/// capture of large stores takes minutes. With `embed` the command drains the embedding
/// queue itself, waiting for work.lock; without it the queue is left to a background
/// worker, and the remaining tasks run only while work.lock is free.
pub(crate) fn after_index(root: &DataRoot, embed: Option<semantic::Drain>) -> Result<()> {
    if root.db().exists() && agrep_core::fts_segments::pending(&store::reader(root)?)? {
        agrep_core::freshness::spawn_merge(root, &std::env::current_exe()?)?;
    }
    let first = if embed.is_some() { 2 } else { 3 };
    let tasks = TASKS[first..]
        .iter()
        .filter(|task| **task != "archive")
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    execute(root, &tasks, embed.is_some(), embed.unwrap_or_default())?;
    if embed.is_none() {
        semantic::schedule(root)?;
    } else if !agrep_core::data::read_only() && agrep_core::archive::due(root)? {
        agrep_core::freshness::spawn(root, &std::env::current_exe()?)?;
    }
    Ok(())
}

fn execute(
    root: &DataRoot,
    requested: &[String],
    blocking: bool,
    drain: semantic::Drain,
) -> Result<i32> {
    for task in requested {
        if !TASKS.contains(&task.as_str()) {
            bail!("unknown background task: {task}");
        }
    }
    let selected = |task: &str| requested.is_empty() || requested.iter().any(|t| t == task);
    if requested.iter().any(|task| task == "refresh") {
        let Some(_lock) = root.work_lock(blocking)? else {
            return Ok(0);
        };
        let refresh = store::refresh(root, false, false)?;
        if refresh.changed_rows > 0 {
            semantic::mark_index_changed(root)?;
        }
    }
    // Merging holds merge.lock instead of work.lock, so a worker started meanwhile
    // refreshes after at most one merge step.
    if selected("merge") {
        let merge = agrep_core::fts_segments::upkeep(root)?;
        agrep_core::semantic_profile::event(serde_json::json!({ "merge": merge }));
    }
    if !TASKS[2..].iter().any(|task| selected(task)) {
        return Ok(0);
    }
    let Some(_lock) = root.work_lock(blocking)? else {
        return Ok(0);
    };
    if selected("semantic") && semantic::drain(root, drain)? {
        // A writer or explicit command waits: it, or the next worker, does the rest.
        return Ok(0);
    }
    if selected("reclaim") && root.db().exists() {
        semantic::reclaim(root)?;
    }
    let archived = if selected("archive") {
        agrep_core::archive::capture_if_due(root)
    } else {
        Ok(None)
    };
    // A failed capture is reported after the post-index command, which does not depend on it.
    if selected("post_index") {
        semantic::post_index(root)?;
    }
    let incomplete = archived?.is_some_and(|s| !s.failures.is_empty() || s.damaged_versions > 0);
    Ok(i32::from(incomplete))
}
