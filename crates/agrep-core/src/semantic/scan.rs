//! Scanning q8 vector blocks for candidates, then resolving them to filtered rows.
use super::budget::Overlay;
use crate::{
    query::{self, Hit, Options, Results},
    semantic_policy, semantic_store, store,
};
use anyhow::{bail, Result};
use rusqlite::{params, Connection};
use std::{
    cmp::Ordering as Order,
    collections::{HashMap, HashSet},
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
            eprintln!(
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
fn read_hit(r: &rusqlite::Row<'_>) -> rusqlite::Result<Hit> {
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
        score: 0.,
        matched: "semantic".into(),
        ..Default::default()
    })
}
/// The page order for `sort`, with a total tie-break.
fn order(sort: &str, a: &Hit, b: &Hit) -> Order {
    let order = match sort {
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
}
/// The retained page: the best row of each session (or family), one past the limit.
struct Page<'a> {
    hits: Vec<Hit>,
    groups: HashSet<(String, String)>,
    want: usize,
    sort: &'a str,
    family_diverse: bool,
}
impl Page<'_> {
    /// A full score-ordered page holds one row past the limit, the score to beat.
    fn full(&self) -> bool {
        self.sort == "score" && self.hits.len() > self.want
    }
    fn offer(&mut self, mut h: Hit) -> Result<()> {
        let family_diverse = self.family_diverse;
        let group = (
            h.agent.clone(),
            if family_diverse && !h.family.is_empty() {
                h.family.clone()
            } else {
                h.session.clone()
            },
        );
        self.groups.insert(group);
        let same = |old: &Hit| {
            old.agent == h.agent
                && (old.session == h.session
                    || (family_diverse && !h.family.is_empty() && old.family == h.family))
        };
        if let Some(at) = self.hits.iter().position(same) {
            if order(self.sort, &h, &self.hits[at]).is_ge() {
                return Ok(());
            }
            self.hits.remove(at);
        }
        if self.hits.len() > self.want && order(self.sort, &h, self.hits.last().unwrap()).is_ge() {
            return Ok(());
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
        self.hits.push(h);
        let sort = self.sort;
        self.hits.sort_by(|a, b| order(sort, a, b));
        self.hits.truncate(self.want + 1);
        Ok(())
    }
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
    let mut resolve=c.prepare("SELECT p.id,p.session,p.agent,p.project,p.position,p.who,p.model,p.model_source,p.ts,p.text,s.family,s.side FROM semantic_chunks v JOIN semantic_refs r ON r.content_key=v.content_key JOIN prose p ON p.id=r.row_id AND p.digest=r.digest JOIN sessions s ON s.id=p.session AND s.agent=p.agent WHERE v.block=? AND v.slot=? AND p.visible=1")?;
    let mut resolve_overlay = if overlay.is_empty() {
        None
    } else {
        Some(c.prepare("SELECT p.id,p.session,p.agent,p.project,p.position,p.who,p.model,p.model_source,p.ts,p.text,s.family,s.side FROM prose p JOIN sessions s ON s.id=p.session AND s.agent=p.agent WHERE p.id=? AND p.visible=1")?)
    };
    let mut page = Page {
        hits: Vec::new(),
        groups: HashSet::new(),
        want,
        sort: &o.sort,
        family_diverse,
    };
    let mut exhausted = true;
    for &(block, slot, upper, score) in candidates {
        if page.full() && upper < page.hits[want].score as f32 {
            exhausted = false;
            break;
        }
        refined += 1;
        if (score as f64) < semantic_policy::FLOOR {
            continue;
        }
        if page.full() && (score as f64) < page.hits[want].score {
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
            let mut h = row?;
            h.score = score as f64;
            h.meta_marker = crate::query::meta_role(&h.who, h.turn);
            if !matches_filters(&h, o)
                || (o.filters.no_meta && crate::history_meta::is_meta(c, &h, &o.pattern)?)
            {
                continue;
            }
            page.offer(h)?;
        }
    }
    if crate::semantic_profile::timing_enabled() {
        eprintln!(
            "{}",
            serde_json::json!({"refine_resolve_ms":started.elapsed().as_secs_f64()*1000.,"refined":refined,"resolved":resolved,"prefetch":stop.is_some()})
        );
    }
    result.total = page.groups.len();
    let mut hits = page.hits;
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
