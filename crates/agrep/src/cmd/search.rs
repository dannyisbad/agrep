use crate::{compact, finish_census, ready, Search};
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
    let stop = |error: (&str, Option<&str>)| -> Result<i32> {
        if s.json {
            let unit = if s.chats { "chat" } else { "row" };
            let query = serde_json::json!(pattern.trim());
            super::usage::stopped(Some(unit), query, "query:error", error, request, no_auto)?;
        }
        Ok(2)
    };
    if pattern.trim().is_empty() {
        // `agrep search` also lands here when a bare `agrep <word>` consumed a command word.
        eprintln!("empty pattern - add a word or phrase to search (a word that is also a command searches with: agrep search \"search\")");
        return stop(("empty-query", None));
    }
    if s.regex {
        if let Some(reason) = agrep_core::query::regex_error(pattern.trim()) {
            eprintln!("bad regex: {reason}");
            return stop(("invalid-regex", Some(&reason)));
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
        eprintln!("semantic model not installed: run agrep setup");
        return Ok(2);
    }
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
    let (root, census) = {
        let _span = agrep_core::semantic_profile::span("freshness_prepare");
        ready(no_auto)?
    };
    let c = store::reader(&root)?;
    let coverage = agrep_core::coverage::dimensions(&c, &f)?;
    if let Some(chat) = &f.chat {
        let Some(session) = super::usage::chat(&c, chat)? else {
            return stop(("chat-unresolved", None));
        };
        f.chat = Some(session);
    }
    let identity = caller::Identity::detect();
    if !s.json && !s.scope.include_self && (s.scope.no_self || identity.agent_context) {
        identity.disclose(s.scope.no_self, false);
    }
    let caller = caller::Caller::resolve(&c, identity)?;
    // 0.4.9's default page outside the compact profile (py/search.py:7398-7399).
    let default_rows = if s.semantic && !compact_page { 10 } else { 40 };
    let options = Options {
        pattern: pattern.clone(),
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
        tools: root.config()?.tools
            || f.who
                .as_deref()
                .is_some_and(|who| who.split(',').any(|w| w == "tool")),
        filters: f.clone(),
    };
    let include_self = s.scope.include_self || (s.scope.chat.is_some() && !s.scope.no_self);
    let options = caller::options(&options, &caller, include_self, s.scope.no_self);
    let automatic = !s.semantic
        && !s.lexical
        && !s.regex
        && !s.word
        && !s.coverage
        && s.sort == "score"
        && f.who.as_deref() != Some("tool")
        && compact_page
        && root.config()?.embeddings != "off"
        && semantic::eligible(&pattern)
        && semantic::installed(&root);
    if timing {
        eprintln!(
            "{}",
            serde_json::json!({"query_prepare_ms":query_start.elapsed().as_secs_f64()*1000.})
        );
    }
    let lane = if s.semantic || automatic {
        Some(semantic::begin(
            &root,
            &c,
            &options,
            !s.scope.all_side_chats,
        ))
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
        caller::search_with(
            &c,
            &options,
            &caller,
            include_self,
            s.scope.no_self,
            &mut plans,
        )?
    };
    if timing {
        agrep_core::semantic_profile::event(serde_json::json!({
            "retrieval":keyword.timings,"examined":keyword.examined,"blocks":keyword.blocks
        }));
    }
    let mut keyword_ms = if s.semantic {
        0.
    } else {
        keyword_start.elapsed().as_secs_f64() * 1000.
    };
    let (mut result, semantic_coverage, keyword_only) = if let Some(lane) = lane {
        match lane.and_then(|work| semantic::finish(&c, work)) {
            Ok(mut found) => {
                caller::decorate_semantic(&c, &mut found.result, &caller)?;
                semantic::mark_weak(
                    &c,
                    &options,
                    &mut found.result.hits,
                    !keyword.hits.is_empty(),
                )?;
                let result = if s.semantic {
                    found.result
                } else if keyword.hits.is_empty()
                    && found.result.hits.iter().all(|h| h.score < semantic::STRONG)
                {
                    // 0.4.9 kept a page of only sub-strong neighbors a miss; `-s` shows
                    // them (py/search.py:7956-7960).
                    keyword
                } else {
                    hybrid::merge(
                        keyword,
                        Some(found.result),
                        options.limit,
                        !s.scope.all_side_chats,
                    )
                };
                (result, Some(found.coverage), false)
            }
            Err(error) => {
                agrep_core::logging::semantic_failure(&root, &error);
                let fallback = if s.semantic {
                    let tick = std::time::Instant::now();
                    let _span = agrep_core::semantic_profile::span("keyword_fallback");
                    let result =
                        caller::search(&c, &options, &caller, include_self, s.scope.no_self)?;
                    keyword_ms += tick.elapsed().as_secs_f64() * 1000.;
                    result
                } else {
                    keyword
                };
                (fallback, None, true)
            }
        }
    } else {
        (keyword, None, true)
    };
    if timing {
        eprintln!("{}", serde_json::json!({"keyword_ms":keyword_ms}));
    }
    let style_semantic = s.semantic && semantic_coverage.is_some();
    if style_semantic && s.chats && s.sort == "score" {
        meaning_heads(&mut result.hits, chrono::Utc::now().timestamp_millis());
    }
    // 0.4.9's reruns of this search (py/search.py:5926-6036): every filter as one token,
    // then the page's own surface and a page size.
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
            .chain(["-n".into(), n.to_string(), "--".into(), pattern.clone()])
            .collect()
    };
    let coverage_hits = if s.coverage {
        // 0.4.9 ranked the coverage lane by coverage whatever --sort said.
        let ranked = Options {
            sort: "score".into(),
            ..options.clone()
        };
        let mut recovered = agrep_core::query::coverage(&c, &ranked)?;
        recovered.hits.retain(|h| {
            !result.hits.iter().any(|old| {
                old.agent == h.agent
                    && old.session == h.session
                    && old.turn == h.turn
                    && old.who == h.who
            })
        });
        Some(recovered)
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
            .withheld(&c, &options, f.chat.as_deref())?
            .map(caller::Withheld::total),
        _ => None,
    };
    let pending_sources = {
        let _span = agrep_core::semantic_profile::span("freshness_join");
        finish_census(&root, census)?
    };
    result.pending_sources = pending_sources;
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
        agrep_core::query::session_counts(&c, &options, &sessions, &mut plans)?
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
        flat: s.flat
            || (!compact_page
                && !(color == "always" || (color == "auto" && std::io::stdout().is_terminal()))),
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
            .filter(|_| exhaustible || s.json)
            .map(|n| rerun(surface, n)),
        exhaustive_argv: (s.json && exhaustible && s.max != Some(0)).then(|| rerun(surface, 0)),
        color: color == "always" || (color == "auto" && std::io::stdout().is_terminal()),
        data_root: Some(root),
        requested: s.max,
        diversify: !s.scope.all_side_chats,
        caller_unknown: caller.identity.agent_context && !caller.resolved,
        self_exclusion: caller.metadata(request, excluded),
        filter_coverage: coverage.clone(),
        pending_sources,
        session_hits,
    };
    if let Some(line) = agrep_core::query::meta_notice(result.meta_dropped, result.meta_retained) {
        eprintln!("{line}");
    }
    {
        let _span = agrep_core::semantic_profile::span("render");
        render::emit_semantic(&result, &pattern, &f, &style, semantic_coverage.as_ref())?;
    }
    if let Some(recovered) = coverage_hits {
        eprintln!(
            "coverage lane (forced): {} additional rows",
            recovered.hits.len()
        );
        agrep_core::surface::line("coverage recovery (partial term matches):")?;
        if recovered.hits.is_empty() {
            agrep_core::surface::line("no additional evidence")?;
        }
        for hit in recovered.hits {
            agrep_core::surface::line(&format!(
                "{} {} {}: {}",
                hit.handle,
                hit.agent,
                hit.who,
                agrep_core::surface::safe(&hit.snippet, false)
            ))?;
        }
    }
    // 0.4.9 named a coverage gap on a miss, and on a page of hits only in its terminal
    // footer or a compact page (py/search.py:8121-8126, 8381-8387, 8482-8485).
    if let Some(coverage) = semantic_coverage.as_ref().filter(|c| c.gap()) {
        if !s.json
            && !s.count
            && !s.count_by_tier
            && (result.hits.is_empty() || compact_page || std::io::stderr().is_terminal())
        {
            eprintln!("semantic {}/{} rows", coverage.embedded, coverage.total);
        }
    }
    // 0.4.9 let an empty filter dimension's line stand alone as the zero's explanation.
    let empty_dimension = coverage["empty_dimensions"]
        .as_array()
        .is_some_and(|d| !d.is_empty());
    if result.total == 0 && !s.json && !s.count && !s.count_by_tier && !empty_dimension {
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
        eprintln!(
            "{label}: 0 matching rows across {} indexed messages{lever}",
            agrep_core::surface::number(n.max(0) as usize)
        );
    }
    if !s.json {
        agrep_core::coverage::disclose(&coverage);
        if let Some(line) = policy.zip(excluded).and_then(|(p, n)| p.notice(n, "hit")) {
            eprintln!("{line}");
        }
    }
    render::note_no_auto(no_auto);
    Ok(if result.total != 0 {
        0
    } else if !style.checked && !style.semantic {
        2
    } else {
        1
    })
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
    if more.is_some() || !page.remainder.records.is_empty() {
        eprintln!(
            "{}",
            agrep_core::pages::footer(&page.remainder, more.as_deref())
        );
    }
    Ok(0)
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
}
