use crate::cmd;
use agrep_core::{data::DataRoot, semantic, store};
use anyhow::Result;
pub(crate) fn compact(classic: bool) -> bool {
    if classic {
        return false;
    }
    match std::env::var("AGREP_PROFILE").as_deref() {
        Ok("classic") => false,
        Ok("compact") => true,
        _ => agrep_core::caller::ENV_KEYS
            .iter()
            .any(|k| std::env::var(k).is_ok_and(|v| !v.is_empty())),
    }
}
pub(crate) type CensusTask = std::thread::JoinHandle<Result<agrep_core::freshness::Check>>;
pub(crate) fn ready(no_auto: bool) -> Result<(DataRoot, Option<CensusTask>)> {
    let root = DataRoot::discover()?;
    if no_auto && !root.db().exists() {
        return Err(cmd::usage::no_index());
    }
    let no_auto = no_auto || agrep_core::data::read_only();
    root.validate(!no_auto)?;
    // A first build runs inline; its census then finds the sources it just read.
    if !no_auto && !root.db().exists() {
        store::refresh(&root, true, false)?;
    }
    let census = (!no_auto).then(|| {
        let copy = root.clone();
        std::thread::spawn(move || {
            let _span = agrep_core::semantic_profile::span("freshness");
            let pending = agrep_core::freshness::check(&copy)?;
            agrep_core::semantic_profile::event(serde_json::json!({"freshness":pending.stats}));
            Ok(pending)
        })
    });
    Ok((root, census))
}
/// Complete source ingestion before the first index read. Opt-outs start no work.
pub(crate) fn finish_census(root: &DataRoot, census: Option<CensusTask>) -> Result<()> {
    let Some(task) = census else {
        return Ok(());
    };
    let _span = agrep_core::semantic_profile::span("freshness_join");
    let check = task
        .join()
        .map_err(|_| anyhow::anyhow!("source census failed"))??;
    let mut merge_pending = check.stats.merge_pending;
    if check.stats.changed_sources > 0 {
        let refresh = store::refresh_checked(root, check)?;
        if refresh.changed_rows > 0 {
            semantic::mark_index_changed(root)?;
        }
        merge_pending = agrep_core::fts_segments::pending(&store::reader(root)?)?;
        agrep_core::semantic_profile::event(serde_json::json!({"auto_refresh":refresh}));
    }
    if merge_pending {
        agrep_core::freshness::spawn_merge(root, &std::env::current_exe()?)?;
    }
    semantic::schedule(root)?;
    Ok(())
}
