//! Whether a query can embed its snapshot's queued rows inline within its time budget;
//! otherwise how far the meaning index's build has come.
use crate::{
    data::DataRoot,
    semantic_model::{self, Embedder},
    semantic_store::{self, Coverage},
    store,
};
use anyhow::{bail, Result};
use rusqlite::{params, Connection, OptionalExtension};
use std::collections::HashMap;

const INLINE_SECONDS: f64 = 1.;
const MIN_INLINE_UNITS: f64 = 0.25;
/// Queued chunks an uncalibrated query prepares before its probe measures the machine.
const PROBE_PREPARATION: usize = 32;

#[derive(Clone, Copy, serde::Serialize, serde::Deserialize)]
pub(super) struct QueryCost {
    pub load: f64,
    pub cpu_unit: f64,
    pub span_unit: f64,
    pub updated: u64,
}
impl QueryCost {
    fn key() -> String {
        format!(
            "semantic_query_cost_v2_{}_{}_{}",
            std::env::consts::OS,
            std::env::consts::ARCH,
            Embedder::inline_streams()
        )
    }
    pub fn now() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs()
    }
    pub fn read(c: &Connection) -> Option<Self> {
        let raw: String = c
            .query_row("SELECT value FROM state WHERE key=?", [Self::key()], |r| {
                r.get(0)
            })
            .ok()?;
        let cost: Self = serde_json::from_str(&raw).ok()?;
        (cost.load.is_finite()
            && cost.load >= 0.
            && cost.cpu_unit.is_finite()
            && cost.cpu_unit > 0.
            && cost.span_unit.is_finite()
            && cost.span_unit > 0.
            && Self::now().saturating_sub(cost.updated) < 60)
            .then_some(cost)
    }
    fn seconds(self, units: f64, largest: f64) -> f64 {
        // CPU work accounts for the core allowance; the longest sequence still
        // has a serial critical path. Headroom covers contention and scheduling.
        (units * self.cpu_unit / Embedder::inline_streams() as f64).max(largest * self.span_unit)
            * 1.5
    }
    pub fn fits(self, units: f64, largest: f64, elapsed: f64) -> bool {
        elapsed + self.seconds(units, largest) <= INLINE_SECONDS * 0.85
    }
    /// How many queued chunks a query decodes before admission. A recorded cost never bounds
    /// it below what an uncalibrated query prepares: a refusal records nothing, so a bound
    /// from one slow observation would otherwise refuse every query until it expires.
    pub fn preparation_limit(cost: Option<Self>) -> usize {
        cost.map_or(PROBE_PREPARATION, |c| {
            (((INLINE_SECONDS * 0.85 - c.load).max(0.) * Embedder::inline_streams() as f64
                / c.cpu_unit
                / MIN_INLINE_UNITS
                / 1.5) as usize)
                .clamp(PROBE_PREPARATION, 4096)
        })
    }
    pub fn observe(root: &DataRoot, load: f64, batch: &semantic_model::InlineBatch) {
        if crate::data::read_only()
            || !load.is_finite()
            || !batch.cpu_unit.is_finite()
            || batch.cpu_unit <= 0.
            || !batch.span_unit.is_finite()
            || batch.span_unit <= 0.
        {
            return;
        }
        let save = || -> Result<()> {
            // Calibration must never add a wait behind an index writer.
            let Some(_lock) = root.lock(false)? else {
                return Ok(());
            };
            let c = store::writer(root)?;
            let blend = |old: f64, new: f64| new.max(old * 0.75 + new * 0.25);
            let old = Self::read(&c);
            let cost = Self {
                load: old.map_or(load, |v| blend(v.load, load)),
                cpu_unit: old.map_or(batch.cpu_unit, |v| blend(v.cpu_unit, batch.cpu_unit)),
                span_unit: old.map_or(batch.span_unit, |v| {
                    if batch.vectors.len() == 1 {
                        blend(v.span_unit, batch.span_unit)
                    } else {
                        v.span_unit
                    }
                }),
                updated: Self::now(),
            };
            c.execute("INSERT INTO state VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                params![Self::key(), serde_json::to_string(&cost)?])?;
            Ok(())
        };
        let _ = save();
    }
}

/// The query model as inline admission uses it.
pub(super) trait InlineModel {
    /// Seconds since the lane started, model loading included.
    fn elapsed(&self) -> f64;
    /// Seconds the model took to load.
    fn load(&self) -> f64;
    fn work_units(&self, texts: &[String]) -> Result<f64>;
    /// Embeds `texts` and records the measurement for later queries.
    fn embed(&self, texts: &[String]) -> Result<semantic_model::InlineBatch>;
}
/// The vectors of a snapshot's queued chunks, or None when they do not fit the inline
/// budget. A recorded cost that admits them all is trusted. Otherwise the first chunk is
/// embedded, as an uncalibrated query probes, and the rest is judged by this command's own
/// measurement: a recorded slowdown is re-measured instead of refusing until it expires.
pub(super) fn embed_queued(
    model: &impl InlineModel,
    queued: &[String],
    recorded: Option<QueryCost>,
) -> Result<Option<Vec<Vec<f32>>>> {
    if queued.is_empty() {
        return Ok(Some(Vec::new()));
    }
    let weights = queued
        .iter()
        .map(|text| model.work_units(std::slice::from_ref(text)))
        .collect::<Result<Vec<_>>>()?;
    let fits = |cost: QueryCost, weights: &[f64]| {
        cost.fits(
            weights.iter().sum(),
            weights.iter().copied().fold(0., f64::max),
            model.elapsed(),
        )
    };
    if recorded.is_some_and(|cost| fits(cost, &weights)) {
        return Ok(Some(model.embed(queued)?.vectors));
    }
    let probe = model.embed(&queued[..1])?;
    let measured = QueryCost {
        load: model.load(),
        cpu_unit: probe.cpu_unit,
        span_unit: probe.span_unit,
        updated: QueryCost::now(),
    };
    let mut vectors = probe.vectors;
    if queued.len() > 1 {
        if !fits(measured, &weights[1..]) {
            return Ok(None);
        }
        vectors.extend(model.embed(&queued[1..])?.vectors);
    }
    Ok(Some(vectors))
}

#[derive(Debug, serde::Serialize)]
pub struct BuildProgress {
    pub indexed_rows: usize,
    pub total_rows: usize,
    pub remaining_rows: usize,
    /// None until a drain on this machine measured how fast rows embed (a rebuild keeps the
    /// rate the index it replaces measured).
    pub estimated_seconds: Option<u64>,
}
pub fn build_progress(c: &Connection, coverage: &Coverage) -> Result<BuildProgress> {
    let rate = c
        .query_row(
            "SELECT value FROM state WHERE key='semantic_rows_per_second'",
            [],
            |r| r.get::<_, String>(0),
        )
        .optional()?
        .and_then(|v| v.parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v > 0.);
    let remaining = coverage.total.saturating_sub(coverage.embedded);
    Ok(BuildProgress {
        indexed_rows: coverage.embedded,
        total_rows: coverage.total,
        remaining_rows: remaining,
        estimated_seconds: rate.map(|rate| (remaining as f64 / rate).ceil() as u64),
    })
}
/// A duration as the build's estimates say it: `1h 5m`, `15m 19s`, `42s`.
pub(super) fn time_left(seconds: u64) -> String {
    if seconds >= 3600 {
        format!("{}h {}m", seconds / 3600, seconds % 3600 / 60)
    } else if seconds >= 60 {
        format!("{}m {}s", seconds / 60, seconds % 60)
    } else {
        format!("{seconds}s")
    }
}
/// A build's time left as `status` and an unavailable `-s` say it: `about 15m 19s left`, or
/// `estimating time left` while no rate has been measured.
pub fn estimate(seconds: Option<u64>) -> String {
    match seconds {
        Some(seconds) => format!("about {} left", time_left(seconds)),
        None => "estimating time left".to_owned(),
    }
}
impl std::fmt::Display for BuildProgress {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "the meaning index is still building ({} of {} rows, {})",
            crate::surface::number(self.indexed_rows),
            crate::surface::number(self.total_rows),
            estimate(self.estimated_seconds)
        )
    }
}
impl std::error::Error for BuildProgress {}
pub fn is_building(error: &anyhow::Error) -> bool {
    error.downcast_ref::<BuildProgress>().is_some()
}

#[derive(Default)]
pub(super) struct Overlay {
    pub texts: Vec<String>,
    // Each vector can serve several identical rows, including rows whose vector
    // was published before their reference was queued in this snapshot.
    pub rows: HashMap<(String, usize), Vec<i64>>,
}
impl Overlay {
    pub fn snapshot(c: &Connection, coverage: &Coverage) -> Result<Self> {
        let pending = coverage.total.saturating_sub(coverage.embedded);
        if pending == 0 {
            return Ok(Self::default());
        }
        let limit = QueryCost::preparation_limit(QueryCost::read(c));
        if pending > limit {
            return Err(build_progress(c, coverage)?.into());
        }
        let mut out = Self::default();
        let mut seen = HashMap::<String, Vec<(String, usize)>>::new();
        let mut known = c.prepare(
            "SELECT block,slot FROM semantic_chunks WHERE content_key=? ORDER BY ordinal",
        )?;
        let queued = semantic_store::batch(c, pending)?;
        if queued.len() != pending {
            bail!("meaning snapshot has unqueued rows without vectors");
        }
        for p in queued {
            let slots = if let Some(slots) = seen.get(&p.key) {
                slots.clone()
            } else {
                let mut slots = known
                    .query_map([&p.key], |r| Ok((r.get(0)?, r.get(1)?)))?
                    .collect::<rusqlite::Result<Vec<(String, usize)>>>()?;
                if slots.is_empty() {
                    for text in p.chunks {
                        slots.push((String::new(), out.texts.len()));
                        out.texts.push(text);
                    }
                }
                seen.insert(p.key, slots.clone());
                slots
            };
            for slot in slots {
                out.rows.entry(slot).or_default().push(p.id);
            }
        }
        // Bound preparation as well as inference, even for a few very long rows.
        if out.texts.len() > limit {
            return Err(build_progress(c, coverage)?.into());
        }
        Ok(out)
    }
}
