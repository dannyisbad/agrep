use super::super::*;
pub(crate) fn run(full: bool, with_semantic: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let refresh = store::refresh(&root, true, full)?;
    println!("{}", serde_json::to_string(&refresh)?);
    agrep_core::coverage::initialize(&root)?;
    if refresh.changed_rows > 0 {
        semantic::mark_index_changed(&root)?;
    }
    if with_semantic {
        semantic::provision(&root)?;
        semantic::initialize(&root)?;
        semantic::work_explicit(&root)?;
        let coverage = semantic::coverage(&store::reader(&root)?)?;
        eprintln!("semantic {}/{} rows", coverage.embedded, coverage.total);
    }
    semantic::schedule(&root)?;
    Ok(0)
}
