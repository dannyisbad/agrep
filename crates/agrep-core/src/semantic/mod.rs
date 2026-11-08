//! In-process semantic retrieval and short-lived background queue draining.
mod background;
mod budget;
mod scan;
use crate::{
    data::DataRoot,
    query::{self, Hit, Options, Results},
    semantic_model::{self, Embedder},
    semantic_policy, semantic_store, store,
};
use anyhow::{bail, Result};
pub use background::{
    configure_background, drain, mark_index_changed, post_index, schedule, Drain,
};
pub use budget::{build_progress, is_building, BuildProgress};
use budget::{Overlay, QueryCost};
use rusqlite::Connection;
use scan::{resolve_scan, VectorScan};
pub use semantic_model::{installed, provision};
pub use semantic_policy::{eligible, FLOOR, STRONG};
pub use semantic_store::{coverage, reclaim, reset, Coverage};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

pub struct SearchResults {
    pub result: Results,
    pub coverage: Coverage,
}

/// Owns model inference and q8 scanning while the caller runs its keyword lane.
/// No SQL connection crosses a thread; `finish` resolves against the caller's snapshot.
pub struct QueryWork {
    coverage: Coverage,
    options: Options,
    family_diverse: bool,
    lane: Option<std::thread::JoinHandle<Result<VectorScan>>>,
    /// Set when the caller starts waiting for the lane.
    joined: Arc<AtomicBool>,
}
pub fn begin(
    root: &DataRoot,
    c: &Connection,
    o: &Options,
    family_diverse: bool,
) -> Result<QueryWork> {
    if !installed(root) {
        bail!("semantic model not installed: run agrep setup");
    }
    if let Some(reason) = semantic_policy::rejection(&o.pattern) {
        return Err(Rejected(reason).into());
    }
    // This first read pins the same WAL snapshot used by the lexical query.
    let coverage = {
        let _span = crate::semantic_profile::span("meaning_snapshot");
        coverage(c)?
    };
    let overlay = Overlay::snapshot(c, &coverage)?;
    let cost = QueryCost::read(c);
    let progress = build_progress(c, &coverage)?;
    let joined = Arc::new(AtomicBool::new(false));
    let lane = if coverage.total == 0 {
        None
    } else {
        let listed = {
            let _span = crate::semantic_profile::span("vector_block_list");
            semantic_store::block_list(c)?
        };
        let lane = Lane {
            root: root.clone(),
            pattern: o.pattern.clone(),
            options: o.clone(),
            family_diverse,
            overlay,
            cost,
            progress,
            waiting: joined.clone(),
        };
        Some(crate::logging::spawn_semantic(move || lane.run(listed))?)
    };
    Ok(QueryWork {
        coverage: Coverage {
            embedded: coverage.total,
            total: coverage.total,
        },
        options: o.clone(),
        family_diverse,
        lane,
        joined,
    })
}
/// What the meaning lane's thread takes from the caller's snapshot.
struct Lane {
    root: DataRoot,
    pattern: String,
    options: Options,
    family_diverse: bool,
    overlay: Overlay,
    cost: Option<QueryCost>,
    progress: BuildProgress,
    /// Set once the caller waits: a speculative prefetch is then pointless.
    waiting: Arc<AtomicBool>,
}
impl Lane {
    fn run(self, listed: Vec<(String, usize)>) -> Result<VectorScan> {
        let Lane {
            root,
            pattern,
            options,
            family_diverse,
            overlay,
            cost,
            progress,
            waiting,
        } = self;
        // Mapping and reading the blocks overlaps model loading and inference.
        let (blocks, vector) = std::thread::scope(|scope| -> Result<_> {
            let opened = {
                let root = &root;
                std::thread::Builder::new()
                    .name("agrep-semantic-query".into())
                    .spawn_scoped(scope, move || {
                        let _span = crate::semantic_profile::span("vector_block_open");
                        semantic_store::open_blocks(root, listed)
                    })?
            };
            let vector = embed_query(&root, pattern, &overlay.texts, cost, progress);
            let blocks = opened.join().map_err(crate::logging::panic_error)?;
            Ok((blocks, vector))
        })?;
        let (vector, extra) = vector?;
        let scan = VectorScan::with_overlay(blocks?, &vector, overlay, &extra)?;
        // While the keyword lane still runs, the same resolution on this thread's
        // own snapshot pages in the rows `finish` reads; its result is discarded.
        if !waiting.load(Ordering::Relaxed) {
            if let Ok(reader) = store::reader(&root) {
                let _span = crate::semantic_profile::span("vector_prefetch");
                let _ = resolve_scan(&reader, &options, family_diverse, &scan, Some(&waiting));
            }
        }
        Ok(scan)
    }
}
/// The query's vector, and the vectors of the snapshot's queued rows when embedding them
/// fits the inline budget: the recorded `cost`, or else one measured on the first row.
fn embed_query(
    root: &DataRoot,
    pattern: String,
    queued: &[String],
    cost: Option<QueryCost>,
    progress: BuildProgress,
) -> Result<(Vec<f32>, Vec<Vec<f32>>)> {
    let started = std::time::Instant::now();
    let model = Embedder::query(root);
    let load_seconds = started.elapsed().as_secs_f64();
    let model = model?;
    let mut extra = Vec::new();
    if !queued.is_empty() {
        let _span = crate::semantic_profile::span("inline_embedding");
        let measured = if let Some(cost) = cost {
            cost
        } else {
            let batch = model.inline_batch(&queued[..1])?;
            QueryCost::observe(root, load_seconds, &batch);
            let measured = QueryCost {
                load: load_seconds,
                cpu_unit: batch.cpu_unit,
                span_unit: batch.span_unit,
                updated: QueryCost::now(),
            };
            extra = batch.vectors;
            measured
        };
        let remaining = &queued[extra.len()..];
        if !remaining.is_empty() {
            let weights = remaining
                .iter()
                .map(|text| model.work_units(std::slice::from_ref(text)))
                .collect::<Result<Vec<_>>>()?;
            if !measured.fits(
                weights.iter().sum(),
                weights.iter().copied().fold(0., f64::max),
                started.elapsed().as_secs_f64(),
            ) {
                return Err(progress.into());
            }
            let batch = model.inline_batch(remaining)?;
            QueryCost::observe(root, load_seconds, &batch);
            extra.extend(batch.vectors);
        }
    }
    let vector = model.embed(&[pattern])?.remove(0);
    Ok((vector, extra))
}
pub fn finish(c: &Connection, work: QueryWork) -> Result<SearchResults> {
    let result = if let Some(lane) = work.lane {
        work.joined.store(true, Ordering::Relaxed);
        let scan = lane.join().map_err(crate::logging::panic_error)??;
        let _span = crate::semantic_profile::span("vector_refine");
        resolve_scan(c, &work.options, work.family_diverse, &scan, None)?
    } else {
        let mut r = Results::default();
        r.total_basis = "exact".into();
        r
    };
    Ok(SearchResults {
        result,
        coverage: work.coverage,
    })
}
pub fn search(root: &DataRoot, o: &Options, family_diverse: bool) -> Result<SearchResults> {
    let c = store::reader(root)?;
    finish(&c, begin(root, &c, o, family_diverse)?)
}
/// 0.4.9's `semantic_status` for one meaning lane that answered: `found` is whether a row
/// cleared the floor (py/search.py:510-578).
pub fn status(coverage: &Coverage, found: bool) -> serde_json::Value {
    debug_assert!(!coverage.building());
    serde_json::json!({"state":if found {"ready"} else {"no-confident-match"},"eligible":true,"reason":null,"min_score":FLOOR,"score_kind":"cosine","coverage":coverage,"complete":true,"fallback_recommended":false})
}
/// A query 0.4.9's meaning policy refuses, with its reason code.
#[derive(Debug)]
pub struct Rejected(pub &'static str);
impl std::fmt::Display for Rejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("semantic query requires natural language; use --lexical for identifiers")
    }
}
impl std::error::Error for Rejected {}
/// 0.4.9's `semantic_status` of a lane that did not answer: a refused query carries its
/// policy reason, a failed lane its cause, bounded as 0.4.9 bounded it
/// (py/search.py:524-531, 581-610).
pub fn failed(error: &anyhow::Error) -> serde_json::Value {
    if let Some(Rejected(reason)) = error.downcast_ref() {
        return serde_json::json!({"state":"query-rejected","complete":false,"fallback_recommended":true,"eligible":false,"reason":reason,"min_score":FLOOR,"coverage":null});
    }
    if let Some(progress) = error.downcast_ref::<BuildProgress>() {
        return serde_json::json!({"state":"unavailable","eligible":true,"reason":progress.to_string(),"min_score":FLOOR,"score_kind":"unavailable","complete":false,"fallback_recommended":true,"progress":progress});
    }
    let cause: String = crate::surface::safe(&format!("{error:#}"), false)
        .chars()
        .take(140)
        .collect();
    serde_json::json!({"state":"unavailable","eligible":true,"reason":cause.trim(),"min_score":FLOOR,"score_kind":"unavailable","coverage":null,"complete":false,"fallback_recommended":true})
}
/// 0.4.9's `semantic_unavailable_notice`, the one line an explicit `-s` prints when its
/// lane did not answer (py/surface_policy.py:2749-2761).
pub fn unavailable_notice(status: &serde_json::Value) -> String {
    let state = status["state"].as_str().unwrap_or("unavailable");
    let detail = match status["reason"].as_str().filter(|r| !r.is_empty()) {
        Some(reason) => reason,
        None if state == "unavailable" => "the meaning lane did not answer",
        None => state,
    };
    format!("semantic search unavailable: {detail}")
}
/// Marks 0.4.9's weak meaning rows (py/search.py:432-480, 632-636, 966-1036): a cosine
/// below the strong band, less than half the query's terms, or a query none of whose
/// first eight terms occurs in the lane's scope. A keyword lane that found rows in that
/// scope already proves a term occurs, so only a lane without one probes.
pub fn mark_weak(c: &Connection, o: &Options, hits: &mut [Hit], lexical_found: bool) -> Result<()> {
    if hits.is_empty() {
        return Ok(());
    }
    let terms = semantic_policy::query_terms(&o.pattern);
    let unanchored = !lexical_found && !terms.is_empty() && {
        let mut absent = true;
        for term in terms.iter().take(8) {
            let probe = Options {
                pattern: term.clone(),
                regex: false,
                word: false,
                sort: "score".into(),
                limit: 1,
                exact: false,
                count_only: false,
                count_by_tier: false,
                content: false,
                chats: false,
                tools: o.tools,
                filters: query::Filters {
                    no_meta: false,
                    ..o.filters.clone()
                },
            };
            if !query::search(c, &probe)?.hits.is_empty() {
                absent = false;
                break;
            }
        }
        absent
    };
    for h in hits.iter_mut().filter(|h| h.matched == "semantic") {
        h.sem_weak = unanchored
            || semantic_policy::term_weak(&terms, &h.text)
            || h.score < semantic_policy::STRONG;
    }
    Ok(())
}
/// Public for reproducible retrieval tests using frozen normalized vectors.
pub fn search_vector(
    root: &DataRoot,
    c: &Connection,
    o: &Options,
    family_diverse: bool,
    vector: &[f32],
) -> Result<Results> {
    resolve_scan(
        c,
        o,
        family_diverse,
        &VectorScan::new(semantic_store::blocks(root, c)?, vector)?,
        None,
    )
}

#[cfg(test)]
mod tests {
    #[test]
    fn inline_budget_uses_query_cpu_work_and_serial_critical_path() {
        let cores = Embedder::inline_streams() as f64;
        let slow = QueryCost {
            load: 0.01,
            cpu_unit: cores * 0.2,
            span_unit: 0.2,
            updated: QueryCost::now(),
        };
        let fast = QueryCost {
            cpu_unit: cores * 0.01,
            span_unit: 0.01,
            ..slow
        };
        assert!(slow.fits(2., 1., 0.01));
        assert!(!slow.fits(5., 1., 0.01));
        assert!(fast.fits(20., 1., 0.01));
        assert!(!fast.fits(20., 1., 0.7));
        let long = QueryCost {
            span_unit: 0.6,
            ..fast
        };
        assert!(!long.fits(1., 1., 0.01));
        assert!(
            QueryCost::preparation_limit(Some(slow)) < QueryCost::preparation_limit(Some(fast))
        );
        assert_eq!(QueryCost::preparation_limit(None), 32);
    }
    #[test]
    fn query_calibration_records_slowdowns_without_using_bulk_rates() {
        let (_temp, root, _) = setup();
        let batch = |unit| semantic_model::InlineBatch {
            cpu_unit: unit,
            span_unit: unit,
            ..Default::default()
        };
        QueryCost::observe(&root, 0.1, &batch(0.01));
        let c = store::reader(&root).unwrap();
        assert_eq!(QueryCost::read(&c).unwrap().cpu_unit, 0.01);
        QueryCost::observe(&root, 0.2, &batch(0.1));
        let slow = QueryCost::read(&store::reader(&root).unwrap()).unwrap();
        assert_eq!(slow.cpu_unit, 0.1);
        assert_eq!(slow.load, 0.2);
        QueryCost::observe(&root, 0.1, &batch(0.01));
        let decayed = QueryCost::read(&store::reader(&root).unwrap()).unwrap();
        assert!(decayed.cpu_unit > 0.01 && decayed.cpu_unit < slow.cpu_unit);
        assert!(c
            .query_row(
                "SELECT value FROM state WHERE key='semantic_rows_per_second'",
                [],
                |r| r.get::<_, String>(0)
            )
            .is_err());
        assert!(c
            .query_row(
                "SELECT value FROM state WHERE key LIKE 'semantic_cpu_cost_v1_%'",
                [],
                |r| r.get::<_, String>(0)
            )
            .is_err());
    }
    #[test]
    fn background_cpu_allowance_never_exceeds_half_the_machine() {
        for logical in [1, 2, 3, 6, 12, 64] {
            let cap = (logical / 2).max(1);
            for requested in [None, Some(0), Some(1), Some(6), Some(256)] {
                let actual = super::background::background_thread_count(logical, requested);
                assert!((1..=cap).contains(&actual));
            }
        }
        assert_eq!(super::background::background_thread_count(12, Some(3)), 3);
        assert_eq!(super::background::background_thread_count(12, Some(64)), 6);
    }
    use super::*;
    use background::drain_with;
    use rusqlite::params;
    use scan::PARALLEL_SCAN_ROWS;
    fn setup() -> (tempfile::TempDir, DataRoot, Options) {
        let t = tempfile::tempdir().unwrap();
        let r = DataRoot::open(t.path().join("data"), true).unwrap();
        store::writer(&r).unwrap();
        let o = Options {
            pattern: "connection pool timeout".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 10,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: true,
            tools: false,
            filters: Default::default(),
        };
        (t, r, o)
    }
    fn insert(r: &DataRoot, id: i64, family: &str, project: &str) {
        let c = store::writer(r).unwrap();
        let session = format!("session-{id}");
        c.execute("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES('codex',?,?, '',?,?,'',0,0)",params![session,project,family,id>1]).unwrap();
        let text = format!("connection timeout row {id}");
        c.execute("INSERT INTO prose(id,source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra) VALUES(?,1,'codex',?,?,1,'user','model','observed',?,?,?,'')",params![id,session,project,id,text.as_bytes(),store::digest(&text)]).unwrap();
    }
    fn embed(r: &DataRoot) {
        let p = semantic_store::batch(&store::reader(r).unwrap(), 128).unwrap();
        let mut v = vec![0f32; semantic_model::DIM];
        v[0] = 1.;
        let refs: Vec<_> = p.iter().map(|p| (p.key.clone(), 0)).collect();
        semantic_store::publish(r, &p, &refs, &vec![v; p.len()]).unwrap();
    }
    #[test]
    fn query_panic_keeps_its_cause_in_the_error_channel() {
        let (_t, root, options) = setup();
        let work = QueryWork {
            coverage: Coverage::default(),
            options,
            family_diverse: false,
            lane: Some(crate::logging::spawn_semantic(|| panic!("test inference panic")).unwrap()),
            joined: Default::default(),
        };
        let error = finish(&store::reader(&root).unwrap(), work).err().unwrap();
        assert!(format!("{error:#}").contains("test inference panic"));
    }
    #[test]
    fn semantic_filters_family_diversity_and_handles() {
        let (_t, r, mut o) = setup();
        insert(&r, 1, "root", "alpha");
        insert(&r, 2, "root", "alpha");
        insert(&r, 3, "other", "beta");
        embed(&r);
        let mut q = vec![0f32; semantic_model::DIM];
        q[0] = 1.;
        let c = store::reader(&r).unwrap();
        let diverse = search_vector(&r, &c, &o, true, &q).unwrap();
        assert_eq!(diverse.hits.len(), 2);
        assert!(diverse.hits[0].handle.starts_with("@session-"));
        assert_eq!(diverse.hits[0].matched, "semantic");
        assert_eq!(search_vector(&r, &c, &o, false, &q).unwrap().hits.len(), 3);
        o.filters.model = Some("MOD".into());
        o.filters.model_soft = true;
        assert_eq!(search_vector(&r, &c, &o, false, &q).unwrap().hits.len(), 3);
        o.filters.model = None;
        o.filters.project = Some("ALP*".into());
        assert_eq!(search_vector(&r, &c, &o, false, &q).unwrap().hits.len(), 2);
        o.filters.no_side = true;
        assert_eq!(search_vector(&r, &c, &o, false, &q).unwrap().hits.len(), 1);
        o.filters.since = Some(5);
        assert!(search_vector(&r, &c, &o, false, &q)
            .unwrap()
            .hits
            .is_empty());
    }
    #[test]
    fn semantic_caller_exclusion_precedes_the_result_limit() {
        let (_t, r, mut options) = setup();
        insert(&r, 1, "first", "alpha");
        insert(&r, 2, "second", "alpha");
        embed(&r);
        options.limit = 1;
        let c = store::reader(&r).unwrap();
        let caller = crate::caller::Caller {
            resolved: true,
            agent: "codex".into(),
            session: "session-2".into(),
            ..Default::default()
        };
        let mut q = vec![0.; semantic_model::DIM];
        q[0] = 1.;
        options = crate::caller::options(&options, &caller, false, false);
        let result = search_vector(&r, &c, &options, false, &q).unwrap();
        assert_eq!(result.hits.len(), 1);
        assert_eq!(result.hits[0].session, "session-1");
    }
    #[test]
    fn equal_scores_and_session_names_have_stable_agent_order() {
        let (_t, r, mut o) = setup();
        insert(&r, 1, "root", "alpha");
        insert(&r, 2, "other", "alpha");
        let c = store::writer(&r).unwrap();
        c.execute("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES('pi','session-1','alpha','','other',0,'',0,0)",[]).unwrap();
        c.execute("UPDATE prose SET ts=0", []).unwrap();
        c.execute(
            "UPDATE prose SET source=2,agent='pi',session='session-1' WHERE id=2",
            [],
        )
        .unwrap();
        embed(&r);
        let mut q = vec![0f32; semantic_model::DIM];
        q[0] = 1.;
        o.limit = 1;
        let result = search_vector(&r, &store::reader(&r).unwrap(), &o, false, &q).unwrap();
        assert_eq!(result.hits[0].agent, "codex");
    }
    #[test]
    fn deferred_semantic_resolution_uses_the_keyword_snapshot() {
        let (_t, r, o) = setup();
        insert(&r, 1, "root", "alpha");
        embed(&r);
        let c = store::reader(&r).unwrap();
        let mut q = vec![0f32; semantic_model::DIM];
        q[0] = 1.;
        let scan = VectorScan::new(semantic_store::blocks(&r, &c).unwrap(), &q).unwrap();
        store::writer(&r)
            .unwrap()
            .execute(
                "UPDATE prose SET text='new body',digest=? WHERE id=1",
                [store::digest("new body")],
            )
            .unwrap();
        let old = resolve_scan(&c, &o, true, &scan, None).unwrap();
        assert_eq!(old.hits[0].text, "connection timeout row 1");
        assert!(search_vector(&r, &store::reader(&r).unwrap(), &o, true, &q)
            .unwrap()
            .hits
            .is_empty());
    }
    #[test]
    fn stale_vectors_never_resolve_to_new_text() {
        let (_t, r, o) = setup();
        insert(&r, 1, "root", "alpha");
        embed(&r);
        store::writer(&r)
            .unwrap()
            .execute(
                "UPDATE prose SET text='changed',digest=? WHERE id=1",
                [store::digest("changed")],
            )
            .unwrap();
        let mut q = vec![0f32; semantic_model::DIM];
        q[0] = 1.;
        let c = store::reader(&r).unwrap();
        assert!(search_vector(&r, &c, &o, true, &q).unwrap().hits.is_empty());
        assert!(coverage(&c).unwrap().building());
    }
    #[test]
    fn queued_overlay_reuses_vectors_and_keeps_the_commands_snapshot() {
        let (_t, r, o) = setup();
        insert(&r, 1, "first", "alpha");
        embed(&r);
        insert(&r, 2, "second", "alpha");
        insert(&r, 3, "third", "alpha");
        let text = "connection timeout row 1";
        store::writer(&r)
            .unwrap()
            .execute(
                "UPDATE prose SET text=?,digest=? WHERE id=3",
                params![text.as_bytes(), store::digest(text)],
            )
            .unwrap();
        let c = store::reader(&r).unwrap();
        let overlay = Overlay::snapshot(&c, &coverage(&c).unwrap()).unwrap();
        assert_eq!(overlay.texts, ["connection timeout row 2"]);
        let mut vector = vec![0.; semantic_model::DIM];
        vector[0] = 1.;
        let scan = VectorScan::with_overlay(
            semantic_store::blocks(&r, &c).unwrap(),
            &vector,
            overlay,
            std::slice::from_ref(&vector),
        )
        .unwrap();
        store::writer(&r)
            .unwrap()
            .execute(
                "UPDATE prose SET text='edited',digest=? WHERE id=2",
                [store::digest("edited")],
            )
            .unwrap();
        let found = resolve_scan(&c, &o, false, &scan, None).unwrap();
        assert_eq!(found.hits.len(), 3);
        assert!(found
            .hits
            .iter()
            .any(|h| h.rowid == 2 && h.text == "connection timeout row 2"));
        assert!(found.hits.iter().any(|h| h.rowid == 3));
        assert_eq!(coverage(&store::reader(&r).unwrap()).unwrap().embedded, 1);
        assert_eq!(semantic_store::block_list(&c).unwrap().len(), 1);
    }
    #[test]
    fn semantic_snippet_is_the_rows_first_240_characters() {
        let (_t, r, mut o) = setup();
        insert(&r, 1, "root", "alpha");
        let text = format!("{} connection exception explanation", "x".repeat(1022));
        store::writer(&r)
            .unwrap()
            .execute(
                "UPDATE prose SET text=?,digest=? WHERE id=1",
                params![text.as_bytes(), store::digest(&text)],
            )
            .unwrap();
        let p = semantic_store::batch(&store::reader(&r).unwrap(), 1).unwrap();
        assert_eq!(p[0].chunks.len(), 2);
        let mut first = vec![0f32; semantic_model::DIM];
        first[1] = 1.;
        let mut last = vec![0f32; semantic_model::DIM];
        last[0] = 1.;
        semantic_store::publish(
            &r,
            &p,
            &[(p[0].key.clone(), 0), (p[0].key.clone(), 1)],
            &[first, last.clone()],
        )
        .unwrap();
        o.pattern = "database failure".into();
        let found = search_vector(&r, &store::reader(&r).unwrap(), &o, true, &last).unwrap();
        assert_eq!(found.hits[0].snippet, "x".repeat(240));
        assert_eq!(found.hits[0].text, text);
    }
    #[test]
    fn low_similarity_is_a_true_zero() {
        let (_t, r, o) = setup();
        insert(&r, 1, "root", "alpha");
        embed(&r);
        let mut q = vec![0f32; semantic_model::DIM];
        q[1] = 1.;
        assert!(search_vector(&r, &store::reader(&r).unwrap(), &o, true, &q)
            .unwrap()
            .hits
            .is_empty());
    }
    #[test]
    fn work_lock_is_exclusive_and_pending_order_is_newest_first() {
        let (_t, r, _o) = setup();
        insert(&r, 1, "root", "alpha");
        insert(&r, 2, "other", "beta");
        let lock = r.work_lock(false).unwrap().unwrap();
        assert!(r.work_lock(false).unwrap().is_none());
        drop(lock);
        assert!(r.work_lock(false).unwrap().is_some());
        assert_eq!(
            semantic_store::batch(&store::reader(&r).unwrap(), 1).unwrap()[0].id,
            2
        );
    }
    #[test]
    fn a_background_drain_yields_to_a_waiting_writer_before_it_loads_the_model() {
        let (_t, r, _o) = setup();
        insert(&r, 1, "root", "alpha");
        // Sparse files of the pinned sizes: an installed model whose weights fail
        // verification once a drain loads them.
        let model = semantic_model::directory(&r);
        std::fs::create_dir_all(&model).unwrap();
        for (name, size, _) in semantic_model::FILES {
            std::fs::File::create(model.join(name))
                .unwrap()
                .set_len(size)
                .unwrap();
        }
        // What `DataRoot::lock(true)` and a blocking `work_lock` hold while they wait.
        let queue = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(r.path().join("writer.queue"))
            .unwrap();
        queue.lock_shared().unwrap();
        let background = Drain {
            background: true,
            ..Default::default()
        };
        assert!(drain(&r, background).unwrap());
        assert!(semantic_store::pending(&store::reader(&r).unwrap()).unwrap());
        queue.unlock().unwrap();
        let error = drain(&r, background).unwrap_err();
        assert!(
            format!("{error:#}").contains("SHA-256 mismatch"),
            "{error:#}"
        );
    }
    #[test]
    fn a_capped_drain_embeds_only_the_newest_rows() {
        let (_t, r, _o) = setup();
        for id in 1..=3 {
            insert(&r, id, &format!("family-{id}"), "alpha");
        }
        let mut calls = 0;
        let capped = Drain {
            limit: Some(2),
            ..Default::default()
        };
        let yielded = drain_with(&r, capped, |texts| {
            calls += 1;
            let mut v = vec![0f32; semantic_model::DIM];
            v[0] = 1.;
            Ok(vec![v; texts.len()])
        })
        .unwrap();
        assert!(!yielded);
        assert_eq!(calls, 1);
        let c = store::reader(&r).unwrap();
        assert_eq!(coverage(&c).unwrap().embedded, 2);
        let left: Vec<i64> = semantic_store::batch(&c, 128)
            .unwrap()
            .iter()
            .map(|p| p.id)
            .collect();
        assert_eq!(left, [1]);
    }
    /// Equal cosines spread through the blocks of a parallel scan resolve to one order on
    /// every search: by cosine, then newest, then session and turn, one row a session.
    #[test]
    fn equal_cosines_across_parallel_blocks_keep_one_order() {
        const SESSIONS: usize = 64;
        const ROWS: usize = PARALLEL_SCAN_ROWS / SESSIONS + 8;
        let (_t, r, mut o) = setup();
        {
            let mut c = store::writer(&r).unwrap();
            let tx = c.transaction().unwrap();
            for s in 0..SESSIONS {
                let session = format!("session-{s:02}");
                tx.execute("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES('codex',?1,'alpha','',?1,0,'',0,0)", [&session]).unwrap();
                for turn in 0..ROWS {
                    let id = (s * ROWS + turn + 1) as i64;
                    let text = format!("connection timeout row {id}");
                    // Turns share three times a session; sessions differ by tens.
                    let ts = 1000 + (s % 4 * 10 + turn % 3) as i64;
                    tx.execute("INSERT INTO prose(id,source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES(?1,1,'codex',?2,'alpha',?3,'user','model','observed',?4,?5,?6,'',?3)", params![id, session, turn as i64, ts, text.as_bytes(), store::digest(&text)]).unwrap();
                }
            }
            tx.commit().unwrap();
        }
        // Each session's first 32 turns share one cosine above the floor; the newest-first
        // embedding order spreads them through the blocks.
        loop {
            let pending = semantic_store::batch(&store::reader(&r).unwrap(), 4096).unwrap();
            if pending.is_empty() {
                break;
            }
            let refs: Vec<_> = pending.iter().map(|p| (p.key.clone(), 0)).collect();
            let vectors: Vec<_> = pending
                .iter()
                .map(|p| {
                    let cosine = if (p.id as usize - 1) % ROWS < 32 {
                        0.9f32
                    } else {
                        0.5
                    };
                    let mut v = vec![0f32; semantic_model::DIM];
                    v[0] = cosine;
                    v[1 + p.id as usize % (semantic_model::DIM - 1)] =
                        (1. - cosine * cosine).sqrt();
                    v
                })
                .collect();
            semantic_store::publish(&r, &pending, &refs, &vectors).unwrap();
        }
        let c = store::reader(&r).unwrap();
        let blocks = semantic_store::blocks(&r, &c).unwrap();
        assert!(blocks.len() > 4);
        assert!(blocks.iter().map(|b| b.rows).sum::<usize>() >= PARALLEL_SCAN_ROWS);
        let mut q = vec![0f32; semantic_model::DIM];
        q[0] = 1.;
        o.chats = false;
        let mut ranked = |limit: usize| {
            o.limit = limit;
            search_vector(&r, &c, &o, false, &q)
                .unwrap()
                .hits
                .into_iter()
                .map(|h| (h.session, h.turn, h.score))
                .collect::<Vec<_>>()
        };
        let first = ranked(SESSIONS);
        let mut expected: Vec<_> = (0..SESSIONS)
            .map(|s| (3 - s % 4, format!("session-{s:02}")))
            .collect();
        expected.sort();
        // A session's head is its first turn at its newest time.
        assert_eq!(
            first
                .iter()
                .map(|(session, turn, _)| (session.as_str(), *turn))
                .collect::<Vec<_>>(),
            expected
                .iter()
                .map(|(_, session)| (session.as_str(), 2))
                .collect::<Vec<_>>()
        );
        assert!(first.iter().all(|(_, _, score)| *score == first[0].2));
        for _ in 0..3 {
            assert_eq!(ranked(SESSIONS), first);
        }
        assert_eq!(ranked(10), first[..10]);
    }
}
