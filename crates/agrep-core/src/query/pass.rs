use super::*;

pub(super) fn search_pass(
    c: &Connection,
    o: &Options,
    partial: bool,
    phase: u8,
    seed: Option<Results>,
    cache: &mut PlanCache,
    coverage_stage: usize,
) -> Result<Results> {
    let started = Instant::now();
    let _pass_span = crate::semantic_profile::span(if partial {
        "keyword_content"
    } else if phase == 1 {
        "keyword_phrase"
    } else if phase == 2 {
        "keyword_all_terms"
    } else {
        "keyword_combined"
    });
    let mut m = cache.matcher(o, c)?;
    m.partial = partial;
    m.phase = phase;
    // A page's ceiling marks its total a floor on any row it cuts, matching or not.
    // Without one, a row that cannot match leaves no trace, so plans may skip it.
    m.plan_anchors = o.exact || o.limit == 0 || o.sort != "score";
    // The tier this pass's rows take; a pass over both tiers is bounded as phrase rows.
    let pass_tier = if partial { 2 } else { u8::from(phase == 2) };
    if partial {
        m.content(&o.pattern)?;
        if m.terms.is_empty() {
            return Ok(Results {
                total_basis: "exact".into(),
                ..Default::default()
            });
        }
    }
    let now = crate::clock::now_ms();
    let only_tools = o.filters.who.as_deref() == Some("tool");
    let include_tools = o.tools
        && o.filters
            .who
            .as_deref()
            .is_none_or(|who| who.split(',').any(|w| w == "tool"))
        && !o
            .filters
            .no_who
            .as_deref()
            .is_some_and(|who| who.split(',').any(|w| w == "tool"));
    if partial {
        m.idf = match &cache.coverage_idf {
            Some(idf) => idf.clone(),
            None => content_idf(c, &m.terms, !only_tools && !include_tools, cache)?,
        };
        cache.coverage_idf = Some(m.idf.clone());
        let mut order: Vec<_> = (0..m.terms.len())
            .filter(|&i| m.terms[i][0].chars().count() >= 3)
            .collect();
        order.sort_by(|&a, &b| m.idf[b].total_cmp(&m.idf[a]).then(a.cmp(&b)));
        order.truncate(content_anchors(&m.terms));
        m.coverage_term = order.get(coverage_stage).copied();
        m.coverage_excluded
            .extend(order.into_iter().take(coverage_stage));
    }
    let mut sessions: HashSet<_> = seed
        .as_ref()
        .into_iter()
        .flat_map(|r| r.session_keys.iter().cloned())
        .collect();
    let mut result = seed.unwrap_or_else(|| Results {
        total_basis: "exact".into(),
        ..Default::default()
    });
    result.tools_excluded = !o.tools && o.filters.who.as_deref() != Some("tool");

    let (filter_scope, values) = sql_filters(&o.filters, "p", c)?;
    let scope = format!(" AND p.visible=1{filter_scope}");
    result.tools_excluded = !only_tools && !include_tools;
    let tables = if only_tools {
        vec!["tools"]
    } else if include_tools {
        vec!["prose", "tools"]
    } else {
        vec!["prose"]
    };
    result.timings.plan_ms += started.elapsed().as_secs_f64() * 1000.;
    for table in tables {
        let plan_started = Instant::now();
        if skip_tools(o, &result, table) {
            result.total_basis = "floor".into();
            result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
            continue;
        }
        m.order_for(c, table, cache)?;
        if m.estimated == 0 {
            result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
            continue;
        }
        let ctx = PassContext {
            c,
            o,
            m: &m,
            now,
            table,
            filter_scope: &filter_scope,
            scope: &scope,
            values: &values,
            pass_tier,
        };
        if ctx.quick_count(&mut result, plan_started)? {
            continue;
        }
        let plan = TablePlan::new(ctx, cache)?;
        let Some(blocks) = plan.blocks(cache, &mut result, plan_started)? else {
            continue;
        };
        Traversal::new(&plan, blocks, cache, &mut result)?.run(
            cache,
            &mut result,
            &mut sessions,
        )?;
    }
    if o.chats {
        result.total = sessions.len();
    }
    result.session_keys = sessions;
    result.hits.sort_by(|a, b| compare(a, b, &o.sort, o.chats));
    result.tiers.insert("total".into(), result.total);
    for h in &mut result.hits {
        let meta = c.query_row(
            "SELECT family,side FROM sessions WHERE agent=?1 AND id=?2",
            [&h.agent, &h.session],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, bool>(1)?)),
        )?;
        h.family = meta.0;
        h.side = meta.1;
    }
    Ok(result)
}

#[derive(Clone, Copy)]
pub(super) struct PassContext<'a> {
    pub(super) c: &'a Connection,
    pub(super) o: &'a Options,
    pub(super) m: &'a Matcher,
    pub(super) now: i64,
    pub(super) table: &'a str,
    pub(super) filter_scope: &'a str,
    pub(super) scope: &'a str,
    pub(super) values: &'a Vec<Value>,
    pub(super) pass_tier: u8,
}

fn skip_tools(o: &Options, result: &Results, table: &str) -> bool {
    table == "tools"
        && !o.chats
        && !o.exact
        && o.sort == "score"
        && o.limit > 0
        && result.hits.len() >= o.limit
        && result.hits[o.limit - 1].tier == 0
        && result.hits[o.limit - 1].score > weight("tool")
}
