use crate::{compact, ready, Search};
use agrep_core::{
    caller, hybrid,
    query::Options,
    render::{self, Style},
    semantic, store,
};
use anyhow::Result;
use std::io::IsTerminal;
pub(crate) fn run(s: Search, no_auto: bool, color: &str) -> Result<i32> {
    let _query_span = agrep_core::semantic_profile::span("query_total");
    let query_start = std::time::Instant::now();
    let timing = agrep_core::semantic_profile::timing_enabled();
    let pattern = s.pattern.join(" ");
    let request = caller::Request {
        include_self: s.scope.include_self,
        no_self: s.scope.no_self,
        waived: s.scope.chat.is_some().then_some("chat-filter"),
    };
    // 0.4.9's usage errors, in its order and each with its `--json` record
    // (py/search.py:7522-7621): the time bounds are read before the index refreshes.
    let stop = |error| stopped(&s, &pattern, request, no_auto, error);
    let Prepared {
        root,
        census,
        mut f,
        compact_page,
    } = match prepare(&s, &pattern, request, no_auto)? {
        std::ops::ControlFlow::Continue(prepared) => prepared,
        std::ops::ControlFlow::Break(code) => return Ok(code),
    };
    let _model_read_ahead = if s.semantic
        || (!s.lexical
            && !s.regex
            && !s.word
            && !s.coverage
            && s.sort == "score"
            && compact_page
            && semantic::eligible(&pattern))
    {
        let root = agrep_core::data::DataRoot::discover()?;
        if s.semantic || root.config()?.embeddings != "off" {
            agrep_core::semantic_model::prefetch_query(&root)
        } else {
            None
        }
    } else {
        None
    };
    let identity = caller::Identity::detect();
    let config = root.config()?;
    crate::finish_census(&root, census)?;
    let c = store::reader(&root)?;
    let coverage = agrep_core::coverage::dimensions(&c, &f)?;
    if let Some(chat) = &f.chat {
        let Some(session) = super::usage::chat(&c, chat)? else {
            return stop(("chat-unresolved", None));
        };
        f.inherited = agrep_core::forks::inherited(&c, &session)?;
        f.chat = Some(session);
    }
    if !s.json && !s.scope.include_self && (s.scope.no_self || identity.agent_context) {
        identity.disclose(s.scope.no_self, false);
    }
    let caller = caller::Caller::resolve(&c, identity)?;
    let (options, default_rows) = query_options(&s, &pattern, &f, config.tools, compact_page);
    // A compact page ranks at least the rows of the page without `-n` and orders them as
    // that page does, so a smaller `-n` only cuts that order.
    let depth = if compact_page && options.limit > 0 {
        options.limit.max(default_rows)
    } else {
        options.limit
    };
    let include_self = s.scope.include_self || (s.scope.chat.is_some() && !s.scope.no_self);
    let options = caller::options(&options, &caller, include_self, s.scope.no_self);
    let wants_meaning = !s.semantic
        && !s.lexical
        && !s.regex
        && !s.word
        && !s.coverage
        && s.sort == "score"
        && f.who.as_deref() != Some("tool")
        && compact_page
        && config.embeddings != "off"
        && semantic::eligible(&pattern);
    let automatic = wants_meaning && semantic::installed(&root);
    if timing {
        agrep_core::errln!(
            "{}",
            serde_json::json!({"query_prepare_ms":query_start.elapsed().as_secs_f64()*1000.})
        );
    }
    let (result, semantic_coverage, keyword_only, plans) = match retrieve(
        Retrieval {
            s: &s,
            root: &root,
            c: &c,
            options: &options,
            caller: &caller,
            include_self,
            no_auto,
            pattern: &pattern,
            request,
            compact_page,
            depth,
        },
        automatic,
        timing,
    )? {
        std::ops::ControlFlow::Continue(Retrieved {
            result,
            semantic_coverage,
            keyword_only,
            plans,
        }) => (result, semantic_coverage, keyword_only, plans),
        std::ops::ControlFlow::Break(code) => return Ok(code),
    };
    render_result(
        RenderRequest {
            s: &s,
            root,
            c: &c,
            f: &f,
            caller: &caller,
            options: &options,
            no_auto,
            color,
            compact_page,
            default_rows,
            pattern: &pattern,
            wants_meaning,
            automatic,
            coverage,
        },
        result,
        semantic_coverage,
        keyword_only,
        plans,
    )
}
struct Prepared {
    root: agrep_core::data::DataRoot,
    census: Option<crate::freshness::CensusTask>,
    f: agrep_core::query::Filters,
    compact_page: bool,
}
fn prepare(
    s: &Search,
    pattern: &str,
    request: caller::Request<'_>,
    no_auto: bool,
) -> Result<std::ops::ControlFlow<i32, Prepared>> {
    let stop = |error| stopped(s, pattern, request, no_auto, error);
    if pattern.trim().is_empty() {
        // `agrep search` also lands here when a bare `agrep <word>` consumed a command word.
        agrep_core::errln!("empty pattern - add a word or phrase to search (a word that is also a command searches with: agrep search \"search\")");
        return Ok(std::ops::ControlFlow::Break(stop(("empty-query", None))?));
    }
    if s.regex {
        if let Some(reason) = agrep_core::query::regex_error(pattern.trim()) {
            agrep_core::errln!("bad regex: {reason}");
            return Ok(std::ops::ControlFlow::Break(stop((
                "invalid-regex",
                Some(&reason),
            ))?));
        }
    }
    let mut f = match s.scope.filters() {
        Ok(f) => f,
        Err(e) => {
            if let Some(error) = super::usage::recorded(&e) {
                stop(error)?;
            }
            return Err(e);
        }
    };
    agrep_core::coverage::validate(&mut f)?;
    // 0.4.9's `profile_wants` (py/search.py:7375-7381): the compact page, and with it the
    // automatic meaning lane, serves only the default row surface.
    let compact_page =
        compact(s.classic) && !(s.json || s.flat || s.chats || s.count || s.count_by_tier);
    if s.semantic && !semantic::installed(&agrep_core::data::DataRoot::discover()?) {
        agrep_core::errln!("semantic model not installed: run agrep setup");
        return Ok(std::ops::ControlFlow::Break(2));
    }
    let (root, census) = {
        let _span = agrep_core::semantic_profile::span("freshness_prepare");
        match ready(no_auto) {
            Ok(ready) => ready,
            Err(e) => {
                if let Some(error) = super::usage::recorded(&e) {
                    stop(error)?;
                }
                return Err(e);
            }
        }
    };
    Ok(std::ops::ControlFlow::Continue(Prepared {
        root,
        census,
        f,
        compact_page,
    }))
}
struct Retrieval<'a> {
    s: &'a Search,
    root: &'a agrep_core::data::DataRoot,
    c: &'a rusqlite::Connection,
    options: &'a Options,
    caller: &'a caller::Caller,
    include_self: bool,
    no_auto: bool,
    pattern: &'a str,
    request: caller::Request<'a>,
    compact_page: bool,
    /// The rows each lane ranks: `-n`, or on a compact page at least a full page's.
    depth: usize,
}
struct Retrieved {
    result: agrep_core::query::Results,
    semantic_coverage: Option<semantic::Coverage>,
    keyword_only: bool,
    plans: agrep_core::query::Plans,
}
fn retrieve(
    ctx: Retrieval<'_>,
    automatic: bool,
    timing: bool,
) -> Result<std::ops::ControlFlow<i32, Retrieved>> {
    let Retrieval {
        s,
        root,
        c,
        options,
        caller,
        include_self,
        no_auto,
        pattern,
        request,
        compact_page,
        depth,
    } = ctx;
    let lane = if s.semantic || automatic {
        let ranked = Options {
            limit: depth,
            ..options.clone()
        };
        Some(semantic::begin(root, c, &ranked, !s.scope.all_side_chats))
    } else {
        None
    };
    let keyword_start = std::time::Instant::now();
    // Word expansions and term estimates of this pattern, reused by the chats' counts.
    let mut plans = agrep_core::query::Plans::default();
    let keyword = if s.semantic {
        Default::default()
    } else {
        let _span = agrep_core::semantic_profile::span("keyword");
        let mut search = |limit| {
            caller::search_with(
                c,
                &Options {
                    limit,
                    ..options.clone()
                },
                caller,
                include_self,
                s.scope.no_self,
                &mut plans,
            )
        };
        // A chat listing names every chat and a count counts every row; pages fold copies.
        if s.chats || s.count || s.count_by_tier {
            search(options.limit)?
        } else {
            let mut copies = agrep_core::query::Copies::new(c, options);
            agrep_core::forks::keyword_page(c, depth, &options.sort, search, |row| {
                let mut found = copies.of(row)?;
                caller::decorate(c, &mut found, caller)?;
                Ok(found)
            })?
        }
    };
    if timing {
        agrep_core::semantic_profile::event(serde_json::json!({
            "retrieval":keyword.timings,"examined":keyword.examined,"blocks":keyword.blocks
        }));
    }
    let keyword_ms = if s.semantic {
        0.
    } else {
        keyword_start.elapsed().as_secs_f64() * 1000.
    };
    let (mut result, semantic_coverage, keyword_only) = if let Some(lane) = lane {
        match lane.and_then(|work| semantic::finish(c, work)) {
            Ok(mut found) => {
                caller::decorate_semantic(c, &mut found.result, caller)?;
                semantic::mark_weak(c, options, &mut found.result.hits, !keyword.hits.is_empty())?;
                let result = if s.semantic {
                    found.result
                } else if keyword.hits.is_empty()
                    && found.result.hits.iter().all(|h| h.score < semantic::STRONG)
                {
                    // 0.4.9 kept a page of only sub-strong neighbors a miss; `-s` shows
                    // them (py/search.py:7956-7960).
                    keyword
                } else {
                    hybrid::merge(keyword, Some(found.result), depth, !s.scope.all_side_chats)
                };
                (result, Some(found.coverage), false)
            }
            Err(error) if s.semantic => {
                // An explicit matcher failure cannot silently change matcher semantics
                // (py/search.py:7686-7719).
                let freshness = render::machine_freshness(no_auto);
                return Ok(std::ops::ControlFlow::Break(unavailable(
                    s,
                    pattern.trim(),
                    semantic::failed(&error),
                    caller.metadata(request, None),
                    freshness,
                )?));
            }
            Err(error) => {
                if !semantic::is_building(&error) {
                    agrep_core::logging::semantic_failure(root, &error);
                }
                (keyword, None, true)
            }
        }
    } else {
        (keyword, None, true)
    };
    if timing {
        agrep_core::errln!("{}", serde_json::json!({"keyword_ms":keyword_ms}));
    }
    if compact_page {
        render::compact_order(&mut result, options.limit);
    }
    Ok(std::ops::ControlFlow::Continue(Retrieved {
        result,
        semantic_coverage,
        keyword_only,
        plans,
    }))
}
struct RenderRequest<'a> {
    s: &'a Search,
    root: agrep_core::data::DataRoot,
    c: &'a rusqlite::Connection,
    f: &'a agrep_core::query::Filters,
    caller: &'a caller::Caller,
    options: &'a Options,
    no_auto: bool,
    color: &'a str,
    compact_page: bool,
    default_rows: usize,
    pattern: &'a str,
    wants_meaning: bool,
    automatic: bool,
    coverage: serde_json::Value,
}
fn render_result(
    ctx: RenderRequest<'_>,
    mut result: agrep_core::query::Results,
    semantic_coverage: Option<semantic::Coverage>,
    keyword_only: bool,
    mut plans: agrep_core::query::Plans,
) -> Result<i32> {
    let RenderRequest {
        s,
        root,
        c,
        f,
        caller,
        options,
        no_auto,
        color,
        compact_page,
        default_rows,
        pattern,
        wants_meaning,
        automatic,
        coverage,
    } = ctx;
    let style_semantic = s.semantic && semantic_coverage.is_some();
    if style_semantic && s.chats && s.sort == "score" {
        meaning_heads(&mut result.hits, agrep_core::clock::now_ms());
    }
    // 0.4.9's reruns of this search (py/search.py:5926-6036): every filter as one token,
    // then the page's own surface and a page size.
    let base = rerun_base(s, f, no_auto, color);
    let surface: &[&str] = match (s.chats, s.json, s.flat) {
        (true, true, _) => &["-l", "--json"],
        (true, false, _) => &["-l"],
        (false, true, _) => &["--json"],
        (false, false, true) => &["--flat"],
        _ => &["--classic"],
    };
    let rerun = |surface: &[&str], n: usize| -> Vec<String> {
        base.iter()
            .cloned()
            .chain(surface.iter().map(|flag| flag.to_string()))
            .chain(["-n".into(), n.to_string(), "--".into(), pattern.to_owned()])
            .collect()
    };
    // 0.4.9's forced lane ran before the page rendered (py/search.py:7907-7914); it ranks
    // by coverage whatever --sort said, and may repeat the page's rows.
    let coverage_hits = if s.coverage {
        let ranked = Options {
            sort: "score".into(),
            ..options.clone()
        };
        Some(caller::coverage(c, &ranked, caller)?.hits)
    } else {
        None
    };
    let request = caller::Request {
        include_self: s.scope.include_self,
        no_self: s.scope.no_self,
        waived: f.chat.is_some().then_some("chat-filter"),
    };
    let policy = caller.policy(request);
    // As in 0.4.9, only a result drawn from the keyword lane alone has an exact count of
    // the rows the policy withheld; a second ranked lane has no exact union with it.
    let excluded = match &policy {
        Some(policy) if keyword_only && !s.coverage => policy
            .withheld(c, options, f.chat.as_deref())?
            .map(caller::Withheld::total),
        _ => None,
    };
    // 0.4.9's `session_hits`: each listed chat's exact matching rows, and on a meaning page
    // the rows `_session_heads` counted per chat (py/search.py:2409-2427).
    let session_hits = if style_semantic {
        result
            .hits
            .iter()
            .map(|h| {
                result
                    .hits
                    .iter()
                    .filter(|o| o.session == h.session)
                    .count()
            })
            .collect()
    } else if s.chats && !s.count && !s.count_by_tier {
        let _span = agrep_core::semantic_profile::span("session_hits");
        let sessions: Vec<String> = result.hits.iter().map(|h| h.session.clone()).collect();
        agrep_core::query::session_counts(c, options, &sessions, &mut plans)?
    } else {
        Vec::new()
    };
    let exhaustible = !s.semantic && !result.hits.iter().any(|h| h.matched == "semantic");
    let current = s.max.unwrap_or(default_rows);
    let larger = match s.max {
        Some(0) => None,
        _ if s.json => [80, 160, 200].into_iter().find(|&n| current < n),
        _ => (current < 80).then_some(80),
    };
    let style = Style {
        json: s.json,
        flat: s.flat || (!compact_page && !super::color_on(color)),
        compact: compact_page,
        count: s.count,
        tiers: s.count_by_tier,
        chats: s.chats,
        semantic: style_semantic,
        regex: s.regex,
        word: s.word,
        checked: agrep_core::render::checked(no_auto),
        no_auto,
        full_argv: rerun(&["--classic"], 0),
        larger_argv: larger
            // A compact page names the bounded rerun once its rows run out, meaning rows or not.
            .filter(|_| exhaustible || s.json || compact_page)
            .map(|n| rerun(surface, n)),
        exhaustive_argv: (s.json && exhaustible && s.max != Some(0)).then(|| rerun(surface, 0)),
        color: super::color_on(color),
        data_root: Some(root),
        requested: s.max,
        diversify: !s.scope.all_side_chats,
        caller_unknown: caller.identity.agent_context && !caller.resolved,
        self_exclusion: caller.metadata(request, excluded),
        filter_coverage: coverage.clone(),
        session_hits,
    };
    emit_result(
        Emission {
            s,
            c,
            f,
            options,
            pattern,
            no_auto,
            wants_meaning,
            automatic,
            excluded,
            coverage: &coverage,
            policy,
        },
        &result,
        &style,
        semantic_coverage.as_ref(),
        coverage_hits,
    )
}
fn rerun_base(
    s: &Search,
    f: &agrep_core::query::Filters,
    no_auto: bool,
    color: &str,
) -> Vec<String> {
    let mut base = vec!["agrep".to_string()];
    if s.semantic {
        base.push("-s".into());
    } else if s.regex {
        base.push("-E".into());
    } else if s.word {
        base.push("-w".into());
    } else if s.lexical {
        base.push("--lexical".into());
    }
    for (flag, value) in [
        ("--agent", &s.scope.agent),
        ("--project", &f.project),
        ("--exclude-project", &s.scope.exclude_project),
        ("--model", &s.scope.model),
        ("--who", &s.scope.who),
        ("--no-who", &s.scope.no_who),
        ("--chat", &f.chat),
        ("--since", &s.scope.since),
        ("--until", &s.scope.until),
    ] {
        if let Some(v) = value {
            base.push(format!("{flag}={v}"));
        }
    }
    let self_flag = if s.scope.include_self {
        Some("--self".to_string())
    } else {
        s.scope.no_self.then(|| "--no-self".into())
    };
    for flag in [
        s.scope.soft.then(|| "--soft".to_string()),
        s.scope.no_meta.then(|| "--no-meta".into()),
        (s.sort != "score").then(|| format!("--sort={}", s.sort)),
        self_flag,
        s.scope.all_side_chats.then(|| "--all-side-chats".into()),
        s.scope.no_side.then(|| "--no-side".into()),
        no_auto.then(|| "--no-auto".into()),
        (color != "auto").then(|| format!("--color={color}")),
    ]
    .into_iter()
    .flatten()
    {
        base.push(flag);
    }
    base
}
struct Emission<'a> {
    s: &'a Search,
    c: &'a rusqlite::Connection,
    f: &'a agrep_core::query::Filters,
    options: &'a Options,
    pattern: &'a str,
    no_auto: bool,
    wants_meaning: bool,
    automatic: bool,
    excluded: Option<usize>,
    coverage: &'a serde_json::Value,
    policy: Option<caller::Policy<'a>>,
}
fn emit_result(
    ctx: Emission<'_>,
    result: &agrep_core::query::Results,
    style: &Style,
    semantic_coverage: Option<&semantic::Coverage>,
    coverage_hits: Option<Vec<agrep_core::query::Hit>>,
) -> Result<i32> {
    let Emission {
        s,
        c,
        f,
        options,
        pattern,
        no_auto,
        wants_meaning,
        automatic,
        excluded,
        coverage,
        policy,
    } = ctx;
    if let Some(line) = agrep_core::query::meta_notice(result.meta_dropped, result.meta_retained) {
        agrep_core::errln!("{line}");
    }
    // 0.4.9's automatic lane answered keyword-only without a model, and said so beside the
    // page or a filtered miss, or as an unfiltered miss's verdict (py/search.py:8063-8104).
    let filtered = s.scope.here
        || [
            &s.scope.chat,
            &s.scope.agent,
            &s.scope.project,
            &s.scope.model,
            &s.scope.since,
            &s.scope.until,
            &s.scope.who,
            &s.scope.no_who,
        ]
        .iter()
        .any(|v| v.is_some());
    let keyword_only_miss = wants_meaning
        && !automatic
        && result.total == 0
        && result.total_basis == "exact"
        && !filtered
        && excluded.unwrap_or(0) == 0;
    if wants_meaning && !automatic && !keyword_only_miss {
        agrep_core::errln!(
            "meaning unavailable; keyword-only (semantic model not installed: run agrep setup)"
        );
    }
    // The block follows the page (py/search.py:8371-8380, 8415-8417), or on a miss the
    // lines explaining it (py/search.py:8139-8157).
    let coverage_block = coverage_hits
        .map(|hits| render::coverage_block(&hits, pattern, style))
        .unwrap_or_default();
    let (after_page, after_miss) = if result.total == 0 {
        (&[][..], &coverage_block[..])
    } else {
        (&coverage_block[..], &[][..])
    };
    {
        let _span = agrep_core::semantic_profile::span("render");
        render::emit_semantic(result, pattern, f, style, semantic_coverage, after_page)?;
    }
    // 0.4.9 let an empty filter dimension's line stand alone as the zero's explanation.
    let empty_dimension = coverage["empty_dimensions"]
        .as_array()
        .is_some_and(|d| !d.is_empty());
    if keyword_only_miss {
        // 0.4.9's `miss_verdict` and `miss_zero_render` (py/surface_policy.py:2912-2958):
        // the stale-history hedge outranks the keyword-only tail.
        let n: i64 = c.query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))?;
        if n == 0 {
            agrep_core::errln!("no match - nothing is indexed yet");
        } else {
            agrep_core::errln!(
                "no match across {} session{} - {}",
                agrep_core::surface::number(n as usize),
                if n == 1 { "" } else { "s" },
                if no_auto {
                    render::NO_AUTO_STALE
                } else {
                    "meaning unavailable; keyword-only"
                }
            );
        }
    } else if s.semantic {
        if result.total == 0 && !s.json && !s.count && !s.count_by_tier {
            // 0.4.9 asked the keyword lane whether a match exists, unless --chat named one
            // chat (py/search.py:8105-8119); `-s` answers only from a complete snapshot.
            let keyword_total = if s.scope.chat.is_none() {
                let count = Options {
                    limit: 0,
                    exact: true,
                    count_only: true,
                    count_by_tier: false,
                    content: false,
                    ..options.clone()
                };
                Some(agrep_core::query::search(c, &count)?.total)
            } else {
                None
            };
            if let Some(coverage) = semantic_coverage {
                for line in meaning_miss(keyword_total, pattern, coverage) {
                    agrep_core::errln!("{line}");
                }
            }
        }
    } else if result.total == 0 && !s.json && !s.count && !s.count_by_tier && !empty_dimension {
        // 0.4.9's matcher_empty_line (py/display_policy.py:1358-1370) names the matcher and
        // its corpus: one message record per user-side row, its reply attached
        // (py/common.py:290-298 sums sessions.jsonl's per-session `n`).
        let n: i64 = c.query_row(
            "SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_user_count'),0)",
            [],
            |r| r.get(0),
        )?;
        let (label, lever) = if s.word {
            ("word", " - `-s` runs semantic search")
        } else if s.regex {
            ("regex", " - `-s` runs semantic search")
        } else {
            ("keyword", "")
        };
        agrep_core::errln!(
            "{label}: 0 matching rows across {} indexed messages{lever}",
            agrep_core::surface::number(n.max(0) as usize)
        );
    }
    if !s.json {
        agrep_core::coverage::disclose(coverage);
        if let Some(line) = f
            .chat
            .as_deref()
            .and_then(|chat| agrep_core::forks::notice(chat, &f.inherited))
        {
            agrep_core::errln!("{line}");
        }
        for line in after_miss {
            agrep_core::errln!("{line}");
        }
        if let Some(line) = policy.zip(excluded).and_then(|(p, n)| p.notice(n, "hit")) {
            agrep_core::errln!("{line}");
        }
    }
    render::note_no_auto(no_auto && !keyword_only_miss);
    Ok(if result.total != 0 {
        0
    } else if !style.checked && !style.semantic {
        2
    } else {
        1
    })
}
/// 0.4.9's explicit `-s` miss: whether keyword rows match, then the meaning lane's verdict
/// over the rows it embedded (py/search.py:8105-8125, py/display_policy.py:1332-1339).
fn meaning_miss(
    keyword_total: Option<usize>,
    pattern: &str,
    coverage: &semantic::Coverage,
) -> Vec<String> {
    let keyword = keyword_total.map(|n| match n {
        0 => "no confident meaning match, and no prose match either".to_owned(),
        n => format!(
            "no confident meaning match; {n} prose match(es) exist - {}",
            agrep_core::surface::shell(&["agrep".into(), pattern.into(), "--lexical".into()])
        ),
    });
    keyword
        .into_iter()
        .chain([format!(
            "semantic: no match among {} embedded rows",
            coverage.embedded
        )])
        .collect()
}
pub(crate) fn more(handle: &str) -> Result<i32> {
    let root = agrep_core::data::DataRoot::discover()?;
    root.validate(false)?;
    // 0.4.9 logged a continuation's refusal as the bare line (py/search.py:7325-7327).
    let frozen = agrep_core::pages::load(&root, handle)
        .map_err(|e| super::usage::refusal("", format!("{e:#}")))?;
    let page = agrep_core::pages::select(frozen);
    let more = agrep_core::pages::save(&root, &page.remainder)?;
    agrep_core::surface::write(&page.text)?;
    if more.is_some() || !page.remainder.records.is_empty() || page.remainder.larger_argv.is_some()
    {
        agrep_core::errln!(
            "{}",
            agrep_core::pages::footer(&page.remainder, more.as_deref())
        );
    }
    Ok(0)
}
/// 0.4.9's answer to an explicit `-s` whose meaning lane did not answer: its one stderr
/// line, or under `--json` its `semantic-unavailable` record, and exit 2
/// (py/search.py:7686-7719).
fn unavailable(
    s: &Search,
    query: &str,
    status: serde_json::Value,
    self_exclusion: serde_json::Value,
    freshness: serde_json::Value,
) -> Result<i32> {
    if !s.json {
        agrep_core::errln!("{}", semantic::unavailable_notice(&status));
        return Ok(2);
    }
    let engine = if status["state"] == "query-rejected" {
        "semantic:policy"
    } else {
        "semantic:unavailable"
    };
    let record = serde_json::json!({
        "kind":"agrep-meta","query":query,"engine":engine,"hits":[],
        "error":{"code":"semantic-unavailable"},"semantic":status,
        "self_exclusion":self_exclusion,
        "completeness":{"shown":0,"total":0,"total_basis":"floor",
            "unit":if s.chats {"chat"} else {"row"},"truncated":true,
            "action_unavailable_reason":"the query did not produce a result set"},
        "filter_coverage":{"empty_dimensions":[],"checked":false,
            "reason":"the query returned before the index was filtered"},
        "freshness":freshness,"semantic_coverage":null,
    });
    agrep_core::surface::line(&agrep_core::json_output::to_string(&record)?)?;
    Ok(2)
}
/// 0.4.9's `-l` heads over a meaning page: lane confidence folds into score, and a meaning
/// row's score is its keyword formula (py/search.py:1892-1898, 7584).
fn meaning_heads(hits: &mut [agrep_core::query::Hit], now: i64) {
    use agrep_core::query::meaning_score;
    hits.sort_by(|a, b| {
        meaning_score(b, now)
            .total_cmp(&meaning_score(a, now))
            .then(b.ts.cmp(&a.ts))
            .then_with(|| a.session.cmp(&b.session))
            .then(a.turn.cmp(&b.turn))
            .then_with(|| a.who.cmp(&b.who))
    });
}

fn query_options(
    s: &Search,
    pattern: &str,
    f: &agrep_core::query::Filters,
    tools: bool,
    compact_page: bool,
) -> (Options, usize) {
    // 0.4.9's default page outside the compact profile (py/search.py:7398-7399).
    let default_rows = if s.semantic && !compact_page { 10 } else { 40 };
    let options = Options {
        pattern: pattern.to_owned(),
        regex: s.regex,
        word: s.word,
        sort: s.sort.clone(),
        limit: if s.count || s.count_by_tier {
            0
        } else {
            s.max.unwrap_or(default_rows)
        },
        exact: s.count || s.count_by_tier || s.max == Some(0),
        count_only: s.count || s.count_by_tier,
        count_by_tier: s.count_by_tier,
        content: !s.lexical
            && !s.json
            && !s.flat
            && !s.count
            && !s.count_by_tier
            && !s.chats
            && (compact_page || std::io::stdout().is_terminal()),
        chats: s.chats,
        tools: tools
            || f.who
                .as_deref()
                .is_some_and(|who| who.split(',').any(|w| w == "tool")),
        filters: f.clone(),
    };
    (options, default_rows)
}

fn stopped(
    s: &Search,
    pattern: &str,
    request: caller::Request<'_>,
    no_auto: bool,
    error: (&str, Option<&str>),
) -> Result<i32> {
    if s.json {
        let unit = if s.chats { "chat" } else { "row" };
        let query = serde_json::json!(pattern.trim());
        super::usage::stopped(Some(unit), query, "query:error", error, request, no_auto)?;
    }
    Ok(2)
}

#[cfg(test)]
mod tests {
    use super::*;
    // Expected order is agrep 0.4.9's `-s "how do we rotate secret credentials" -l --json`
    // on a six-chat fixture; `now` reproduces that run's scores.
    #[test]
    fn meaning_chat_heads_rank_by_score_as_0_4_9() {
        let mut hits: Vec<_> = [
            ("f000", 1792058906000, 0.882),
            ("f002", 1792231706000, 0.869),
            ("f005", 1792490906000, 0.845),
            ("f003", 1792318106000, 0.836),
        ]
        .map(|(session, ts, score)| agrep_core::query::Hit {
            session: session.into(),
            who: "agent".into(),
            ts,
            score,
            matched: "semantic".into(),
            ..Default::default()
        })
        .into();
        meaning_heads(&mut hits, 1793787066000);
        let heads: Vec<_> = hits
            .iter()
            .map(|h| {
                (
                    h.session.as_str(),
                    agrep_core::query::meaning_score(h, 1793787066000),
                )
            })
            .collect();
        assert_eq!(
            heads,
            [
                ("f005", 0.3806),
                ("f003", 0.3448),
                ("f002", 0.3281),
                ("f000", 0.2972)
            ]
        );
    }
    // Expected lines are 0.4.9's explicit `-s` miss (py/search.py:8105-8125,
    // py/display_policy.py:1332-1339).
    #[test]
    fn explicit_meaning_miss_names_the_meaning_lane() {
        let coverage = semantic::Coverage {
            embedded: 31,
            total: 31,
        };
        assert_eq!(
            meaning_miss(Some(0), "how did we fix it", &coverage),
            [
                "no confident meaning match, and no prose match either",
                "semantic: no match among 31 embedded rows"
            ]
        );
        assert_eq!(
            meaning_miss(Some(24), "wedged rows", &coverage),
            [
                "no confident meaning match; 24 prose match(es) exist - agrep 'wedged rows' --lexical",
                "semantic: no match among 31 embedded rows"
            ]
        );
        assert_eq!(
            meaning_miss(None, "wedged", &coverage),
            ["semantic: no match among 31 embedded rows"]
        );
    }
}
