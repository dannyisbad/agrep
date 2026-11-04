use super::super::*;
pub(crate) fn run(full: bool, with_semantic: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let _foreground = root.foreground()?;
    let refresh = store::refresh(&root, true, full)?;
    agrep_core::surface::line(&agrep_core::surface::refresh_json(&refresh)?)?;
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
    }
    if !with_semantic {
        super::work::after_index(&root, false)?;
    }
    Ok(0)
}
