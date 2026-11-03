use super::super::*;

const TASKS: &[&str] = &["refresh", "semantic", "reclaim", "archive", "post_index"];

pub(crate) fn run(tasks: &[String]) -> Result<i32> {
    semantic::configure_background()?;
    let root = DataRoot::discover()?;
    if !root.path().exists() {
        return Ok(0);
    }
    root.validate(false)?;
    execute(&root, tasks, false, false)
}

pub(crate) fn after_index(root: &DataRoot, force_semantic: bool) -> Result<i32> {
    let tasks = TASKS[1..].iter().map(|s| s.to_string()).collect::<Vec<_>>();
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
    let Some(_lock) = root.work_lock(blocking)? else {
        return Ok(0);
    };
    let selected = |task: &str| requested.is_empty() || requested.iter().any(|t| t == task);
    if selected("refresh") {
        let refresh = store::refresh(root, false, false)?;
        if refresh.changed_rows > 0 {
            semantic::mark_index_changed(root)?;
        }
    }
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
