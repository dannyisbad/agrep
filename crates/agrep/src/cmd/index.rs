use super::super::*;
pub(crate) fn run(full: bool, with_semantic: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let _foreground = root.foreground()?;
    let refresh = store::refresh(&root, true, full)?;
    let after = std::time::Instant::now();
    agrep_core::coverage::initialize(&root)?;
    semantic::initialize(&root)?;
    if refresh.changed_rows > 0 {
        semantic::mark_index_changed(&root)?;
    }
    if with_semantic {
        semantic::provision(&root)?;
        semantic::initialize(&root)?;
        super::work::after_index(&root, true)?;
        let coverage = semantic::coverage(&store::reader(&root)?)?;
        eprintln!("semantic {}/{} rows", coverage.embedded, coverage.total);
    } else {
        super::work::after_index(&root, false)?;
    }
    statistics(&refresh, after)
}

/// The command's one line: the refresh's phases, how long the whole command was
/// blocked on each lock file, and the time it spent after the refresh.
pub(crate) fn statistics(refresh: &store::Refresh, after: std::time::Instant) -> Result<i32> {
    let mut extra = serde_json::to_value(agrep_core::data::lock_waits())?;
    extra["after_refresh_ms"] = serde_json::json!(after.elapsed().as_secs_f64() * 1000.);
    agrep_core::surface::line(&agrep_core::surface::refresh_json(refresh, extra)?)?;
    Ok(0)
}
