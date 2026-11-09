//! Scanning q8 vector blocks for candidates, then resolving them to filtered rows.
use super::{budget::Overlay, page::Page};
use crate::{
    query::{self, Hit, Options, Results},
    semantic_policy, semantic_store, store,
};
use anyhow::Result;
use rusqlite::{params, Connection};
use std::{
    collections::HashMap,
    sync::atomic::{AtomicBool, Ordering},
};

/// Scans of fewer vectors than this stay on one thread; at owner scale the scan is
/// split across the Rayon pool.
pub(super) const PARALLEL_SCAN_ROWS: usize = 32_768;
pub(super) struct VectorScan {
    names: Vec<String>,
    /// (block, slot, upper bound, exact cosine) in descending bound order.
    candidates: Vec<(usize, usize, f32, f32)>,
    total_rows: usize,
    overlay: HashMap<(usize, usize), Vec<i64>>,
}
impl VectorScan {
    pub fn with_overlay(
        mut blocks: Vec<semantic_store::Block>,
        vector: &[f32],
        overlay: Overlay,
        extra: &[Vec<f32>],
    ) -> Result<Self> {
        if overlay.rows.is_empty() && extra.is_empty() {
            return Self::new(blocks, vector);
        }
        if !extra.is_empty() {
            blocks.push(semantic_store::Block::memory(extra)?);
        }
        let mut scan = Self::new(blocks, vector)?;
        let names: HashMap<_, _> = scan
            .names
            .iter()
            .enumerate()
            .map(|(i, n)| (n.as_str(), i))
            .collect();
        for ((name, slot), rows) in overlay.rows {
            let block = *names
                .get(name.as_str())
                .ok_or_else(|| anyhow::anyhow!("missing semantic overlay block"))?;
            scan.overlay.insert((block, slot), rows);
        }
        Ok(scan)
    }
    pub fn new(blocks: Vec<semantic_store::Block>, vector: &[f32]) -> Result<Self> {
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
            crate::errln!(
                "{}",
                serde_json::json!({"scan_ms":scan_ms,"candidate_sort_ms":started.elapsed().as_secs_f64()*1000.-scan_ms,"candidates":candidates.len(),"vectors":total_rows})
            );
        }
        Ok(Self {
            names: blocks.into_iter().map(|b| b.name).collect(),
            candidates,
            total_rows,
            overlay: HashMap::new(),
        })
    }
}
/// A resolved row, and when its chat was last active.
fn read_hit(r: &rusqlite::Row<'_>) -> rusqlite::Result<(Hit, i64)> {
    let hit = Hit {
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
        score: 0.,
        matched: "semantic".into(),
        ..Default::default()
    };
    Ok((hit, r.get(12)?))
}
/// `stop` ends a speculative resolution before its next row lookup.
pub(super) fn resolve_scan(
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
        overlay,
    } = scan;
    let want = if o.limit == 0 { 200 } else { o.limit.min(200) };
    let mut result = Results::default();
    let mut resolve=c.prepare("SELECT p.id,p.session,p.agent,p.project,p.position,p.who,p.model,p.model_source,p.ts,p.text,s.family,s.side,s.last_ts FROM semantic_chunks v JOIN semantic_refs r ON r.content_key=v.content_key JOIN prose p ON p.id=r.row_id AND p.digest=r.digest JOIN sessions s ON s.id=p.session AND s.agent=p.agent WHERE v.block=? AND v.slot=? AND p.visible=1")?;
    let mut resolve_overlay = if overlay.is_empty() {
        None
    } else {
        Some(c.prepare("SELECT p.id,p.session,p.agent,p.project,p.position,p.who,p.model,p.model_source,p.ts,p.text,s.family,s.side,s.last_ts FROM prose p JOIN sessions s ON s.id=p.session AND s.agent=p.agent WHERE p.id=? AND p.visible=1")?)
    };
    // A chat listing names every chat, so only row pages fold copies of a row.
    let mut page = Page::new(want, &o.sort, family_diverse, !o.chats);
    let mut exhausted = true;
    for &(block, slot, upper, score) in candidates {
        if page.full() && upper < page.score_to_beat() as f32 {
            exhausted = false;
            break;
        }
        refined += 1;
        if (score as f64) < semantic_policy::FLOOR {
            continue;
        }
        if page.full() && (score as f64) < page.score_to_beat() {
            // This exact cosine cannot enter the retained page, regardless of
            // metadata filters. Avoid fetching a long source row just to reject it.
            exhausted = false;
            continue;
        }
        if stop.is_some_and(|s| s.load(Ordering::Relaxed)) {
            break;
        }
        resolved += 1;
        let rows = resolve.query_map(params![names[block], slot], read_hit)?;
        let mut extra = Vec::new();
        if let Some(ids) = overlay.get(&(block, slot)) {
            let resolve_overlay = resolve_overlay.as_mut().unwrap();
            for id in ids {
                extra.push(resolve_overlay.query_row([id], read_hit));
            }
        }
        for row in rows.chain(extra) {
            let (mut h, last) = row?;
            h.score = score as f64;
            h.meta_marker = crate::query::meta_role(&h.who, h.turn);
            if !matches_filters(&h, o)
                || (o.filters.no_meta && crate::history_meta::is_meta(c, &h, &o.pattern)?)
            {
                continue;
            }
            page.offer(h, last)?;
        }
    }
    if crate::semantic_profile::timing_enabled() {
        crate::errln!(
            "{}",
            serde_json::json!({"refine_resolve_ms":started.elapsed().as_secs_f64()*1000.,"refined":refined,"resolved":resolved,"prefetch":stop.is_some()})
        );
    }
    result.total = page.groups.len();
    result.hits = page.into_hits();
    result.total_basis = if exhausted { "exact" } else { "floor" }.into();
    result.examined = *total_rows;
    result.blocks = names.len();
    Ok(result)
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
        && !f.chat.as_ref().is_some_and(|s| {
            !h.session.starts_with(s.trim_start_matches('@'))
                && !f
                    .inherited
                    .iter()
                    .any(|link| link.session == h.session && h.ts <= link.until)
        })
        && !f
            .project
            .as_ref()
            .is_some_and(|s| !crate::history::project_matches(&h.project, s))
        && !f
            .exclude_project
            .as_ref()
            .is_some_and(|s| crate::history::project_matches(&h.project, s))
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
