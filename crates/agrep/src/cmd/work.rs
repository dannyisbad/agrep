use super::super::*;
pub(crate) fn run() -> Result<i32> {
    let root = DataRoot::discover()?;
    if !root.path().exists() {
        return Ok(0);
    }
    root.validate(false)?;
    let Some(_lock) = root.work_lock(false)? else {
        return Ok(0);
    };
    // Engine integration adds the source refresh before these ordered tasks.
    semantic::drain(&root, false)?;
    if root.db().exists() {
        semantic::reclaim(&root)?;
    }
    let incomplete =
        agrep_core::archive::capture_if_due(&root)?.is_some_and(|s| !s.failures.is_empty());
    semantic::post_index(&root)?;
    Ok(i32::from(incomplete))
}
