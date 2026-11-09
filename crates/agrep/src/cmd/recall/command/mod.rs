use super::*;
mod emission;
mod retrieval;
use emission::*;
use retrieval::*;
pub(crate) fn run(a: Recall, pack: bool, no_auto: bool, color: &str) -> Result<i32> {
    let Prepared {
        input,
        mut filters,
        root,
        census,
    } = match prepare(&a, pack, no_auto)? {
        std::ops::ControlFlow::Continue(prepared) => prepared,
        std::ops::ControlFlow::Break(code) => return Ok(code),
    };
    let queries = &input.queries;
    let request = input.request;
    let stop = |engine: &str, error| input.stop(engine, error);
    let target = if a.probe {
        1
    } else {
        a.hits
            .unwrap_or(if a.budget == 0 {
                3
            } else {
                (a.budget / 5000).clamp(3, 8)
            })
            .max(queries.len())
    };
    // 0.4.9 disclosed raising an explicit --hits to one slot per query (py/recall.py:1988-1997).
    let hits_floored = a
        .hits
        .filter(|&n| !a.probe && queries.len() > n)
        .map(|n| json!({"requested":n,"raised_to":queries.len()}));
    if let Some(floored) = hits_floored.as_ref().filter(|_| !a.json) {
        agrep_core::errln!(
            "--hits {} raised to {}: pack reserves one slot per query",
            floored["requested"],
            floored["raised_to"]
        );
    }
    // 0.4.9 kept a lone query's meaning lane off a page too small to hold it beside its
    // keyword lead, and never searched meaning for tool rows (py/recall.py:1999, 2331-2336).
    let meaning_room = target >= queries.len().max(2);
    // How deep 0.4.9 read each query's lanes (py/recall.py:2000-2001, 2255-2259).
    let query_target = target.max(if a.scope.no_meta { 40 } else { 8 });
    let identity = Identity::detect();
    crate::finish_census(&root, census)?;
    let c = store::reader(&root)?;
    let include_self = a.scope.include_self || (a.scope.chat.is_some() && !a.scope.no_self);
    if !a.json && !a.scope.include_self && (a.scope.no_self || identity.agent_context) {
        identity.disclose(a.scope.no_self, true);
    }
    let caller = Caller::resolve(&c, identity)?;
    let policy = caller.policy(request);
    let tools_only = filters.who.as_deref() == Some("tool");
    let coverage = agrep_core::coverage::dimensions(&c, &filters)?;
    if let Some(chat) = filters
        .chat
        .clone()
        .filter(|_| !queries.iter().any(|q| handles::is_handle(q)))
    {
        let Some(session) = crate::cmd::usage::chat(&c, &chat)? else {
            return stop("query:error", ("chat-unresolved", None));
        };
        filters.chat = Some(session)
    }
    let direct_filters: Vec<_> = [
        ("--chat", filters.chat.is_some()),
        ("--agent", filters.agent.is_some()),
        ("--project", filters.project.is_some()),
        ("--exclude-project", filters.exclude_project.is_some()),
        ("--model", filters.model.is_some()),
        ("--who", filters.who.is_some()),
        ("--no-who", filters.no_who.is_some()),
        ("--since", filters.since.is_some()),
        ("--until", filters.until.is_some()),
    ]
    .into_iter()
    .filter(|(_, yes)| *yes)
    .map(|(name, _)| name)
    .collect();
    let override_filters =
        queries.iter().any(|q| handles::is_handle(q)) && !direct_filters.is_empty();
    let handle_only = queries.iter().all(|q| handles::is_handle(q));
    let mut state = Collected {
        totals_exact: true,
        ..Default::default()
    };
    let mut ctx = QueryContext {
        input: &input,
        root: &root,
        c: &c,
        filters: &filters,
        caller: &caller,
        policy,
        target,
        query_target,
        meaning_room,
        include_self,
        tools_only,
        direct_filters,
        override_filters,
        coverage,
        hits_floored,
    };
    for query in queries {
        if handles::is_handle(query) {
            if let std::ops::ControlFlow::Break(code) = collect_handle(&ctx, &mut state, query)? {
                return Ok(code);
            }
        } else {
            collect_query(&ctx, &mut state, query)?;
        }
    }
    // An explicit matcher failure cannot silently change matcher semantics: 0.4.9 dropped
    // every row, and outside JSON and a probe stopped with its one line
    // (py/recall.py:2404-2412).
    if state.semantic_runtime {
        if !ctx.input.a.json {
            let meta = semantic_meta(&state.lane_runs, ctx.input.queries.len() > 1, false, false);
            agrep_core::errln!("{}", semantic::unavailable_notice(&meta["semantic_status"]));
            if !ctx.input.a.probe {
                return Ok(2);
            }
        }
        state.buckets.iter_mut().for_each(Vec::clear);
    }
    // 0.4.9's JSON note for a keyword lane that fell back to its any-order tiers
    // (py/recall.py:2379-2382, 2916-2917).
    let fell_back = state
        .buckets
        .iter()
        .flatten()
        .any(|h| h.matched == "all-terms" || h.matched == "content-terms")
        && !state
            .buckets
            .iter()
            .flatten()
            .any(|h| h.matched == "phrase");
    let selected = select_hits(&ctx, &mut state)?;
    let hybrid_visible = selected.iter().any(|(_, h)| h.matched == "semantic");
    let blocks = evidence_blocks(&ctx, selected)?;
    emit_page(
        &mut ctx,
        &state,
        &blocks,
        color,
        hybrid_visible,
        handle_only,
        fell_back,
    )
}

struct QueryInput<'a> {
    a: &'a Recall,
    pack: bool,
    no_auto: bool,
    queries: Vec<String>,
    request: caller::Request<'static>,
}
impl QueryInput<'_> {
    fn stop(&self, engine: &str, error: (&str, Option<&str>)) -> Result<i32> {
        if self.a.json {
            let query = if self.pack {
                json!(self.queries)
            } else {
                json!(self.queries.first().map_or("", String::as_str))
            };
            crate::cmd::usage::stopped(None, query, engine, error, self.request, self.no_auto)?;
        }
        Ok(2)
    }
}

struct Prepared<'a> {
    input: QueryInput<'a>,
    filters: agrep_core::query::Filters,
    root: agrep_core::data::DataRoot,
    census: Option<crate::freshness::CensusTask>,
}
fn prepare(
    a: &Recall,
    pack: bool,
    no_auto: bool,
) -> Result<std::ops::ControlFlow<i32, Prepared<'_>>> {
    if a.query
        .iter()
        .any(|s| s.starts_with('@') && s.contains(':') && handles::parse(s).is_err())
    {
        bail!("invalid result handle");
    }
    let queries: Vec<String> = if pack {
        a.query
            .iter()
            .map(|q| q.trim().to_owned())
            .filter(|q| !q.is_empty())
            .collect()
    } else {
        Some(a.query.join(" ").trim().to_owned())
            .filter(|q| !q.is_empty())
            .into_iter()
            .collect()
    };
    let request = caller::Request {
        include_self: a.scope.include_self,
        no_self: a.scope.no_self,
        waived: a.scope.chat.is_some().then_some("chat-filter"),
    };
    let input = QueryInput {
        a,
        pack,
        no_auto,
        queries,
        request,
    };
    let queries = &input.queries;
    let stop = |engine: &str, error| input.stop(engine, error);
    if queries.is_empty() || a.query.iter().any(|q| q.trim().is_empty()) {
        let prog = if pack { "pack" } else { "recall" };
        agrep_core::errln!("query arguments must not be empty (searching the word itself: agrep search \"{prog}\")");
        return Ok(std::ops::ControlFlow::Break(stop(
            "query:error",
            ("empty-query", None),
        )?));
    }
    if a.semantic && a.lexical {
        agrep_core::errln!("-s and --lexical are contradictory; pick one");
        return Ok(std::ops::ControlFlow::Break(stop(
            "query:error",
            ("conflicting-modes", None),
        )?));
    }
    let who_tool = a.scope.no_who.is_none()
        && a.scope
            .who
            .as_deref()
            .map(agrep_core::coverage::speakers)
            .and_then(Result::ok)
            .as_deref()
            == Some("tool");
    if a.semantic && who_tool {
        agrep_core::errln!(
            "-s cannot search --who tool: tool rows are not embedded; use --lexical"
        );
        return Ok(std::ops::ControlFlow::Break(stop(
            "query:error",
            ("unsupported-speaker", None),
        )?));
    }
    let mut filters = match a.scope.filters() {
        Ok(f) => f,
        Err(e) => {
            if let Some(error) = crate::cmd::usage::recorded(&e) {
                stop("query:error", error)?;
            }
            return Err(e);
        }
    };
    // 0.4.9's parser named the command in a filter refusal (py/recall.py:1704-1709).
    agrep_core::coverage::validate(&mut filters).map_err(|e| {
        crate::cmd::usage::refusal(if pack { "agrep pack" } else { "agrep recall" }, e)
    })?;
    if a.semantic && !semantic::installed(&agrep_core::data::DataRoot::discover()?) {
        agrep_core::errln!("semantic model not installed: run agrep setup");
        return Ok(std::ops::ControlFlow::Break(2));
    }
    let (root, census) = {
        let _span = agrep_core::semantic_profile::span("freshness_prepare");
        match ready(no_auto) {
            Ok(ready) => ready,
            Err(e) => {
                if let Some(error) = crate::cmd::usage::recorded(&e) {
                    stop("query:error", error)?;
                }
                return Err(e);
            }
        }
    };
    Ok(std::ops::ControlFlow::Continue(Prepared {
        input,
        filters,
        root,
        census,
    }))
}

#[derive(Default)]
struct Collected {
    meanings: Vec<Vec<Hit>>,
    semantic_ran: bool,
    semantic_failed: bool,
    semantic_building: bool,
    model_missing: bool,
    semantic_unavailable: bool,
    semantic_runtime: bool,
    hybrid_used: bool,
    lane_runs: Vec<(String, Value, Option<semantic::Coverage>)>,
    excluded: Option<usize>,
    filter_note: Option<String>,
    handle_notes: Vec<Value>,
    buckets: Vec<Vec<Hit>>,
    known_sessions: HashSet<String>,
    totals_exact: bool,
    meta_dropped: usize,
    meta_retained: usize,
    deepen: Vec<Option<Options>>,
}

struct QueryContext<'a> {
    input: &'a QueryInput<'a>,
    root: &'a agrep_core::data::DataRoot,
    c: &'a rusqlite::Connection,
    filters: &'a agrep_core::query::Filters,
    caller: &'a Caller,
    policy: Option<caller::Policy<'a>>,
    target: usize,
    query_target: usize,
    meaning_room: bool,
    include_self: bool,
    tools_only: bool,
    direct_filters: Vec<&'static str>,
    override_filters: bool,
    coverage: Value,
    hits_floored: Option<Value>,
}

fn select_hits(ctx: &QueryContext<'_>, state: &mut Collected) -> Result<Vec<(usize, Hit)>> {
    let per_session = ctx.input.a.scope.all_side_chats || ctx.filters.chat.is_some();
    let lone_hybrid = ctx.input.queries.len() == 1 && state.meanings.iter().any(|m| !m.is_empty());
    let selected = if lone_hybrid && ctx.input.a.probe {
        // 0.4.9's probe judged the keyword lane's pick and the meaning lane's
        // (py/recall.py:2552-2561).
        let mut picked = select(&state.buckets, ctx.target, per_session).0;
        picked.extend(select(&state.meanings, ctx.target, per_session).0);
        picked
    } else if lone_hybrid {
        // A lone query's page is its keyword chats merged with its meaning rows
        // (py/recall.py:883-890).
        let mut lexical = Results::default();
        lexical.hits = select(&state.buckets, ctx.query_target, per_session)
            .0
            .into_iter()
            .map(|(_, h)| h)
            .collect();
        let mut meaning = Results::default();
        meaning.hits = std::mem::take(&mut state.meanings[0]);
        hybrid::merge(lexical, Some(meaning), ctx.target, !per_session)
            .hits
            .into_iter()
            .map(|h| (0, h))
            .collect()
    } else {
        // A pack ranks each query's meaning rows after its lived prose phrases, ahead of
        // tool rows and bag-of-words scatter, then reserves a meaning slot per query
        // (py/recall.py:983-994, 2566-2571).
        for (bucket, meaning) in state.buckets.iter_mut().zip(&state.meanings) {
            if !meaning.is_empty() {
                bucket.extend(meaning.iter().cloned());
                bucket.sort_by_key(|h| {
                    (
                        meta_row(h),
                        scatter(h),
                        if h.matched == "semantic" {
                            1
                        } else if h.who == "tool" {
                            2
                        } else {
                            0
                        },
                    )
                });
            }
        }
        let selected = select_deepening(
            &mut state.buckets,
            &mut state.deepen,
            ctx.target,
            per_session,
            |options| {
                let _span = agrep_core::semantic_profile::span("keyword");
                let hits = caller::search(
                    ctx.c,
                    &options,
                    ctx.caller,
                    ctx.include_self,
                    ctx.input.a.scope.no_self,
                )?;
                Ok(heads(hits.hits, per_session))
            },
        )?;
        reserve(selected, &state.meanings, ctx.target, per_session)
    };
    Ok(selected)
}

fn evidence_blocks(ctx: &QueryContext<'_>, selected: Vec<(usize, Hit)>) -> Result<Vec<Block>> {
    let requests: Vec<_> = selected
        .iter()
        .map(|(_, h)| Request::new(&h.handle, None, Some(ctx.input.a.context as i64)))
        .collect();
    let windows_span = agrep_core::semantic_profile::span("windows");
    let windows = evidence::windows(ctx.c, &requests)?;
    // The caller's current window stays excluded after expansion reads neighboring turns.
    let before = (ctx.caller.resolved && !ctx.include_self)
        .then(|| (ctx.caller.session.clone(), ctx.caller.boundary.unwrap_or(0)));
    let query_of: HashMap<String, usize> = selected
        .iter()
        .map(|(query, h)| (h.handle.clone(), *query))
        .collect();
    let mut pairs: Vec<(Hit, Window)> = selected
        .into_iter()
        .map(|(_, hit)| hit)
        .zip(windows)
        .collect();
    if ctx.input.a.budget > 0 && !pairs.is_empty() {
        pairs = expand(
            ctx.c,
            pairs,
            &ctx.input.queries,
            ctx.input.a.budget,
            ctx.input.a.context as i64,
            before.as_ref(),
        )?;
    }
    drop(windows_span);
    let mut blocks = merge_chats(pairs, &query_of);
    if let Some((session, boundary)) = &before {
        for b in &mut blocks {
            if &b.window.session == session {
                b.window.rows.retain(|r| r.turn < *boundary);
                b.window.events.retain(|e| e.turn < *boundary);
            }
        }
        blocks.retain(|b| !b.window.rows.is_empty());
    }
    Ok(blocks)
}
