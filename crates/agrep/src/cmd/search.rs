use crate::{compact, ready, Search};
use agrep_core::{
    caller, hybrid,
    query::Options,
    render::{self, Style},
    semantic, store,
};
use anyhow::{bail, Result};
use std::io::IsTerminal;
pub(crate) fn run(s: Search, no_auto: bool, color: &str) -> Result<i32> {
    if s.semantic && !semantic::installed(&agrep_core::data::DataRoot::discover()?) {
        eprintln!("semantic model not installed: run agrep setup");
        return Ok(2);
    }
    let root = ready(no_auto)?;
    semantic::schedule(&root)?;
    let c = store::reader(&root)?;
    let mut f = s.scope.filters()?;
    agrep_core::coverage::validate(&mut f)?;
    let coverage = agrep_core::coverage::dimensions(&c, &f)?;
    if let Some(chat) = &f.chat {
        let candidates = agrep_core::handles::candidates(&c, chat)?;
        if candidates.len() != 1 {
            bail!("chat is missing or ambiguous");
        }
        f.chat = Some(candidates[0].clone());
    }
    let pattern = s.pattern.join(" ");
    let identity = caller::Identity::detect();
    if !s.json && !s.scope.include_self && (s.scope.no_self || identity.agent_context) {
        identity.disclose(s.scope.no_self);
    }
    let caller = caller::Caller::resolve(&c, identity)?;
    let options = Options {
        pattern: pattern.clone(),
        regex: s.regex,
        word: s.word,
        sort: s.sort.clone(),
        limit: if s.count || s.count_by_tier {
            0
        } else {
            s.max.unwrap_or(40)
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
            && (compact(s.classic) || std::io::stdout().is_terminal()),
        chats: s.chats,
        tools: root.config()?.tools
            || f.who
                .as_deref()
                .is_some_and(|who| who.split(',').any(|w| w == "tool")),
        filters: f.clone(),
    };
    let include_self = s.scope.include_self || (s.scope.chat.is_some() && !s.scope.no_self);
    let automatic = !s.semantic
        && !s.lexical
        && !s.regex
        && !s.word
        && !s.count
        && !s.count_by_tier
        && !s.coverage
        && s.sort == "score"
        && f.who.as_deref() != Some("tool")
        && compact(s.classic)
        && root.config()?.embeddings != "off"
        && semantic::eligible(&pattern)
        && semantic::installed(&root);
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
    let keyword = if s.semantic {
        Default::default()
    } else {
        caller::search(&c, &options, &caller, include_self, s.scope.no_self)?
    };
    let (result, semantic_coverage) = if let Some(lane) = lane {
        match lane.and_then(|work| {
            caller::scoped(&c, &caller, include_self, s.scope.no_self, || {
                semantic::finish(&c, work)
            })
        }) {
            Ok(mut found) => {
                caller::filter_results(
                    &c,
                    &mut found.result,
                    &caller,
                    include_self,
                    s.scope.no_self,
                )?;
                let result = if s.semantic {
                    found.result
                } else {
                    hybrid::merge(
                        keyword,
                        Some(found.result),
                        options.limit,
                        !s.scope.all_side_chats,
                        hybrid::Lane::Compact,
                    )
                };
                (result, Some(found.coverage))
            }
            Err(error) => {
                agrep_core::logging::semantic_failure(&root, &error);
                let fallback = if s.semantic {
                    caller::search(&c, &options, &caller, include_self, s.scope.no_self)?
                } else {
                    keyword
                };
                (fallback, None)
            }
        }
    } else {
        (
            keyword,
            if s.coverage {
                Some(semantic::coverage(&c)?)
            } else {
                None
            },
        )
    };
    let compact = compact(s.classic);
    let mut full_argv = vec![
        "agrep".to_string(),
        "search".into(),
        "--max".into(),
        "0".into(),
        "--sort".into(),
        s.sort.clone(),
        "--color".into(),
        color.into(),
    ];
    for (flag, value) in [
        ("--agent", &s.scope.agent),
        ("--project", &s.scope.project),
        ("--exclude-project", &s.scope.exclude_project),
        ("--model", &s.scope.model),
        ("--who", &s.scope.who),
        ("--no-who", &s.scope.no_who),
        ("--chat", &s.scope.chat),
        ("--since", &s.scope.since),
        ("--until", &s.scope.until),
    ] {
        if let Some(v) = value {
            full_argv.extend([flag.into(), v.clone()]);
        }
    }
    for (flag, on) in [
        ("--here", s.scope.here),
        ("--soft", s.scope.soft),
        ("--no-meta", s.scope.no_meta),
        ("--self", s.scope.include_self),
        ("--no-self", s.scope.no_self),
        ("--all-side-chats", s.scope.all_side_chats),
        ("--no-side", s.scope.no_side),
        ("--regex", s.regex),
        ("--word", s.word),
        ("--chats", s.chats),
        ("--classic", s.classic),
        ("--flat", s.flat),
        ("--json", s.json),
        ("--lexical", s.lexical),
        ("--semantic", s.semantic),
        ("--no-auto", no_auto),
    ] {
        if on {
            full_argv.push(flag.into());
        }
    }
    full_argv.extend(["--".into(), pattern.clone()]);
    let style = Style {
        json: s.json,
        flat: s.flat
            || (!compact
                && !(color == "always" || (color == "auto" && std::io::stdout().is_terminal()))),
        compact,
        count: s.count,
        tiers: s.count_by_tier,
        chats: s.chats,
        semantic: s.semantic && semantic_coverage.is_some(),
        checked: agrep_core::render::checked(no_auto),
        full_argv,
        color: color == "always" || (color == "auto" && std::io::stdout().is_terminal()),
        data_root: Some(root),
        requested: s.max,
        diversify: !s.scope.all_side_chats,
        caller_unknown: caller.identity.agent_context && !caller.resolved,
        self_exclusion: if f.chat.is_some() && !s.scope.no_self {
            serde_json::json!({"active":false,"reason":"chat-filter"})
        } else {
            caller.metadata(s.scope.include_self, s.scope.no_self)
        },
        filter_coverage: coverage.clone(),
        pending_sources: 0,
    };
    render::emit_semantic(&result, &pattern, &f, &style, semantic_coverage.as_ref())?;
    if !s.json {
        if let Some(coverage) = &semantic_coverage {
            if s.semantic || s.coverage || coverage.partial() {
                eprintln!("semantic {}/{} rows", coverage.embedded, coverage.total);
            }
        }
    }
    if result.total == 0 && !s.json && !s.count && !s.count_by_tier {
        let n = store::counts(&c)?["prose"].as_u64().unwrap_or(0);
        eprintln!(
            "keyword: 0 matching rows across {n} indexed messages{}",
            if style.checked {
                ""
            } else {
                "; freshness unchecked (--no-auto)"
            }
        );
    }
    if !s.json {
        agrep_core::coverage::disclose(&coverage);
    }
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
    let frozen = agrep_core::pages::load(&root, handle)?;
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
