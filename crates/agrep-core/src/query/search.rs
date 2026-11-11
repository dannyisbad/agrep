use super::*;

pub fn search(c: &Connection, o: &Options) -> Result<Results> {
    search_with(c, o, &mut Plans::default())
}
/// Word expansions, posting estimates and term weights of one pattern and lane set,
/// kept across searches of that pattern over different scopes, and which tool rows of
/// their pages repeat a smaller call id's row.
#[derive(Default)]
pub struct Plans(PlanCache);
pub fn search_with(c: &Connection, o: &Options, plans: &mut Plans) -> Result<Results> {
    if o.filters.no_meta && (o.count_only || o.count_by_tier) {
        bail!("--no-meta cannot narrow -c/--count-by-tier; machine counts keep grep parity");
    }
    // 0.4.9's keyword lane found no row for a pattern without a word, such as `_`
    // (py/corpusdb.py:4757-4758, 4817-4818).
    if !o.regex
        && !o.word
        && !o.pattern.trim().is_empty()
        && boundary_rank::query_tokens(&o.pattern).is_empty()
    {
        return Ok(Results {
            total_basis: "exact".into(),
            ..Default::default()
        });
    }
    let started = Instant::now();
    plans.0.scopes.clear();
    let mut result = if o.filters.no_meta {
        // 0.4.9 filtered only the page it fetched, at least 40 rows (or chats) deep.
        let mut fetched = o.clone();
        fetched.filters.no_meta = false;
        if o.limit > 0 {
            fetched.limit = o.limit.max(crate::history_meta::ROWS_PER_QUERY);
        }
        let result = search_unfiltered(c, &fetched, &mut plans.0)?;
        drop_meta(c, result, o)?
    } else {
        search_unfiltered(c, o, &mut plans.0)?
    };
    name_repeated_calls(c, &mut result.hits, &mut plans.0.repeated_calls)?;
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
/// 0.4.9's `_filter_meta_rows`: a proven ~meta row leaves the page unless it is the
/// best of a page holding nothing else; the totals then count only the kept rows.
pub(super) fn drop_meta(c: &Connection, mut result: Results, o: &Options) -> Result<Results> {
    let fetched = result.hits.len();
    let meta = meta_rows(c, &result.hits, &o.pattern)?;
    let lived = meta.contains(&false);
    let mut kept = Vec::with_capacity(fetched);
    for (mut h, meta) in std::mem::take(&mut result.hits).into_iter().zip(meta) {
        if !meta {
            kept.push(h);
        } else if !lived && result.meta_retained == 0 {
            h.meta_marker = true;
            kept.push(h);
            result.meta_retained = 1;
        } else {
            result.meta_dropped += 1;
        }
    }
    let total = kept.len();
    if o.limit > 0 {
        kept.truncate(o.limit);
    }
    if result.meta_dropped + result.meta_retained > 0 {
        // 0.4.9 compared its matching-row total with the page, also for chat heads.
        let complete = result.total_basis == "exact" && result.rows <= fetched;
        result.total = total;
        result.total_basis = if complete { "exact" } else { "floor" }.into();
        result.tool_rows = kept.iter().filter(|h| h.who == "tool").count();
    }
    result.hits = kept;
    Ok(result)
}
/// Which rows 0.4.9's `--no-meta` proved to be tooling about history: stored harness,
/// synthetic and spawned-task rows, history-reading tool calls, and agent prose within
/// four turns after a history read of `query`.
pub fn meta_rows(c: &Connection, hits: &[Hit], query: &str) -> Result<Vec<bool>> {
    hits.iter()
        .map(|h| crate::history_meta::is_meta(c, h, query))
        .collect()
}
/// 0.4.9's one `--no-meta` disclosure line.
pub fn meta_notice(dropped: usize, retained: usize) -> Option<String> {
    let rows = |n: usize, noun: &str| {
        format!(
            "{} {noun}{}",
            crate::surface::number(n),
            if n == 1 { "" } else { "s" }
        )
    };
    match (dropped, retained) {
        (0, 0) => None,
        (dropped, 0) => Some(format!(
            "excluded {} (--no-meta)",
            rows(dropped, "~meta row")
        )),
        (0, retained) => Some(format!(
            "only ~meta evidence; retained {} (--no-meta)",
            rows(retained, "best row")
        )),
        (dropped, retained) => Some(format!(
            "excluded {}; retained {} (--no-meta)",
            rows(dropped, "~meta row"),
            rows(retained, "sole ~meta row")
        )),
    }
}
pub(super) fn search_unfiltered(
    c: &Connection,
    o: &Options,
    cache: &mut PlanCache,
) -> Result<Results> {
    let started = Instant::now();
    if !o.regex && !o.word && boundary_rank::query_tokens(&o.pattern).len() > 1 {
        crate::fts_span::install(c)?;
    }
    let mut normalized = o.clone();
    normalize(&mut normalized.filters);
    let o = &normalized;
    let optimized = !o.regex && !o.word && o.limit > 0 && !o.exact && o.sort == "score";
    let mut result = if optimized {
        let mut phrase = search_pass(c, o, false, 1, None, cache, 0)?;
        if !o.chats && phrase.hits.len() >= o.limit {
            // Lower tiers have not been counted, even if the phrase stream ended.
            phrase.total_basis = "floor".into();
            phrase
        } else {
            search_pass(c, o, false, 2, Some(phrase), cache, 0)?
        }
    } else {
        search_pass(c, o, false, 0, None, cache, 0)?
    };
    if result.total == 0 && o.content && !o.regex && !o.word && automatic_content(&o.pattern) {
        let anchors = content_anchors(&content_terms(&o.pattern));
        if anchors > 0 {
            let mut fallback = None;
            // Disjoint MaxScore partitions: the rarest anchor first, then rows lacking
            // every earlier one.
            for stage in 0..anchors {
                fallback = Some(search_pass(c, o, true, 0, fallback, cache, stage)?);
            }
            let mut fallback = fallback.unwrap_or_default();
            fallback.timings.add(&result.timings);
            result = fallback;
        }
    }
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
