use crate::{ready, Chats};
use agrep_core::{
    history as h,
    query::{self, Options},
    render, store,
};
use anyhow::Result;
use serde_json::json;
use std::collections::HashMap;

pub fn run(args: Chats, no_auto: bool, color: &str) -> Result<i32> {
    // 0.4.9 read the time bounds before it checked the index (py/search.py:8743-8758).
    let mut filters = args.scope.filters()?;
    h::validate_filters(&filters)?;
    let (root, census) = ready(no_auto)?;
    let detected = agrep_core::caller::Identity::detect();
    let tools = root.config()?.tools;
    let pattern = args.pattern.join(" ");
    if args.pattern.len() == 1 && pattern.starts_with('@') && pattern.contains(':') {
        agrep_core::handles::parse(&pattern)?;
    }
    let identity = if args.pattern.len() == 1 {
        h::identity(&pattern).ok()
    } else {
        None
    };
    crate::finish_census(&root, census)?;
    let c = store::reader(&root)?;
    let all = h::sessions(&c)?;
    let direct = identity
        .as_ref()
        .is_some_and(|id| all.iter().any(|s| s.id.starts_with(id)));
    filters.no_side = !args.side && !direct;
    if args.scope.no_self && !args.scope.include_self && !args.json {
        detected.disclose(true, false);
    }
    let caller = agrep_core::caller::Caller::resolve(&c, detected)?;
    // A bare listing is identity, not an echo: only --no-self narrows it.
    let request = agrep_core::caller::Request {
        include_self: args.scope.include_self,
        no_self: args.scope.no_self,
        waived: if direct {
            Some("chat-filter")
        } else if pattern.is_empty() {
            Some("not-applicable")
        } else {
            None
        },
    };
    let policy = caller.policy(request);
    let apply_self =
        !args.scope.include_self && (args.scope.no_self || (!pattern.is_empty() && !direct));
    let selection = Selection {
        args: &args,
        filters: &filters,
        caller: &caller,
        identity: identity.as_deref(),
        apply_self,
        direct,
    };
    let (hits, exact) = content_hits(&c, &selection, &pattern, tools)?;
    let (selected, excluded, total) = select_chats(&selection, &all, &hits);
    let checked = agrep_core::render::checked(no_auto);
    let truncated = selected.len() < total || !exact;
    // 0.4.9 named a page cut by -n with its larger rerun (py/search.py:8888-8912).
    let more_command = (args.max > 0 && truncated).then(|| more_command(&args, &filters, no_auto));
    // A listed chat proves the filters matched; only an empty page names empty ones.
    let coverage = if selected.is_empty() {
        agrep_core::coverage::dimensions(&c, &filters)?
    } else {
        json!({"empty_dimensions":[],"checked":true})
    };
    if args.json {
        let mut completeness = json!({"shown":selected.len(),"total":total,"total_basis":if exact{"exact"}else{"floor"},"unit":"matching chat","truncated":truncated});
        if let Some(command) = &more_command {
            completeness["more_command"] = json!(command);
        }
        let mut meta = json!({"kind":"agrep-meta","completeness":completeness,"filter_coverage":coverage,"freshness":render::machine_freshness(no_auto),"self_exclusion":caller.metadata(request,None),"self_excluded":excluded});
        if selected.is_empty() {
            meta["hits"] = json!([]);
        }
        agrep_core::outln!("{}", agrep_core::json_output::to_string(&meta)?)?;
    }
    let index = agrep_core::handles::SessionIndex::load(&c)?;
    print_chats(
        &c,
        &selected,
        &hits,
        &index,
        RowStyle {
            args: &args,
            caller: &caller,
            color,
            tools,
            apply_self,
            direct,
            terms: &preview_terms(&pattern),
        },
    )?;
    if !args.json {
        if selected.is_empty() && total == 0 && exact {
            let side = filters.no_side && side_chats_match(&c, &selection, &all, &pattern, tools)?;
            agrep_core::errln!(
                "no chats match{}",
                if side {
                    "; some side chats do (--side shows them)"
                } else {
                    ""
                }
            );
        } else {
            agrep_core::errln!(
                "showing {} of {}{} matching chat{}, {}{}{}",
                selected.len(),
                total,
                if exact { "" } else { "+" },
                if total == 1 { "" } else { "s" },
                if pattern.is_empty() || direct {
                    "newest first"
                } else {
                    "best match first"
                },
                if !args.side && !direct {
                    " (side chats hidden; --side shows them)"
                } else {
                    ""
                },
                more_command
                    .as_ref()
                    .map(|command| format!(" · larger page: {command}"))
                    .unwrap_or_default()
            );
        }
        if let Some(notice) = policy.and_then(|p| p.notice(excluded, "chat")) {
            agrep_core::errln!("{notice}");
        }
        agrep_core::coverage::disclose(&coverage);
        render::note_no_auto(no_auto);
    }
    Ok(if !selected.is_empty() {
        0
    } else if checked && exact {
        1
    } else {
        2
    })
}

type Hits = HashMap<(String, String), (usize, query::Hit)>;
struct Selection<'a> {
    args: &'a Chats,
    filters: &'a query::Filters,
    caller: &'a agrep_core::caller::Caller,
    identity: Option<&'a str>,
    apply_self: bool,
    direct: bool,
}
fn select_chats<'a>(
    ctx: &Selection<'_>,
    all: &'a [h::Session],
    hits: &Hits,
) -> (Vec<&'a h::Session>, usize, usize) {
    let mut excluded = 0;
    let mut selected: Vec<_> = all
        .iter()
        .filter(|s| {
            if !h::in_scope(s, ctx.filters) {
                return false;
            }
            let hit = hits.get(&(s.agent.clone(), s.id.clone()));
            let matched = if ctx.direct {
                ctx.identity.as_ref().is_some_and(|id| s.id.starts_with(id))
            } else {
                // 0.4.9's haystack, less the concept 0.5 does not compute
                // (py/search.py:8579-8583).
                let identity = format!(
                    "{} {} {} {} {}",
                    s.id,
                    s.agent,
                    h::project_leaf(&s.project),
                    s.project,
                    s.title
                )
                .to_lowercase();
                ctx.args
                    .pattern
                    .iter()
                    .all(|p| identity.contains(&p.to_lowercase()))
                    || hit.is_some()
            };
            if !matched {
                return false;
            }
            if ctx.apply_self
                && !ctx.direct
                && agrep_core::caller::excludes(
                    ctx.caller,
                    &s.agent,
                    &s.id,
                    hit.map_or(0, |(_, hit)| hit.turn),
                    false,
                    ctx.args.scope.no_self,
                )
            {
                excluded += 1;
                return false;
            }
            true
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
    if ctx.args.max > 0 {
        selected.truncate(ctx.args.max);
    }
    (selected, excluded, total)
}

/// Whether a page that found nothing would find side chats, so it names --side only then.
fn side_chats_match(
    c: &rusqlite::Connection,
    ctx: &Selection<'_>,
    all: &[h::Session],
    pattern: &str,
    tools: bool,
) -> Result<bool> {
    let mut filters = ctx.filters.clone();
    filters.no_side = false;
    let ctx = Selection {
        filters: &filters,
        ..*ctx
    };
    let (hits, _) = content_hits(c, &ctx, pattern, tools)?;
    Ok(select_chats(&ctx, all, &hits).0.iter().any(|s| s.side))
}

fn preview_terms(pattern: &str) -> Vec<String> {
    let mut terms: Vec<String> = Vec::new();
    for token in agrep_core::boundary_rank::query_tokens(pattern) {
        let term = token.to_lowercase();
        if !terms.contains(&term) {
            terms.push(term);
        }
    }
    terms
}

/// A one-line preview of at most `max` characters: the start of `text`, unless a later
/// window holds more of the query's distinct `terms`; then the window centred on the
/// terms it holds, after a cut marker.
fn preview(text: &str, terms: &[String], max: usize) -> String {
    let line: Vec<char> = text
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .collect();
    let head = || line.iter().take(max).collect::<String>();
    if line.len() <= max || max < 2 || terms.is_empty() {
        return head();
    }
    // Each term occurrence as (term, first char, end char) of `line`; the first few per
    // term suffice, and keep a long row text without a snippet cheap.
    let mut lower = String::new();
    let mut char_at = Vec::new();
    for (i, c) in line.iter().enumerate() {
        for folded in c.to_lowercase() {
            lower.push(folded);
            char_at.resize(lower.len(), i);
        }
    }
    let occurrences: Vec<(usize, usize, usize)> = terms
        .iter()
        .enumerate()
        .flat_map(|(t, term)| {
            let (lower, char_at) = (&lower, &char_at);
            lower
                .match_indices(term.as_str())
                .take(64)
                .map(move |(at, m)| (t, char_at[at], char_at[at + m.len() - 1] + 1))
        })
        .collect();
    let held = |start: usize, len: usize| {
        let mut seen = vec![false; terms.len()];
        for &(t, a, b) in &occurrences {
            seen[t] |= start <= a && b <= start + len;
        }
        seen.iter().filter(|s| **s).count()
    };
    // A moved window spends one character on the cut marker.
    let len = max - 1;
    let last = line.len() - len;
    let (mut best, mut start) = (held(0, max), 0);
    for &(_, _, b) in &occurrences {
        let s = b.saturating_sub(len).clamp(1, last);
        let n = held(s, len);
        if n > best || (n == best && start > 0 && s < start) {
            (best, start) = (n, s);
        }
    }
    if start == 0 {
        return head();
    }
    let inside = occurrences
        .iter()
        .filter(|&&(_, a, b)| start <= a && b <= start + len);
    let a = inside.clone().map(|o| o.1).min().unwrap_or(start);
    let b = inside.map(|o| o.2).max().unwrap_or(start + len);
    let centred = a.saturating_sub((len - (b - a)) / 2);
    let start = centred.clamp(b.saturating_sub(len).max(1), a.min(last));
    std::iter::once('…')
        .chain(line[start..start + len].iter().copied())
        .collect()
}

fn more_command(args: &Chats, filters: &query::Filters, no_auto: bool) -> String {
    let mut argv: Vec<String> = vec!["agrep".into(), "chats".into()];
    if let Some(agent) = &args.scope.agent {
        argv.extend(["--agent".into(), agent.clone()]);
    }
    if let Some(project) = &filters.project {
        argv.push(format!("--project={project}"));
    }
    if let Some(project) = &args.scope.exclude_project {
        argv.push(format!("--exclude-project={project}"));
    }
    for (flag, value) in [
        ("--since", &args.scope.since),
        ("--until", &args.scope.until),
    ] {
        if let Some(value) = value {
            argv.extend([flag.into(), value.clone()]);
        }
    }
    for (flag, on) in [
        ("--side", args.side),
        ("--self", args.scope.include_self),
        ("--no-self", args.scope.no_self),
        ("--json", args.json),
        ("--no-auto", no_auto),
    ] {
        if on {
            argv.push(flag.into());
        }
    }
    argv.extend(["-n".into(), 80.max(args.max * 4).to_string()]);
    if !args.pattern.is_empty() {
        argv.push("--".into());
        argv.extend(args.pattern.iter().cloned());
    }
    agrep_core::surface::shell(&argv)
}

struct RowStyle<'a> {
    args: &'a Chats,
    caller: &'a agrep_core::caller::Caller,
    color: &'a str,
    tools: bool,
    apply_self: bool,
    direct: bool,
    /// The query's distinct terms, lowercased, for choosing preview windows.
    terms: &'a [String],
}
fn print_chats(
    c: &rusqlite::Connection,
    selected: &[&h::Session],
    hits: &Hits,
    index: &agrep_core::handles::SessionIndex,
    style: RowStyle<'_>,
) -> Result<()> {
    let RowStyle {
        args,
        caller,
        color,
        tools,
        apply_self,
        direct,
        terms,
    } = style;
    // 0.4.9 named each chat by its shortest unique prefix (py/search.py:8862-8866).
    let short = |handle: &str, session: &str| {
        handle.replacen(
            &h::target(session),
            &format!("@{}", index.target(session)),
            1,
        )
    };
    for s in selected {
        let latest = h::latest_claim(c, s, tools)?;
        let latest_handle = latest.as_ref().map(|r| short(&h::handle(&s.id, r), &s.id));
        let turns = h::turn_count(c, s)?;
        let hit = hits
            .get(&(s.agent.clone(), s.id.clone()))
            .map(|(_, hit)| hit);
        // 0.4.9 previewed a content match by its snippet, falling back to the row's text
        // (py/search.py:8875-8876).
        let matched_text = hit.map(|hit| {
            if hit.snippet.is_empty() {
                hit.text.as_str()
            } else {
                hit.snippet.as_str()
            }
        });
        let followup = hit
            .map(|hit| short(&hit.handle, &s.id))
            .or_else(|| latest_handle.clone())
            .unwrap_or_else(|| format!("@{}", index.target(&s.id)));
        if args.json {
            let mut row = json!({"session":s.id,"agent":s.agent,"project":s.project,"turns":turns,"first_ts":s.first_ts,"last_ts":s.last_ts,"first_text":s.title.trim(),"side":s.side,"session_handle":format!("@{}", index.target(&s.id)),"last_turn":latest.as_ref().map(|r|r.turn),"latest_handle":latest_handle,"match_source":if hit.is_some(){"content"}else{"identity"}});
            // 0.4.9 marked a chat's own family only when it withheld the caller's window
            // (py/search.py:8838-8846).
            if apply_self && caller.resolved && !direct {
                row["self"] = json!(
                    caller.agent == s.agent
                        && (caller.session == s.id || caller.members.contains(&s.id))
                );
            }
            if let Some(hit) = hit {
                row["match_turn"] = json!(hit.turn);
                row["match_text"] = json!(preview(matched_text.unwrap_or_default(), terms, 240));
                row["score"] = json!(hit.score);
                if hit.matched != "phrase" {
                    row["matched"] = json!(hit.matched);
                }
                row["who"] = json!(hit.who);
                row["match_ts"] = json!(hit.ts);
                row["match_handle"] = json!(short(&hit.handle, &s.id));
            }
            agrep_core::outln!("{}", agrep_core::json_output::to_string(&row)?)?;
        } else {
            agrep_core::outln!(
                "{} {} {} {} {}t{}  {}{} · agrep around {}",
                super::paint(&format!("@{}", index.target(&s.id)), color),
                render::safe(&s.agent),
                render::safe(&render::project(&s.project)),
                render::age(hit.map_or(s.last_ts, |h| h.ts)),
                turns,
                if s.side { " [side chat]" } else { "" },
                if hit.is_some() { "match: " } else { "" },
                render::safe(&match matched_text {
                    Some(text) => preview(text, terms, 96),
                    None => h::one_line(&s.title, 96),
                }),
                followup
            )?;
        }
    }
    Ok(())
}

fn content_hits(
    c: &rusqlite::Connection,
    ctx: &Selection<'_>,
    pattern: &str,
    tools: bool,
) -> Result<(Hits, bool)> {
    let mut hits = HashMap::new();
    let mut exact = true;
    // 0.4.9's content lookup found nothing for a pattern without a word, such as `-`, and
    // still matched it against chat identity (py/search.py:8764-8801).
    if !ctx.direct && !agrep_core::boundary_rank::query_tokens(pattern).is_empty() {
        let options = Options {
            pattern: pattern.to_owned(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: if ctx.args.max == 0 {
                0
            } else {
                20.max(ctx.args.max.saturating_mul(2))
            },
            exact: ctx.args.max == 0,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: true,
            tools,
            filters: ctx.filters.clone(),
        };
        // Content heads come from outside the withheld rows, so a chat's head can sit
        // before the caller's window.
        let result = query::search(
            c,
            &agrep_core::caller::options(
                &options,
                ctx.caller,
                !ctx.apply_self,
                ctx.args.scope.no_self,
            ),
        )?;
        exact = result.total_basis == "exact" && result.total <= result.hits.len();
        for (rank, hit) in result.hits.into_iter().enumerate() {
            hits.insert((hit.agent.clone(), hit.session.clone()), (rank, hit));
        }
    }
    Ok((hits, exact))
}
