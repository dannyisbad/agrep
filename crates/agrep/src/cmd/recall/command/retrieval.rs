use super::*;
pub(super) fn collect_handle(
    ctx: &QueryContext<'_>,
    state: &mut Collected,
    query: &str,
) -> Result<std::ops::ControlFlow<i32>> {
    let w = match evidence::load(
        ctx.c,
        &Request::new(query, None, Some(ctx.input.a.context as i64)),
    ) {
        Ok(w) => w,
        Err(e) => {
            let Some(failure) = e.downcast_ref::<evidence::HandleError>() else {
                return Err(e);
            };
            let reason = render::safe(&failure.to_string());
            agrep_core::errln!("{reason}");
            return Ok(std::ops::ControlFlow::Break(
                ctx.input.stop("handle", (failure.code, Some(&reason)))?,
            ));
        }
    };
    // 0.4.9 printed each way the served turn differs from the cited one, JSON or not,
    // and carried them as `served` (py/recall.py:1941-1946, 2898-2902).
    for note in &w.notes {
        agrep_core::errln!("{}", note["note"].as_str().unwrap_or_default());
    }
    state.handle_notes.extend(w.notes.iter().cloned());
    let claim = handles::parse(query)?;
    let event = w.events.iter().find(|e| {
        e.turn == w.center
            && claim
                .digest
                .as_deref()
                .is_some_and(|d| d == agrep_core::query::content_digest(&e.text))
            && claim.event.as_ref().is_none_or(|id| id == &e.identity)
    });
    let row = w.rows.iter().find(|r| {
        r.turn == w.center
            && claim
                .digest
                .as_deref()
                .is_none_or(|d| d == agrep_core::query::content_digest(&r.text))
    });
    let (text, who, ts) = if let Some(e) = event {
        (e.text.clone(), "tool".to_owned(), e.ts)
    } else if let Some(r) = row {
        (r.text.clone(), r.who.clone(), r.ts)
    } else {
        let reason = "handle has no indexed row";
        agrep_core::errln!("{reason}");
        return Ok(std::ops::ControlFlow::Break(
            ctx.input
                .stop("handle", ("stale-result-handle", Some(reason)))?,
        ));
    };
    if ctx.override_filters && state.filter_note.is_none() {
        state.filter_note = Some(handle_filter_note(&ctx.direct_filters, ctx.filters, &w, ts));
        if !ctx.input.a.json {
            agrep_core::errln!("{}", state.filter_note.as_deref().unwrap_or_default());
        }
    }
    let mut handle = handles::SessionIndex::load(ctx.c)?.mint(&w.session, w.center, &text);
    if let Some(e) = event {
        let (span_start, b) = claim.span.unwrap_or((0, e.text.chars().count()));
        handle += &format!("~{}:{span_start}-{b}", e.identity);
    }
    let h = Hit {
        session: w.session.clone(),
        agent: w.agent.clone(),
        project: w.project.clone(),
        family: w.family.clone(),
        side: w.side,
        turn: w.center,
        ts,
        text: text.clone(),
        snippet: text.split_whitespace().collect::<Vec<_>>().join(" "),
        who,
        matched: "phrase".into(),
        score: 1.0,
        handle,
        self_marker: ctx.caller.resolved && ctx.caller.session == w.session,
        ..Hit::default()
    };
    state.known_sessions.insert(w.session.clone());
    // 0.4.9 hides a handle into the caller's own window and counts it withheld
    // (py/recall.py:2452-2471).
    let hidden = ctx.policy.is_some()
        && caller::excludes(
            ctx.caller,
            &h.agent,
            &h.session,
            h.turn,
            ctx.include_self,
            ctx.input.a.scope.no_self,
        );
    if hidden {
        state.excluded = Some(state.excluded.unwrap_or(0) + 1);
    }
    state.buckets.push(if hidden { vec![] } else { vec![h] });
    state.deepen.push(None);
    state.meanings.push(Vec::new());
    Ok(std::ops::ControlFlow::Continue(()))
}
pub(super) fn retrieve_query(
    ctx: &QueryContext<'_>,
    state: &mut Collected,
    query: &str,
    options: &Options,
    meta: &mut Option<(Results, MetaHeads)>,
) -> Result<(Results, usize)> {
    let wanted = !state.semantic_failed
        && !ctx.input.a.lexical
        && !ctx.tools_only
        && (ctx.input.a.probe || ctx.meaning_room)
        && semantic::eligible(query)
        && ctx.root.config()?.embeddings != "off";
    let installed = semantic::installed(ctx.root);
    state.model_missing |= !ctx.input.a.semantic && wanted && !installed;
    let lane = if ctx.input.a.semantic || (wanted && installed) {
        Some(semantic::begin(
            ctx.root,
            ctx.c,
            options,
            !ctx.input.a.scope.all_side_chats,
        ))
    } else {
        None
    };
    // Selection reads only the head of each query's ranking, and a page as
    // deep as twice the target usually holds it at a fraction of the cost.
    // Merging with meaning results and the probe's session count need the
    // full page.
    let depth = if lane.is_none() && !ctx.input.a.probe {
        options.limit.min(ctx.target.saturating_mul(2))
    } else {
        options.limit
    };
    let keyword = if ctx.input.a.semantic {
        Default::default()
    } else if let Some((base, heads)) = meta.as_mut() {
        state.meta_dropped += heads.dropped;
        state.meta_retained += usize::from(heads.retained);
        let mut keyword = std::mem::take(base);
        keyword.hits = std::mem::take(&mut heads.heads);
        keyword
    } else {
        let _span = agrep_core::semantic_profile::span("keyword");
        let page = Options {
            limit: depth,
            ..options.clone()
        };
        let keyword = caller::search(
            ctx.c,
            &page,
            ctx.caller,
            ctx.include_self,
            ctx.input.a.scope.no_self,
        )?;
        agrep_core::semantic_profile::event(serde_json::json!({
            "retrieval":keyword.timings,"examined":keyword.examined,"blocks":keyword.blocks
        }));
        keyword
    };
    let r = if let Some(lane) = lane {
        match lane.and_then(|work| semantic::finish(ctx.c, work)) {
            Ok(mut found) => {
                state.semantic_ran = true;
                state.hybrid_used |= !ctx.input.a.semantic && !found.result.hits.is_empty();
                state.lane_runs.push((
                    query.to_owned(),
                    semantic::status(&found.coverage, !found.result.hits.is_empty()),
                    Some(found.coverage),
                ));
                caller::decorate_semantic(ctx.c, &mut found.result, ctx.caller)?;
                semantic::mark_weak(
                    ctx.c,
                    options,
                    &mut found.result.hits,
                    !keyword.hits.is_empty(),
                )?;
                if ctx.input.a.semantic {
                    found.result
                } else {
                    // 0.4.9 read `query_target` meaning rows and ranked them lived
                    // first, then strong first, each band by cosine
                    // (py/recall.py:983-994).
                    let mut rows = std::mem::take(&mut found.result.hits);
                    rows.truncate(ctx.query_target);
                    rows.sort_by_key(|h| (meta_row(h), h.sem_weak));
                    state.meanings.push(rows);
                    keyword
                }
            }
            Err(error) if ctx.input.a.semantic => {
                let status = semantic::failed(&error);
                state.semantic_runtime |= status["state"] != "query-rejected";
                state.semantic_unavailable = true;
                state.lane_runs.push((query.to_owned(), status, None));
                Results::default()
            }
            Err(error) if semantic::is_building(&error) => {
                state.semantic_building = true;
                keyword
            }
            Err(error) => {
                agrep_core::logging::semantic_failure(ctx.root, &error);
                state.semantic_failed = true;
                state
                    .lane_runs
                    .push((query.to_owned(), semantic::failed(&error), None));
                keyword
            }
        }
    } else {
        keyword
    };
    Ok((r, depth))
}
pub(super) fn collect_query(
    ctx: &QueryContext<'_>,
    state: &mut Collected,
    query: &str,
) -> Result<()> {
    let options = Options {
        pattern: query.to_owned(),
        regex: false,
        word: false,
        sort: "score".into(),
        limit: ctx.target.saturating_mul(12).max(40),
        exact: false,
        count_only: false,
        count_by_tier: false,
        content: !ctx.input.a.lexical,
        chats: false,
        tools: ctx.root.config()?.tools
            || ctx
                .filters
                .who
                .as_deref()
                .is_some_and(|who| who.split(',').any(|w| w == "tool")),
        filters: ctx.filters.clone(),
    };
    let options = caller::options(
        &options,
        ctx.caller,
        ctx.include_self,
        ctx.input.a.scope.no_self,
    );
    // The withheld-row probes drop --no-meta's stored tooling roles themselves; the
    // searches leave ~meta rows to the family heads below.
    let probes = options.clone();
    let mut options = options;
    options.filters.no_meta = false;
    let per_session = ctx.input.a.scope.all_side_chats || ctx.filters.chat.is_some();
    let mut meta = if ctx.input.a.scope.no_meta && !ctx.input.a.semantic {
        let who = options.filters.who.is_some();
        Some(MetaHeads::rank(
            ctx.c,
            query,
            ctx.target,
            per_session,
            who,
            |limit, tools| {
                let mut heads = Options {
                    chats: true,
                    limit,
                    ..options.clone()
                };
                match tools {
                    Some(true) if !options.tools => return Ok(Default::default()),
                    Some(true) => heads.filters.who = Some("tool".into()),
                    Some(false) => heads.tools = false,
                    None => {}
                }
                caller::search(
                    ctx.c,
                    &heads,
                    ctx.caller,
                    ctx.include_self,
                    ctx.input.a.scope.no_self,
                )
            },
        )?)
    } else {
        None
    };
    let (r, depth) = retrieve_query(ctx, state, query, &options, &mut meta)?;
    // 0.4.9 counted withheld rows only for a single keyword query; packs and lanes
    // fused with meaning results have no exact count.
    if let Some(policy) = ctx
        .policy
        .as_ref()
        .filter(|_| ctx.input.queries.len() == 1 && !ctx.input.a.semantic && !state.semantic_ran)
    {
        if let Some(withheld) = policy.withheld(ctx.c, &probes, ctx.filters.chat.as_deref())? {
            let tools = ctx.filters.who.is_some()
                || ctx.filters.no_who.is_some()
                || tool_lane(
                    &r.hits,
                    depth,
                    ctx.target,
                    ctx.input.a.scope.all_side_chats,
                    |limit| {
                        let prose = Options {
                            tools: false,
                            limit,
                            ..options.clone()
                        };
                        Ok(caller::search(
                            ctx.c,
                            &prose,
                            ctx.caller,
                            ctx.include_self,
                            ctx.input.a.scope.no_self,
                        )?
                        .hits)
                    },
                )?;
            state.excluded = Some(withheld.prose + if tools { withheld.tools } else { 0 });
        }
    }
    if state.meanings.len() < state.buckets.len() + 1 {
        state.meanings.push(Vec::new());
    }
    let meaning = state.meanings.last().map(Vec::as_slice).unwrap_or_default();
    state.totals_exact &= r.total_basis == "exact" && meaning.is_empty();
    state
        .known_sessions
        .extend(r.hits.iter().chain(meaning).map(|h| h.session.clone()));
    // A --no-meta bucket already holds every head 0.4.9 selected from.
    let deeper =
        (meta.is_none() && depth < options.limit && r.hits.len() == depth).then_some(options);
    // A cut page's ~meta heads wait for the full page, which can hold more lived ones.
    let mut hits = if meta.is_some() {
        r.hits
    } else {
        heads(r.hits, per_session)
    };
    if deeper.is_some() {
        hits.truncate(hits.iter().position(meta_row).unwrap_or(hits.len()));
    }
    state.deepen.push(deeper);
    state.buckets.push(hits);
    Ok(())
}
