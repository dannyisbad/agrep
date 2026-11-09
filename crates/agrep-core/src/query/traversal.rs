use super::*;

pub(super) struct Traversal<'a, 'p> {
    plan: &'p TablePlan<'a>,
    adaptive_prose: bool,
    ids: Option<CandidateGroups>,
    admitted: Option<HashMap<i64, Vec<i64>>>,
    /// On a page, the matcher whose plans take anchors, with the rows they admit per
    /// block. A row is checked against it only after the page's ceiling has read it.
    anchored: Option<(Matcher, HashMap<i64, Vec<i64>>)>,
    order: Vec<BlockBound>,
    position: HashMap<i64, usize>,
    next: usize,
    runs: HashMap<i64, Vec<i64>>,
    occurrences: Option<HashMap<i64, u32>>,
    occurrence_weights: Option<Vec<u8>>,
    frontier: Option<tool_rank::Frontier>,
    visited: HashSet<i64>,
    coverage_tried: bool,
    coverage: Option<CoverageBounds>,
}
enum BlockVisit {
    Read,
    Skip,
    Stop,
}
impl<'a, 'p> Traversal<'a, 'p> {
    pub(super) fn new(
        plan: &'p TablePlan<'a>,
        blocks: Vec<BlockBound>,
        cache: &mut PlanCache,
        result: &mut Results,
    ) -> Result<Self> {
        let c = plan.ctx.c;
        let o = plan.ctx.o;
        let m = plan.ctx.m;
        let table = plan.ctx.table;
        let lane_total = plan.lane_total;
        let span_plan = &plan.span_plan;
        let fts = &plan.fts;
        let has_scope = plan.has_scope;
        let scope_blocks = &plan.scope_blocks;
        let selective = plan.selective;
        let bounded = plan.bounded;
        let partial = plan.ctx.m.partial;
        // A small selective posting stream is cheaper to read once than to rebuild
        // the same expanded-word cursor in every block. This changes the access
        // method only: all posting IDs are retained and the same block bounds stop
        // scoring. If a phrase stream exceeds the small ID-buffer allocation,
        // discard that plan and use the complete block cursor instead. This
        // bounds planner memory, not matches or result accuracy.
        let adaptive_prose = table == "prose" && bounded && !partial && m.terms.len() > 1;
        let mut ids = None;
        // FTS5 advances every OR child on each step, so one corpus-wide stream
        // of a broad substring expansion costs more than the bounded blocks
        // reading only the words each block holds.
        if table == "tools"
            && bounded
            && m.estimated.saturating_mul(8) <= lane_total
            && fts.as_ref().is_some_and(|f| f.matches('"').count() <= 64)
        {
            if let Some(expression) = &fts {
                let tick = Instant::now();
                ids = cache.candidate_rows(
                    c,
                    table,
                    expression,
                    span_plan.as_ref().map(|(_, v)| v.as_slice()),
                )?;
                result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
            }
        }
        // A selective scope is still scanned in its own order, so the same rows
        // fill the page and meet the same ceilings; the text index only spares
        // decoding rows it proves cannot match.
        let admitted = match (&scope_blocks, &fts) {
            (Some(scope_blocks), Some(expression)) => {
                let tick = Instant::now();
                let admitted = scope_candidates(
                    c,
                    m,
                    table,
                    expression,
                    span_plan.as_ref().map(|(_, v)| v.as_slice()),
                    scope_blocks,
                )?;
                result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
                Some(admitted)
            }
            _ => None,
        };
        let anchored = page_anchors(plan, result)?;
        let order = blocks;
        let position: HashMap<i64, usize> = order
            .iter()
            .enumerate()
            .map(|(at, entry)| (entry.0, at))
            .collect();
        let next = 0;
        // Rows the text index admits in blocks still to visit, read for whole runs of
        // consecutive blocks at once.
        let runs: HashMap<i64, Vec<i64>> = HashMap::new();
        let occurrences = (table == "tools"
            && bounded
            && !o.chats
            && !partial
            && m.phase == 1
            && m.explicit.is_none()
            && m.terms.len() == 1
            && (3..=4096).contains(&m.terms[0][0].len())
            && m.terms[0][0].bytes().all(|b| b.is_ascii_alphanumeric())
            && m.ordered_terms[0]
                .iter()
                .all(|t| m.tool_supported.contains(t)))
        .then(HashMap::new);
        if occurrences.is_some() {
            tool_rank::install(c)?;
        }
        let occurrence_weights = (occurrences.is_some()
            && ids.is_none()
            && !selective
            && fts.as_ref().is_some_and(|f| f.matches('"').count() <= 16))
        .then(|| tool_rank::window_plan(&m.group_words(&m.ordered_terms[0], None), &m.terms[0][0]));
        let frontier = (occurrence_weights.is_some()
            && o.sort == "score"
            && o.filters.who.as_deref() == Some("tool")
            && !has_scope
            && o.filters.exclude_project.is_none()
            && o.filters.no_who.is_none()
            && !o.filters.no_side
            && o.filters.caller.is_none())
        .then(|| tool_rank::frontier(c))
        .transpose()?;
        let visited = HashSet::new();
        let coverage_tried = false;
        Ok(Self {
            plan,
            adaptive_prose,
            ids,
            admitted,
            anchored,
            order,
            position,
            next,
            runs,
            occurrences,
            occurrence_weights,
            frontier,
            visited,
            coverage_tried,
            coverage: None,
        })
    }

    pub(super) fn run(
        &mut self,
        cache: &mut PlanCache,
        result: &mut Results,
        sessions: &mut HashSet<(String, String)>,
    ) -> Result<()> {
        while let Some(entry) = self.order.get(self.next).cloned() {
            let (block, bound, _, _, _, max_gap) = entry;
            self.next += 1;
            let block_index = self.visited.len();
            if self.reorder_coverage(cache, result, block_index)? {
                continue;
            }
            self.visited.insert(block);
            match self.block_visit(result, &entry) {
                BlockVisit::Stop => break,
                BlockVisit::Skip => continue,
                BlockVisit::Read => {}
            }
            self.adapt_prose(cache, result, block_index)?;
            let spanned = self.plan.bounded
                && block >= 0
                && !self.plan.selective
                && self.ids.is_none()
                && self.plan.fts.is_some()
                && self.occurrence_weights.is_none();
            self.admit_block_run(result, block, max_gap, spanned)?;
            let (ascii, prefetched) = self.prefetch(result, block, bound)?;
            self.account_prefetched(result, prefetched.as_ref());
            self.admit_anchored(result, block)?;
            let block_ids = if let Some(prefetched) = &prefetched {
                Some(&prefetched.ids)
            } else {
                match &self.ids {
                    Some(map) => map.get(&block),
                    None => self.runs.get(&block),
                }
            };
            let narrowed = self.narrow_to_anchors(result, block, block_ids);
            let block_ids = narrowed.as_ref().or(block_ids);
            if (prefetched.is_some() || self.ids.is_some() || spanned || narrowed.is_some())
                && block_ids.is_none_or(Vec::is_empty)
            {
                continue;
            }
            let read = BlockRead {
                block,
                bound,
                max_gap,
                ascii,
                block_ids,
                spanned,
                prefetched: prefetched.is_some(),
                buffered: self.ids.is_some(),
                weights: self
                    .occurrence_weights
                    .as_ref()
                    .filter(|_| prefetched.is_none()),
            };
            self.read_block(result, sessions, &read)?;
        }
        Ok(())
    }

    fn read_block(
        &self,
        result: &mut Results,
        sessions: &mut HashSet<(String, String)>,
        read: &BlockRead<'_>,
    ) -> Result<()> {
        let c = self.plan.ctx.c;
        result.blocks += 1;
        let query = self.plan.row_query(result, read)?;
        let sql_started = Instant::now();
        let mut stmt = c.prepare_cached(&query.sql)?;
        let mut rows = stmt.query(params_from_iter(query.bind))?;
        result.timings.sqlite_ms += sql_started.elapsed().as_secs_f64() * 1000.;
        loop {
            let sql_started = Instant::now();
            let row = rows.next()?;
            result.timings.sqlite_ms += sql_started.elapsed().as_secs_f64() * 1000.;
            let Some(r) = row else { break };
            let ctx = RowContext {
                plan: self.plan,
                coverage: self.coverage.as_ref(),
                admitted: self.admitted.as_ref(),
                anchored: self.anchored.as_ref().map(|(_, rows)| rows),
                occurrences: self.occurrences.as_ref(),
                eager_prose: query.eager_prose,
                lazy_identity: query.lazy_identity,
                weighted: query.weighted,
                bound: read.bound,
            };
            Candidate::new(ctx, r).process(result, sessions)?;
        }
        Ok(())
    }

    fn reorder_coverage(
        &mut self,
        cache: &mut PlanCache,
        result: &mut Results,
        block_index: usize,
    ) -> Result<bool> {
        let c = self.plan.ctx.c;
        let m = self.plan.ctx.m;
        let now = self.plan.ctx.now;
        let table = self.plan.ctx.table;
        let lane_total = self.plan.lane_total;
        let bounded = self.plan.bounded;
        let partial = self.plan.ctx.m.partial;
        if bounded && partial && table == "prose" && !self.coverage_tried && block_index >= 4 {
            self.coverage_tried = true;
            let tick = Instant::now();
            self.coverage = cache.coverage_bounds(c, m, lane_total)?;
            if let Some(coverage) = &self.coverage {
                let mut replacement = block_bounds(c, m, now, table, cache, Some(coverage))?;
                replacement.retain(|entry| !self.visited.contains(&entry.0));
                self.position = replacement
                    .iter()
                    .enumerate()
                    .map(|(at, entry)| (entry.0, at))
                    .collect();
                (self.order, self.next) = (replacement, 0);
                self.ids = Some(coverage.candidates.clone());
                result.timings.plan_ms += tick.elapsed().as_secs_f64() * 1000.;
                return Ok(true);
            }
            result.timings.plan_ms += tick.elapsed().as_secs_f64() * 1000.;
        }
        Ok(false)
    }

    fn block_visit(&self, result: &mut Results, entry: &BlockBound) -> BlockVisit {
        let o = self.plan.ctx.o;
        let pass_tier = self.plan.ctx.pass_tier;
        let bounded = self.plan.bounded;
        let (_, bound, max_ts, min_session, min_turn, _) = entry;
        let (bound, max_ts, min_turn) = (*bound, *max_ts, *min_turn);
        if bounded && result.hits.len() >= o.limit {
            let last = &result.hits[o.limit - 1];
            if o.sort == "time" {
                if max_ts < last.ts {
                    result.total_basis = "floor".into();
                    return BlockVisit::Stop;
                }
            } else {
                let cutoff = Cutoff::of(last, o.chats, max_ts, min_session, min_turn);
                if cutoff.excludes(pass_tier, bound) {
                    result.total_basis = "floor".into();
                    // Blocks come in descending raw ceiling order, and a lower raw
                    // ceiling can round to the same score: a block that only loses
                    // that tie on time ends nothing, as a newer row after it may win.
                    if cutoff.below(pass_tier, bound) {
                        return BlockVisit::Stop;
                    }
                    return BlockVisit::Skip;
                }
            }
        }
        BlockVisit::Read
    }

    fn adapt_prose(
        &mut self,
        cache: &mut PlanCache,
        result: &mut Results,
        block_index: usize,
    ) -> Result<()> {
        let c = self.plan.ctx.c;
        let table = self.plan.ctx.table;
        let span_plan = &self.plan.span_plan;
        let fts = &self.plan.fts;
        if self.adaptive_prose && block_index > 0 {
            self.adaptive_prose = false;
            if let Some(expression) = fts.as_ref().filter(|_| span_plan.is_none()) {
                let tick = Instant::now();
                self.ids = cache.candidate_rows(
                    c,
                    table,
                    expression,
                    span_plan.as_ref().map(|(_, v)| v.as_slice()),
                )?;
                result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
            }
        }
        Ok(())
    }

    fn admit_block_run(
        &mut self,
        result: &mut Results,
        block: i64,
        max_gap: i64,
        spanned: bool,
    ) -> Result<()> {
        let c = self.plan.ctx.c;
        let m = self.plan.ctx.m;
        let table = self.plan.ctx.table;
        let span_plan = &self.plan.span_plan;
        let fts = &self.plan.fts;
        if !spanned || self.runs.contains_key(&block) {
            return Ok(());
        }
        // Probe one block before committing to a longer posting read.
        let limit = if self.adaptive_prose { 1 } else { RUN_BLOCKS };
        let run = self.run_around(result, block, limit, &self.runs);
        let max_gap = run
            .iter()
            .map(|id| self.order[self.position[id]].5)
            .max()
            .unwrap_or(max_gap);
        let tick = Instant::now();
        admit_run(
            c,
            m,
            table,
            fts.as_deref().unwrap_or_default(),
            span_plan.as_ref().map(|(_, v)| v.as_slice()),
            &run,
            Some(max_gap),
            &mut self.runs,
            self.occurrences.as_mut(),
        )?;
        result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
        Ok(())
    }

    /// `block` with the unvisited blocks around it, up to `limit` in all, that the page
    /// still admits and `admitted` lacks, in block order.
    fn run_around(
        &self,
        result: &Results,
        block: i64,
        limit: usize,
        admitted: &HashMap<i64, Vec<i64>>,
    ) -> Vec<i64> {
        let o = self.plan.ctx.o;
        let pass_tier = self.plan.ctx.pass_tier;
        let page = (o.limit > 0 && result.hits.len() >= o.limit).then(|| &result.hits[o.limit - 1]);
        // Blocks the page still admits will be visited unless its cutoff rises.
        let admits = |entry: &BlockBound| {
            page.is_none_or(|last| {
                if o.sort == "time" {
                    entry.2 >= last.ts
                } else {
                    !Cutoff::of(last, o.chats, entry.2, &entry.3, entry.4)
                        .excludes(pass_tier, entry.1)
                }
            })
        };
        let mut run = vec![block];
        for step in [1, -1] {
            let (mut id, mut skipped) = (block + step, 0);
            while id >= 0 && run.len() < limit && skipped <= RUN_GAP {
                let member = self.position.get(&id).is_some_and(|&at| {
                    !self.visited.contains(&id)
                        && !admitted.contains_key(&id)
                        && admits(&self.order[at])
                });
                if member {
                    run.push(id);
                    skipped = 0;
                } else {
                    skipped += 1;
                }
                id += step;
            }
        }
        run.sort_unstable();
        run
    }

    /// Before a page reads a bounded block, the rows anchors admit in it.
    fn admit_anchored(&mut self, result: &mut Results, block: i64) -> Result<()> {
        let admitted = match &self.anchored {
            Some((_, admitted)) if block >= 0 && !admitted.contains_key(&block) => admitted,
            _ => return Ok(()),
        };
        let run = self.run_around(result, block, RUN_BLOCKS, admitted);
        let tick = Instant::now();
        let (matcher, admitted) = self.anchored.as_mut().expect("matched above");
        admit_run(
            self.plan.ctx.c,
            matcher,
            "tools",
            "",
            None,
            &run,
            None,
            admitted,
            None,
        )?;
        result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
        Ok(())
    }

    /// The rows of `block` to read when a page reads only those anchors admit: once its
    /// total is a floor, or while the block's admitted rows cannot fill it. Otherwise its
    /// ceiling still reads every row, and each is checked after it.
    fn narrow_to_anchors(
        &self,
        result: &Results,
        block: i64,
        block_ids: Option<&Vec<i64>>,
    ) -> Option<Vec<i64>> {
        let anchored = self.anchored.as_ref()?.1.get(&block)?;
        if result.total_basis != "floor"
            && result.hits.len() + anchored.len() >= self.plan.ctx.o.limit
        {
            return None;
        }
        Some(match block_ids {
            Some(ids) => anchored
                .iter()
                .copied()
                .filter(|id| ids.binary_search(id).is_ok())
                .collect(),
            None => anchored.clone(),
        })
    }

    fn prefetch(
        &self,
        result: &mut Results,
        block: i64,
        bound: f64,
    ) -> Result<(bool, Option<tool_rank::Prefetched>)> {
        let c = self.plan.ctx.c;
        let o = self.plan.ctx.o;
        let now = self.plan.ctx.now;
        let fts = &self.plan.fts;
        let ascii = if self.occurrence_weights.is_some() {
            c.query_row(
                "SELECT all_ascii FROM tool_blocks WHERE id=?",
                [block],
                |r| r.get::<_, bool>(0),
            )
            .optional()?
            .unwrap_or(false)
        } else {
            false
        };
        let prefetched = if let (Some(frontier), Some(plan), Some(expression)) =
            (&self.frontier, &self.occurrence_weights, &fts)
        {
            if ascii
                && result.hits.len() >= o.limit
                && result.hits[o.limit - 1].tier == 0
                && result.hits[o.limit - 1].score > 0.
                && result.hits[o.limit - 1].score < ranked(bound)
            {
                let tick = Instant::now();
                let cutoff = result.hits[o.limit - 1].score;
                let prefetched = tool_rank::competitive_rows(
                    c,
                    frontier,
                    expression,
                    plan,
                    block * BLOCK..(block + 1) * BLOCK,
                    (now, cutoff, ascii),
                )?
                .filter(|p| p.complete(cutoff, bound));
                result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
                prefetched
            } else {
                None
            }
        } else {
            None
        };
        Ok((ascii, prefetched))
    }

    fn account_prefetched(
        &mut self,
        result: &mut Results,
        prefetched: Option<&tool_rank::Prefetched>,
    ) {
        if let Some(prefetched) = prefetched {
            // The strict frontier proves the old uncapped row ceiling never
            // skips these matches, even after every possible page improvement.
            let counted = prefetched.total - prefetched.ids.len();
            result.total += counted;
            result.rows += counted;
            result.tool_rows += counted;
            if let Some(occurrences) = &mut self.occurrences {
                occurrences.extend(
                    prefetched
                        .ids
                        .iter()
                        .copied()
                        .zip(prefetched.counts.iter().copied()),
                );
            }
        }
    }
}

type PageAnchors = (Matcher, HashMap<i64, Vec<i64>>);
/// On a page over tool rows, the matcher whose plans take anchors, with the rows it
/// admits in a selective scope; bounded blocks are admitted as they are read. None
/// when anchors add no condition.
fn page_anchors(plan: &TablePlan<'_>, result: &mut Results) -> Result<Option<PageAnchors>> {
    let m = plan.ctx.m;
    if plan.ctx.table != "tools" || m.plan_anchors || !(plan.bounded || plan.selective) {
        return Ok(None);
    }
    let mut anchored = m.clone();
    anchored.plan_anchors = true;
    let expression = anchored.fts(None);
    if expression.is_none() || expression == m.fts(None) {
        return Ok(None);
    }
    let mut rows = HashMap::new();
    if let Some(scope) = &plan.scope_blocks {
        let tick = Instant::now();
        rows = scope_candidates(plan.ctx.c, &anchored, "tools", "", None, scope)?;
        result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
    }
    Ok(Some((anchored, rows)))
}
