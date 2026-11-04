use super::super::*;

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
    execute(&root, tasks, false, false)
}

/// An explicit index or reindex finishes the background tasks inline except merging,
/// which it leaves to a detached worker so that it waits for no merge step.
pub(crate) fn after_index(root: &DataRoot, force_semantic: bool) -> Result<i32> {
    if root.db().exists() && agrep_core::fts_segments::pending(&store::reader(root)?)? {
        agrep_core::freshness::spawn_merge(root, &std::env::current_exe()?)?;
    }
    let tasks = TASKS[2..].iter().map(|s| s.to_string()).collect::<Vec<_>>();
    execute(root, &tasks, true, force_semantic)
}

fn execute(
    root: &DataRoot,
    requested: &[String],
    blocking: bool,
    force_semantic: bool,
) -> Result<i32> {
    for task in requested {
        if !TASKS.contains(&task.as_str()) {
            bail!("unknown background task: {task}");
        }
    }
    let selected = |task: &str| requested.is_empty() || requested.iter().any(|t| t == task);
    if selected("refresh") {
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
    if selected("semantic") {
        semantic::drain(root, force_semantic)?;
    }
    if selected("reclaim") && root.db().exists() {
        semantic::reclaim(root)?;
    }
    let incomplete = selected("archive")
        && agrep_core::archive::capture_if_due(root)?.is_some_and(|s| !s.failures.is_empty());
    if selected("post_index") {
        semantic::post_index(root)?;
    }
    Ok(i32::from(incomplete))
}
