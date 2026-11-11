use super::*;

/// Symbol rows a bounded plan reads at most, as for its other candidate rows.
const SYMBOL_ROWS: usize = 65_536;

pub(super) struct TablePlan<'a> {
    pub(super) ctx: PassContext<'a>,
    pub(super) lane_total: usize,
    pub(super) fts_table: &'static str,
    pub(super) span_plan: Option<(String, Vec<u8>)>,
    pub(super) fts: Option<String>,
    pub(super) has_scope: bool,
    pub(super) scope_blocks: Option<ScopeBlocks>,
    pub(super) selective: bool,
    pub(super) bounded: bool,
    /// Tool rows the symbol index admits (`TablePlan::symbol_rows`).
    pub(super) symbols: Option<CandidateGroups>,
}
impl<'a> TablePlan<'a> {
    pub(super) fn new(ctx: PassContext<'a>, cache: &mut PlanCache) -> Result<Self> {
        let c = ctx.c;
        let o = ctx.o;
        let m = ctx.m;
        let table = ctx.table;
        let partial = ctx.m.partial;
        let lane_total: usize = c
            .query_row(
                "SELECT CAST(value AS INTEGER) FROM state WHERE key=?",
                [format!("{table}_count")],
                |r| r.get(0),
            )
            .optional()?
            .unwrap_or(0);
        // A prose term in most rows is cheaper to verify than to admit through the index.
        // Tool rows decode far more slowly, so tool blocks always read admitted rows.
        let common_scan = table == "prose"
            && !partial
            && !o.regex
            && !o.word
            && !o.exact
            && o.limit > 0
            && o.sort == "score"
            && m.terms.len() == 1
            && m.estimated != usize::MAX
            && m.estimated.saturating_mul(4) >= lane_total;
        let fts_table = if table == "tools" {
            "tools_fts"
        } else {
            "prose_fts"
        };
        let span_plan = if table == "tools" {
            m.word_positions(None)
        } else {
            m.prose_positions()
        };
        let fts = if common_scan {
            None
        } else if let Some((expression, _)) = &span_plan {
            Some(expression.clone())
        } else if table == "prose" {
            m.prose_fts(&o.pattern)
        } else {
            m.fts(None)
        };
        let has_scope = o.filters.agent.is_some()
            || o.filters.chat.is_some()
            || o.filters.since.is_some()
            || o.filters.model.is_some()
            || o.filters.project.is_some()
            || o.filters.until.is_some()
            || o.filters.session_rows.is_some()
            || o.filters.sessions.is_some();
        let scope_blocks = Self::scope_blocks(ctx, lane_total, has_scope, cache)?;
        let selective = scope_blocks.is_some();
        let bounded = !selective
            && m.estimated > 128
            && !o.exact
            && o.limit > 0
            && matches!(o.sort.as_str(), "score" | "time");
        let symbols = Self::symbol_rows(ctx, lane_total, selective, bounded, cache)?;
        Ok(Self {
            ctx,
            lane_total,
            fts_table,
            span_plan,
            fts,
            has_scope,
            scope_blocks,
            selective,
            bounded,
            symbols,
        })
    }
    /// The tool rows the symbol index admits, read where they spare reading others: in
    /// plans that may skip the rows no index admits, and on a bounded page, which checks
    /// them only after its ceilings. A plan reading every admitted row takes up to half
    /// the lane this way, a bounded one up to [`SYMBOL_ROWS`], and neither more than the
    /// dictionary's estimate.
    fn symbol_rows(
        ctx: PassContext<'_>,
        lane_total: usize,
        selective: bool,
        bounded: bool,
        cache: &mut PlanCache,
    ) -> Result<Option<CandidateGroups>> {
        let m = ctx.m;
        if ctx.table != "tools" || selective || !(m.plan_anchors || bounded) {
            return Ok(None);
        }
        let Some(expression) = m.symbol_expression() else {
            return Ok(None);
        };
        let cap = if bounded { SYMBOL_ROWS } else { lane_total / 2 };
        cache.symbol_rows(ctx.c, &expression, cap.min(m.estimated))
    }
    fn scope_blocks(
        ctx: PassContext<'_>,
        lane_total: usize,
        has_scope: bool,
        cache: &mut PlanCache,
    ) -> Result<Option<ScopeBlocks>> {
        let c = ctx.c;
        let table = ctx.table;
        let filter_scope = ctx.filter_scope;
        let values = ctx.values;
        let scope_blocks = if let Some(blocks) = cache.scopes.get(table) {
            blocks.clone()
        } else if has_scope {
            let limit = 4096usize.max((lane_total / 16).min(65_536));
            // Invisible rows may conservatively make a scope look larger, but
            // cannot exclude a result. Leaving visibility out lets ordinary
            // agent/time probes use their covering indexes instead of fetching
            // thousands of prose records past their text payload.
            let sql = format!(
                "SELECT id/{BLOCK},count(*) FROM (SELECT p.id FROM {table} p WHERE 1{filter_scope} LIMIT {}) GROUP BY 1 ORDER BY 1",
                limit + 1
            );
            let blocks = c
                .prepare(&sql)?
                .query_map(params_from_iter(values.clone()), |r| {
                    Ok((r.get::<_, i64>(0)?, r.get::<_, usize>(1)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let blocks = (blocks.iter().map(|(_, rows)| rows).sum::<usize>() <= limit)
                .then(|| std::sync::Arc::new(blocks));
            cache.scopes.insert(table.into(), blocks.clone());
            blocks
        } else {
            None
        };
        Ok(scope_blocks)
    }
    pub(super) fn blocks(
        &self,
        cache: &mut PlanCache,
        result: &mut Results,
        plan_started: Instant,
    ) -> Result<Option<Vec<BlockBound>>> {
        let c = self.ctx.c;
        let o = self.ctx.o;
        let m = self.ctx.m;
        let now = self.ctx.now;
        let table = self.ctx.table;
        let bounded = self.bounded;
        let mut blocks = if bounded {
            block_bounds(c, m, now, table, cache, None)?
        } else {
            let max_gap = c.query_row(
                &format!(
                    "SELECT coalesce(max(max_gap),0) FROM {}",
                    if table == "tools" {
                        "tool_blocks"
                    } else {
                        "blocks"
                    }
                ),
                [],
                |r| r.get(0),
            )?;
            match &self.symbols {
                // Such a plan reads only the rows the symbol index admits, by block.
                Some(symbols) => {
                    let mut ids: Vec<i64> = symbols.keys().copied().collect();
                    ids.sort_unstable();
                    ids.into_iter()
                        .map(|block| (block, f64::INFINITY, i64::MAX, String::new(), 0, max_gap))
                        .collect()
                }
                None => vec![(-1, f64::INFINITY, i64::MAX, String::new(), 0, max_gap)],
            }
        };
        if bounded && o.sort == "time" {
            blocks.sort_by(|a, b| b.2.cmp(&a.2).then(a.3.cmp(&b.3)).then(a.4.cmp(&b.4)));
        }
        if bounded
            && o.sort == "score"
            && m.phase == 1
            && result.hits.len() >= o.limit
            && result.hits[o.limit - 1].tier == 0
            && blocks
                .first()
                .is_some_and(|b| ranked(b.1) < result.hits[o.limit - 1].score)
        {
            result.total_basis = "floor".into();
            result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
            return Ok(None);
        }
        result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
        Ok(Some(blocks))
    }
}
