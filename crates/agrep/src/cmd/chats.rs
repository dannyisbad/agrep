use crate::{ready, Chats};
use agrep_core::{
    history as h,
    query::{self, Options},
    render, store,
};
use anyhow::{bail, Result};
use serde_json::json;
use std::collections::HashMap;

pub fn run(args: Chats, no_auto: bool, color: &str) -> Result<i32> {
    if args.pattern.iter().any(|s| s.trim().is_empty()) {
        bail!("pattern arguments must not be empty")
    }
    let (root, census) = ready(no_auto)?;
    let c = store::reader(&root)?;
    let all = h::sessions(&c)?;
    let mut filters = args.scope.filters()?;
    h::validate_filters(&filters)?;
    if filters
        .since
        .zip(filters.until)
        .is_some_and(|(s, u)| s >= u)
    {
        bail!("--since must be before --until")
    }
    let pattern = args.pattern.join(" ");
    if args.pattern.len() == 1 && pattern.starts_with('@') && pattern.contains(':') {
        agrep_core::handles::parse(&pattern)?;
    }
    let identity = if args.pattern.len() == 1 {
        h::identity(&pattern).ok()
    } else {
        None
    };
    let direct = identity
        .as_ref()
        .is_some_and(|id| all.iter().any(|s| s.id.starts_with(id)));
    filters.no_side = !args.side && !direct;
    let mut hits = HashMap::new();
    let mut exact = true;
    if !pattern.is_empty() && !direct {
        let result = query::search(
            &c,
            &Options {
                pattern: pattern.clone(),
                regex: false,
                word: false,
                sort: "score".into(),
                limit: if args.max == 0 {
                    0
                } else {
                    20.max(args.max.saturating_mul(2))
                },
                exact: args.max == 0,
                count_only: false,
                count_by_tier: false,
                content: false,
                chats: true,
                tools: root.config()?.tools,
                filters: filters.clone(),
            },
        )?;
        exact = result.total_basis == "exact" && result.total <= result.hits.len();
        for (rank, hit) in result.hits.into_iter().enumerate() {
            hits.insert((hit.agent.clone(), hit.session.clone()), (rank, hit));
        }
    }
    let caller = agrep_core::caller::Caller::resolve(&c, agrep_core::caller::Identity::detect())?;
    let apply_self =
        !args.scope.include_self && (args.scope.no_self || (!pattern.is_empty() && !direct));
    let mut excluded = 0;
    let mut selected: Vec<_> = all
        .iter()
        .filter(|s| {
            if !h::in_scope(s, &filters) {
                return false;
            }
            if apply_self
                && !direct
                && agrep_core::caller::excludes(
                    &caller,
                    &s.agent,
                    &s.id,
                    hits.get(&(s.agent.clone(), s.id.clone()))
                        .map_or(0, |(_, hit)| hit.turn),
                    false,
                    args.scope.no_self,
                )
            {
                excluded += 1;
                return false;
            }
            if direct {
                return identity.as_ref().is_some_and(|id| s.id.starts_with(id));
            }
            let identity = format!("{} {} {}", s.id, s.project, s.title).to_lowercase();
            args.pattern
                .iter()
                .all(|p| identity.contains(&p.to_lowercase()))
                || hits.contains_key(&(s.agent.clone(), s.id.clone()))
        })
        .collect();
    selected.sort_by_key(|s| {
        (
            hits.get(&(s.agent.clone(), s.id.clone()))
                .map(|(rank, _)| *rank)
                .unwrap_or(usize::MAX),
            -s.last_ts,
            s.agent.clone(),
            s.id.clone(),
        )
    });
    let total = selected.len();
    if args.max > 0 {
        selected.truncate(args.max);
    }
    let pending_sources = crate::finish_census(&root, census)?;
    let checked = agrep_core::render::checked(no_auto);
    if args.json {
        let mut meta = json!({"kind":"agrep-meta","engine":"keyword","filters":filters,"freshness":render::freshness(checked,pending_sources),"self_exclusion":caller.metadata(!apply_self,args.scope.no_self),"self_excluded":excluded,"completeness":{"shown":selected.len(),"total":total,"total_basis":if exact{"exact"}else{"floor"},"truncated":selected.len()<total||!exact}});
        if selected.is_empty() {
            meta["hits"] = json!([]);
        }
        println!("{}", agrep_core::json_output::to_string(&meta)?);
    }
    for s in &selected {
        let latest = h::latest(&c, s)?;
        let latest_handle = latest.as_ref().map(|r| h::handle(&s.id, r));
        let turns = h::turn_count(&c, s)?;
        let hit = hits
            .get(&(s.agent.clone(), s.id.clone()))
            .map(|(_, hit)| hit);
        let followup = hit
            .map(|hit| hit.handle.clone())
            .or_else(|| latest_handle.clone())
            .unwrap_or_else(|| h::target(&s.id));
        if args.json {
            let mut row = json!({"session":s.id,"agent":s.agent,"project":s.project,"turns":turns,"first_ts":s.first_ts,"last_ts":s.last_ts,"first_text":s.title,"side":s.side,"self":caller.resolved && caller.agent == s.agent && caller.session == s.id,"session_handle":h::target(&s.id),"last_turn":latest.as_ref().map(|r|r.turn),"latest_handle":latest_handle,"match_source":if hit.is_some(){"content"}else{"identity"}});
            if let Some(hit) = hit {
                row["match_turn"] = json!(hit.turn);
                row["match_text"] = json!(h::one_line(&hit.text, 240));
                row["score"] = json!(hit.score);
                row["matched"] = json!(hit.matched);
                row["who"] = json!(hit.who);
                row["match_ts"] = json!(hit.ts);
                row["match_handle"] = json!(hit.handle);
            }
            println!("{}", agrep_core::json_output::to_string(&row)?);
        } else {
            println!(
                "{} {} {} {} {}t{}  {}{} · agrep around {}",
                super::paint(&h::target(&s.id), color),
                render::safe(&s.agent),
                render::safe(&s.project),
                h::age(hit.map_or(s.last_ts, |h| h.ts)),
                turns,
                if s.side { " [side chat]" } else { "" },
                if hit.is_some() { "match: " } else { "" },
                render::safe(&h::one_line(hit.map_or(&s.title, |h| &h.text), 96)),
                followup
            );
        }
    }
    if !args.json {
        eprintln!(
            "showing {} of {}{} matching chats, {}{}",
            selected.len(),
            total,
            if exact { "" } else { "+" },
            if pattern.is_empty() || direct {
                "newest first"
            } else {
                "best match first"
            },
            if !args.side && !direct {
                " (side chats hidden; --side shows them)"
            } else {
                ""
            }
        );
    }
    Ok(if !selected.is_empty() {
        0
    } else if checked && exact {
        1
    } else {
        2
    })
}
