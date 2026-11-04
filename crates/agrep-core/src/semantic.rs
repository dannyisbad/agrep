//! In-process semantic retrieval and short-lived background queue draining.
use crate::{
    data::DataRoot,
    query::{self, Hit, Options, Results},
    semantic_model::{self, Embedder},
    semantic_policy, semantic_store, store,
};
use anyhow::{bail, Result};
use rusqlite::{params, Connection};
pub use semantic_model::{installed, provision};
pub use semantic_policy::{eligible, FLOOR, STRONG};
pub use semantic_store::{coverage, reclaim, reset, Coverage};
use std::{
    collections::HashSet,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};

pub struct SearchResults {
    pub result: Results,
    pub coverage: Coverage,
}

fn background_thread_count(logical: usize, requested: Option<usize>) -> usize {
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
fn drain_with(
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
        let vectors = if texts.is_empty() {
            Vec::new()
        } else {
            embed(&texts)?
        };
        semantic_store::publish(root, &pending, &refs, &vectors)?;
        if let Some(n) = left.as_mut() {
            *n -= pending.len();
        }
        std::thread::yield_now();
    }
    Ok(false)
}
/// Scans of fewer vectors than this stay on one thread; at owner scale the scan is
/// split across the Rayon pool.
const PARALLEL_SCAN_ROWS: usize = 32_768;
struct VectorScan {
    names: Vec<String>,
    /// (block, slot, upper bound, exact cosine) in descending bound order.
    candidates: Vec<(usize, usize, f32, f32)>,
    total_rows: usize,
}
impl VectorScan {
    fn new(blocks: Vec<semantic_store::Block>, vector: &[f32]) -> Result<Self> {
        let _span = crate::semantic_profile::span("vector_scan_and_sort");
        use rayon::prelude::*;
        let started = std::time::Instant::now();
        let (q, scale) = semantic_store::quantize(vector)?;
        let total_rows = blocks.iter().map(|b| b.rows).sum();
        // A small scan runs on the calling thread rather than waiting for pool workers.
        let parallel = total_rows >= PARALLEL_SCAN_ROWS;
        let scan = |(b, block): (usize, &semantic_store::Block)| {
            block
                .candidates(&q, scale, vector, semantic_policy::FLOOR as f32)
                .into_iter()
                .map(move |(slot, upper, exact)| (b, slot, upper, exact))
        };
        let mut candidates: Vec<_> = {
            let _span = crate::semantic_profile::span("vector_scan");
            if parallel {
                blocks.par_iter().enumerate().flat_map_iter(scan).collect()
            } else {
                blocks.iter().enumerate().flat_map(scan).collect()
            }
        };
        let scan_ms = started.elapsed().as_secs_f64() * 1000.;
        {
            let _span = crate::semantic_profile::span("vector_sort");
            let order = |a: &(usize, usize, f32, f32), b: &(usize, usize, f32, f32)| {
                b.2.total_cmp(&a.2).then(a.0.cmp(&b.0)).then(a.1.cmp(&b.1))
            };
            if parallel {
                candidates.par_sort_unstable_by(order);
            } else {
                candidates.sort_unstable_by(order);
            }
        }
        if crate::semantic_profile::timing_enabled() {
            eprintln!(
                "{}",
                serde_json::json!({"scan_ms":scan_ms,"candidate_sort_ms":started.elapsed().as_secs_f64()*1000.-scan_ms,"candidates":candidates.len(),"vectors":total_rows})
            );
        }
        Ok(Self {
            names: blocks.into_iter().map(|b| b.name).collect(),
            candidates,
            total_rows,
        })
    }
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
        let _span = crate::semantic_profile::span("semantic_coverage");
        coverage(c)?
    };
    let joined = Arc::new(AtomicBool::new(false));
    let lane = if coverage.embedded == 0 {
        None
    } else {
        let listed = {
            let _span = crate::semantic_profile::span("vector_block_list");
            semantic_store::block_list(c)?
        };
        let root = root.clone();
        let pattern = o.pattern.clone();
        let options = o.clone();
        let waiting = joined.clone();
        Some(crate::logging::spawn_semantic(move || {
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
                let vector =
                    Embedder::query(&root).and_then(|model| Ok(model.embed(&[pattern])?.remove(0)));
                let blocks = opened.join().map_err(crate::logging::panic_error)?;
                Ok((blocks, vector))
            })?;
            let scan = VectorScan::new(blocks?, &vector?)?;
            // While the keyword lane still runs, the same resolution on this thread's
            // own snapshot pages in the rows `finish` reads; its result is discarded.
            if !waiting.load(Ordering::Relaxed) {
                if let Ok(reader) = store::reader(&root) {
                    let _span = crate::semantic_profile::span("vector_prefetch");
                    let _ = resolve_scan(&reader, &options, family_diverse, &scan, Some(&waiting));
                }
            }
            Ok(scan)
        })?)
    };
    Ok(QueryWork {
        coverage,
        options: o.clone(),
        family_diverse,
        lane,
        joined,
    })
}
pub fn finish(c: &Connection, work: QueryWork) -> Result<SearchResults> {
    let mut result = if let Some(lane) = work.lane {
        work.joined.store(true, Ordering::Relaxed);
        let scan = lane.join().map_err(crate::logging::panic_error)??;
        let _span = crate::semantic_profile::span("vector_refine");
        resolve_scan(c, &work.options, work.family_diverse, &scan, None)?
    } else {
        let mut r = Results::default();
        r.total_basis = "exact".into();
        r
    };
    if work.coverage.partial() {
        result.total_basis = "floor".into();
    }
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
    let partial = coverage.partial();
    // py/surface_policy.py:2839-2852: complete, or within the 1% live-update tail.
    let usable = !partial
        || (coverage.total > 0 && coverage.embedded as f64 >= coverage.total as f64 * 0.99);
    serde_json::json!({"state":if found {"ready"} else {"no-confident-match"},"eligible":true,"reason":null,"min_score":FLOOR,"score_kind":"cosine","coverage":coverage,"partial":partial,"complete":!partial,"fallback_recommended":!found && !usable})
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
    let cause: String = crate::surface::safe(&format!("{error:#}"), false)
        .chars()
        .take(140)
        .collect();
    serde_json::json!({"state":"unavailable","eligible":true,"reason":cause.trim(),"min_score":FLOOR,"score_kind":"unavailable","coverage":null,"partial":true,"complete":false,"fallback_recommended":true})
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
/// `stop` ends a speculative resolution before its next row lookup.
fn resolve_scan(
    c: &Connection,
    o: &Options,
    family_diverse: bool,
    scan: &VectorScan,
    stop: Option<&AtomicBool>,
) -> Result<Results> {
    let started = std::time::Instant::now();
    let (mut refined, mut resolved) = (0usize, 0usize);
    let VectorScan {
        names,
        candidates,
        total_rows,
    } = scan;
    let want = if o.limit == 0 { 200 } else { o.limit.min(200) };
    let mut result = Results::default();
    let mut resolve=c.prepare("SELECT p.id,p.session,p.agent,p.project,p.position,p.who,p.model,p.model_source,p.ts,p.text,s.family,s.side FROM semantic_chunks v JOIN semantic_refs r ON r.content_key=v.content_key JOIN prose p ON p.id=r.row_id AND p.digest=r.digest JOIN sessions s ON s.id=p.session AND s.agent=p.agent WHERE v.block=? AND v.slot=? AND p.visible=1")?;
    let mut hits = Vec::<Hit>::new();
    let mut matched_groups = HashSet::new();
    let compare = |a: &Hit, b: &Hit| {
        let order = match o.sort.as_str() {
            "time" => b.ts.cmp(&a.ts).then(b.score.total_cmp(&a.score)),
            "position" => a
                .session
                .cmp(&b.session)
                .then(a.turn.cmp(&b.turn))
                .then(b.score.total_cmp(&a.score)),
            _ => b
                .score
                .total_cmp(&a.score)
                .then(b.ts.cmp(&a.ts))
                .then(a.session.cmp(&b.session))
                .then(a.turn.cmp(&b.turn)),
        };
        order
            .then(a.agent.cmp(&b.agent))
            .then(a.session.cmp(&b.session))
            .then(a.turn.cmp(&b.turn))
            .then(a.who.cmp(&b.who))
            .then(a.rowid.cmp(&b.rowid))
    };
    let mut exhausted = true;
    for &(block, slot, upper, score) in candidates {
        if o.sort == "score" && hits.len() > want && upper < hits[want].score as f32 {
            exhausted = false;
            break;
        }
        refined += 1;
        if (score as f64) < semantic_policy::FLOOR {
            continue;
        }
        if o.sort == "score" && hits.len() > want && (score as f64) < hits[want].score {
            // This exact cosine cannot enter the retained page, regardless of
            // metadata filters. Avoid fetching a long source row just to reject it.
            exhausted = false;
            continue;
        }
        if stop.is_some_and(|s| s.load(Ordering::Relaxed)) {
            break;
        }
        resolved += 1;
        let rows = resolve.query_map(params![names[block], slot], |r| {
            Ok(Hit {
                rowid: r.get(0)?,
                session: r.get(1)?,
                agent: r.get(2)?,
                project: r.get(3)?,
                turn: r.get(4)?,
                who: r.get(5)?,
                model: r.get(6)?,
                model_source: r.get(7)?,
                ts: r.get(8)?,
                text: store::decode("prose", r.get::<_, Vec<u8>>(9)?).map_err(|e| {
                    rusqlite::Error::FromSqlConversionFailure(
                        9,
                        rusqlite::types::Type::Blob,
                        e.into_boxed_dyn_error(),
                    )
                })?,
                family: r.get(10)?,
                side: r.get(11)?,
                score: score as f64,
                matched: "semantic".into(),
                ..Default::default()
            })
        })?;
        for row in rows {
            let mut h = row?;
            h.meta_marker = crate::query::meta_role(&h.who, h.turn);
            if !matches_filters(&h, o)
                || (o.filters.no_meta && crate::history_meta::is_meta(c, &h, &o.pattern)?)
            {
                continue;
            }
            let group = (
                h.agent.clone(),
                if family_diverse && !h.family.is_empty() {
                    h.family.clone()
                } else {
                    h.session.clone()
                },
            );
            matched_groups.insert(group);
            let same = |old: &Hit| {
                old.agent == h.agent
                    && (old.session == h.session
                        || (family_diverse && !h.family.is_empty() && old.family == h.family))
            };
            if let Some(at) = hits.iter().position(same) {
                if compare(&h, &hits[at]).is_ge() {
                    continue;
                }
                hits.remove(at);
            }
            if hits.len() > want && compare(&h, hits.last().unwrap()).is_ge() {
                continue;
            }
            h.handle = format!(
                "{}:{}.{}",
                session_target(&h.session)?,
                h.turn,
                query::content_digest(&h.text)
            );
            // 0.4.9's meaning rows showed their text's first 240 characters with whitespace
            // runs (Python's, which counts \x1c-\x1f) folded to one space; compact pages
            // then narrow that around a keyword match.
            h.snippet = h
                .text
                .split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                .filter(|word| !word.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .take(240)
                .collect();
            hits.push(h);
            hits.sort_by(compare);
            hits.truncate(want + 1);
        }
    }
    if crate::semantic_profile::timing_enabled() {
        eprintln!(
            "{}",
            serde_json::json!({"refine_resolve_ms":started.elapsed().as_secs_f64()*1000.,"refined":refined,"resolved":resolved,"prefetch":stop.is_some()})
        );
    }
    result.total = matched_groups.len();
    hits.truncate(want);
    result.hits = hits;
    result.total_basis = if exhausted { "exact" } else { "floor" }.into();
    result.examined = *total_rows;
    result.blocks = names.len();
    Ok(result)
}

fn project_matches(value: &str, pattern: &str) -> bool {
    let v = value.to_lowercase();
    let p = pattern.to_lowercase();
    if p.contains(['*', '?', '[']) {
        globset::GlobBuilder::new(&p)
            .literal_separator(false)
            .build()
            .is_ok_and(|g| g.compile_matcher().is_match(&v))
    } else {
        v == p || v.rsplit(['/', '\\']).next() == Some(p.as_str())
    }
}
fn matches_filters(h: &Hit, o: &Options) -> bool {
    let f = &o.filters;
    if f.who.is_none() && ["control", "synthetic", "recap", "harness"].contains(&h.who.as_str()) {
        return false;
    }
    query::caller_allows(f, h)
        && !f
            .agent
            .as_ref()
            .is_some_and(|s| !s.split(',').any(|a| a == h.agent))
        && !f
            .chat
            .as_ref()
            .is_some_and(|s| !h.session.starts_with(s.trim_start_matches('@')))
        && !f
            .project
            .as_ref()
            .is_some_and(|s| !project_matches(&h.project, s))
        && !f
            .exclude_project
            .as_ref()
            .is_some_and(|s| project_matches(&h.project, s))
        && !f
            .who
            .as_ref()
            .is_some_and(|s| !s.split(',').any(|w| w == h.who))
        && !f
            .no_who
            .as_ref()
            .is_some_and(|s| s.split(',').any(|w| w == h.who))
        && !(f.no_meta && ["control", "harness", "synthetic"].contains(&h.who.as_str()))
        && !(f.no_side && h.side)
        && !f.since.is_some_and(|ts| h.ts < ts)
        && !f.until.is_some_and(|ts| h.ts >= ts)
        && !f.model.as_ref().is_some_and(|m| {
            if f.model_soft {
                !h.model
                    .to_ascii_lowercase()
                    .contains(&m.to_ascii_lowercase())
            } else {
                h.model != *m
            }
        })
}
fn session_target(session: &str) -> Result<String> {
    if (3..=128).contains(&session.len())
        && session.as_bytes()[0].is_ascii_alphanumeric()
        && session
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return Ok(format!("@{session}"));
    }
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let (mut token, mut bits, mut available) = (String::new(), 0u32, 0);
    for b in session.bytes() {
        bits = (bits << 8) | b as u32;
        available += 8;
        while available >= 6 {
            available -= 6;
            token.push(A[((bits >> available) & 63) as usize] as char);
        }
    }
    if available != 0 {
        token.push(A[((bits << (6 - available)) & 63) as usize] as char);
    }
    if token.is_empty() || token.len() > 1024 {
        bail!("session id cannot be encoded safely");
    }
    Ok(format!("@~{token}"))
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

#[cfg(test)]
mod tests {
    #[test]
    fn background_cpu_allowance_never_exceeds_half_the_machine() {
        for logical in [1, 2, 3, 6, 12, 64] {
            let cap = (logical / 2).max(1);
            for requested in [None, Some(0), Some(1), Some(6), Some(256)] {
                let actual = super::background_thread_count(logical, requested);
                assert!((1..=cap).contains(&actual));
            }
        }
        assert_eq!(super::background_thread_count(12, Some(3)), 3);
        assert_eq!(super::background_thread_count(12, Some(64)), 6);
    }
    use super::*;
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
        assert!(coverage(&c).unwrap().partial());
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
