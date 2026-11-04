mod page;
use crate::{ready, Recall};
use agrep_core::{
    caller::{self, Caller, Identity},
    evidence::{self, Request, Window},
    handles, hybrid,
    query::{Hit, Options},
    render, semantic, store, surface,
};
use anyhow::{bail, Result};
use serde_json::json;
use std::{
    collections::{BTreeSet, HashMap, HashSet},
    io::IsTerminal,
};
/// One chat's evidence: the first hit's head over every window of the chat.
struct Block {
    hit: Hit,
    window: Window,
    /// The turns the chat's windows centered on.
    hit_turns: BTreeSet<i64>,
    /// The query each merged window came from, in merge order.
    queries: Vec<usize>,
}
/// Bag-of-words scatter and sub-strong meaning matches: evidence that earns one line
/// rather than a transcript.
fn weak(h: &Hit) -> bool {
    matches!(h.matched.as_str(), "all-terms" | "content-terms")
        || (h.matched == "semantic" && h.score < 0.84)
}
/// A block's head: handle · agent · project · when · meaning label · provenance marks.
fn head(h: &Hit) -> String {
    let meaning = if h.matched == "semantic" && h.score < 0.84 {
        "weak meaning match"
    } else if h.matched == "semantic" {
        "meaning match"
    } else {
        ""
    };
    let marks = [
        h.self_marker.then_some("~self"),
        h.meta_marker.then_some("~meta"),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join(" ");
    [
        surface::safe(&h.handle, false),
        surface::safe(&h.agent, false),
        surface::safe(&render::project(&h.project), false),
        surface::timestamp(h.ts),
        meaning.into(),
        marks,
    ]
    .into_iter()
    .filter(|field| !field.is_empty())
    .collect::<Vec<_>>()
    .join(" · ")
}
/// Takes each query's next hit in turn, skipping turns already taken and
/// families that query already supplied, until `target` hits. Also reports,
/// per query, whether selection asked for a hit past the end of its ranking.
fn select(
    buckets: &[Vec<Hit>],
    target: usize,
    per_session: bool,
) -> (Vec<(usize, Hit)>, Vec<bool>) {
    let mut selected = Vec::new();
    let mut seen = HashSet::new();
    let mut families = HashSet::new();
    let mut offsets = vec![0; buckets.len()];
    let mut exhausted = vec![false; buckets.len()];
    while selected.len() < target {
        let mut added = false;
        for (i, bucket) in buckets.iter().enumerate() {
            loop {
                let Some(h) = bucket.get(offsets[i]) else {
                    exhausted[i] = true;
                    break;
                };
                offsets[i] += 1;
                let family = if per_session {
                    h.session.clone()
                } else {
                    h.family.clone()
                };
                if seen.contains(&(h.session.clone(), h.turn))
                    || families.contains(&(i, family.clone()))
                {
                    continue;
                }
                seen.insert((h.session.clone(), h.turn));
                families.insert((i, family));
                selected.push((i, h.clone()));
                added = true;
                break;
            }
            if selected.len() == target {
                break;
            }
        }
        if !added {
            break;
        }
    }
    (selected, exhausted)
}
/// `select` over ranking prefixes. A prefix selects exactly what its full
/// ranking does unless selection ran past it; such a query's prefix, where
/// `deepen` holds its full search, is replaced by `full` until none runs past.
fn select_deepening<T>(
    buckets: &mut [Vec<Hit>],
    deepen: &mut [Option<T>],
    target: usize,
    per_session: bool,
    mut full: impl FnMut(T) -> Result<Vec<Hit>>,
) -> Result<Vec<(usize, Hit)>> {
    loop {
        let (selected, exhausted) = select(buckets, target, per_session);
        let mut deepened = false;
        for (i, ran_out) in exhausted.into_iter().enumerate() {
            if let Some(search) = deepen[i].take_if(|_| ran_out) {
                buckets[i] = full(search)?;
                deepened = true;
            }
        }
        if !deepened {
            return Ok(selected);
        }
    }
}
/// The head line cost 0.4.9's recall reserves per block.
const HEAD_COST: usize = 120;
/// Hit-turn bytes beyond which a block needs no more to be judged.
const JUDGEABLE_BLOCK: usize = 900;
/// Visible hit-turn bytes below which a block is a stub rather than evidence.
const JUDGEABLE_HIT: usize = 200;
/// What one `[+N chars - agrep around …]` marker costs.
const CAP_MARKER_RESERVE: usize = 120;
/// The largest level with the sum of `min(extra, level)` within `budget`.
fn water_level(extras: &[i64], budget: i64) -> i64 {
    let mut ordered = extras.to_vec();
    ordered.sort_unstable();
    let mut used = 0;
    for (index, extra) in ordered.iter().enumerate() {
        let share = (budget - used).div_euclid((ordered.len() - index) as i64);
        if *extra > share {
            return share.max(0);
        }
        used += extra;
    }
    budget
}
/// Bytes reserved for each hit's own turn before anything widens: every hit gets its
/// `needs` when the budget holds them all; otherwise the budget is water-filled above
/// each block's indivisible minimum (`heads` plus `lows`), and the lowest-ranked hits get
/// nothing until the kept ones are judgeable.
fn hit_floors(needs: &[usize], heads: &[usize], lows: &[usize], budget: usize) -> Vec<usize> {
    let mins: Vec<usize> = needs
        .iter()
        .zip(heads.iter().zip(lows))
        .map(|(need, (head, low))| (*need).min(head + low))
        .collect();
    if budget == 0 || needs.iter().sum::<usize>() <= budget {
        return needs.to_vec();
    }
    for keep in (1..=needs.len()).rev() {
        let base: usize = mins[..keep].iter().sum();
        if base > budget && keep > 1 {
            continue;
        }
        let extras: Vec<i64> = needs[..keep]
            .iter()
            .zip(&mins)
            .map(|(need, low)| (need - low) as i64)
            .collect();
        let level = water_level(&extras, budget.saturating_sub(base) as i64);
        let floors: Vec<usize> = needs[..keep]
            .iter()
            .zip(&mins)
            .map(|(need, low)| low + ((need - low) as i64).min(level) as usize)
            .collect();
        let judgeable = floors
            .iter()
            .zip(heads.iter().zip(needs))
            .all(|(floor, (head, need))| {
                *floor as i64 - *head as i64 - CAP_MARKER_RESERVE as i64
                    >= (JUDGEABLE_HIT as i64)
                        .min(*need as i64 - *head as i64 - CAP_MARKER_RESERVE as i64)
            });
        if judgeable || keep == 1 {
            let mut floors = floors;
            floors.resize(needs.len(), 0);
            return floors;
        }
    }
    vec![0; needs.len()]
}
/// Bytes the hit's own turn takes as rendered rows, at most one judgeable block.
fn hit_need(w: &Window) -> usize {
    page::hit_need(&page::turns(&w.rows), &BTreeSet::from([w.center]))
}
/// Coverage expansion: a hit session whose prose fits its share of the budget left after
/// every hit's own turn is shown whole; then turns elsewhere in the hit sessions holding
/// the most distinct query terms, newest first, get windows of one turn around them until
/// the page holds as many windows as the budget feeds (one per 2,500 bytes, at most 12).
fn expand(
    c: &rusqlite::Connection,
    mut pairs: Vec<(Hit, Window)>,
    queries: &[String],
    budget: usize,
    context: i64,
    before: Option<&(String, i64)>,
) -> Result<Vec<(Hit, Window)>> {
    let mut sessions: Vec<String> = Vec::new();
    for (_, w) in &pairs {
        if !sessions.contains(&w.session) {
            sessions.push(w.session.clone());
        }
    }
    let mut terms: Vec<String> = Vec::new();
    for term in evidence::content_terms(&queries.join(" ")) {
        let term = term.to_lowercase();
        if !terms.contains(&term) {
            terms.push(term);
        }
    }
    terms.truncate(32);
    let room = (budget / 2500).min(12);
    let scanned: &[String] = if room > pairs.len() { &terms } else { &[] };
    let mut stats = std::collections::HashMap::new();
    for session in &sessions {
        let boundary = before
            .filter(|(caller, _)| caller == session)
            .map(|(_, turn)| *turn);
        stats.insert(
            session.clone(),
            evidence::coverage(c, session, boundary, scanned)?,
        );
    }
    let needs: Vec<usize> = pairs.iter().map(|(_, w)| hit_need(w) + HEAD_COST).collect();
    let heads = vec![HEAD_COST; pairs.len()];
    let floors = hit_floors(&needs, &heads, &vec![0; pairs.len()], budget);
    let spare = budget.saturating_sub(floors.iter().sum()) / pairs.len();
    let mut widened = HashSet::new();
    let mut widen = Vec::new();
    for (i, (h, w)) in pairs.iter().enumerate() {
        let chars = stats[&w.session].chars as f64;
        if chars - floors[i] as f64 <= spare as f64 * 1.2 && widened.insert(w.session.clone()) {
            let radius = (w.center - w.first).max(w.last - w.center);
            widen.push((i, Request::new(&h.handle, None, Some(radius))));
        }
    }
    let requests: Vec<_> = widen.iter().map(|(_, r)| r.clone()).collect();
    for ((i, _), window) in widen.into_iter().zip(evidence::windows(c, &requests)?) {
        pairs[i].1 = window;
    }
    if pairs.len() >= room || terms.is_empty() {
        return Ok(pairs);
    }
    let mut covered: HashSet<(String, i64)> = pairs
        .iter()
        .flat_map(|(_, w)| w.rows.iter().map(|r| (r.session.clone(), r.turn)))
        .collect();
    let mut candidates: Vec<(u32, i64, &String, i64)> = sessions
        .iter()
        .flat_map(|s| {
            stats[s]
                .turns
                .iter()
                .map(move |(turn, ts, n)| (*n, *ts, s, *turn))
        })
        .collect();
    candidates.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then(b.1.cmp(&a.1))
            .then(a.2.cmp(b.2))
            .then(a.3.cmp(&b.3))
    });
    candidates.truncate((room * 32).max(256));
    let radius = context.min(1);
    let mut extra = Vec::new();
    for (n, ts, session, turn) in candidates {
        if pairs.len() + extra.len() >= room {
            break;
        }
        if covered.contains(&(session.clone(), turn)) {
            continue;
        }
        for nearby in turn - radius..=turn + radius {
            covered.insert((session.clone(), nearby));
        }
        extra.push((n, ts, session.clone(), turn));
    }
    let requests: Vec<_> = extra
        .iter()
        .map(|(_, _, session, turn)| Request::new(session, Some(*turn), Some(radius)))
        .collect();
    for ((n, ts, session, turn), w) in extra.into_iter().zip(evidence::windows(c, &requests)?) {
        let text = w
            .rows
            .iter()
            .find(|r| r.turn == w.center)
            .map_or("", |r| r.text.as_str());
        let hit = Hit {
            handle: handles::mint_full(&session, w.center, text),
            session,
            agent: w.agent.clone(),
            project: w.project.clone(),
            family: w.family.clone(),
            side: w.side,
            turn,
            ts,
            score: f64::from(n) / terms.len() as f64,
            matched: "expanded".into(),
            ..Hit::default()
        };
        pairs.push((hit, w));
    }
    Ok(pairs)
}
/// One chronological block per chat: every window of a session merges into the session's
/// first block, each row and event once, and each window's center becomes a hit turn.
/// `query_of` names the query each hit came from; expansion windows count as the first.
fn merge_chats(pairs: Vec<(Hit, Window)>, query_of: &HashMap<String, usize>) -> Vec<Block> {
    let mut blocks: Vec<Block> = Vec::new();
    for (hit, window) in pairs {
        let query = query_of.get(&hit.handle).copied().unwrap_or(0);
        let Some(b) = blocks
            .iter_mut()
            .find(|b| b.window.session == window.session)
        else {
            blocks.push(Block {
                hit_turns: BTreeSet::from([window.center]),
                hit,
                window,
                queries: vec![query],
            });
            continue;
        };
        b.hit_turns.insert(window.center);
        b.queries.push(query);
        for row in window.rows {
            if !b
                .window
                .rows
                .iter()
                .any(|r| r.turn == row.turn && r.who == row.who && r.text == row.text)
            {
                b.window.rows.push(row);
            }
        }
        b.window
            .rows
            .sort_by_key(|r| (r.turn, r.who == "agent", r.ts));
        let known: HashSet<String> = b.window.events.iter().map(|e| e.identity.clone()).collect();
        b.window.events.extend(
            window
                .events
                .into_iter()
                .filter(|e| !known.contains(&e.identity)),
        );
        b.window.events.sort_by_key(|e| (e.turn, e.ts));
    }
    blocks
}
pub(crate) fn run(a: Recall, pack: bool, no_auto: bool, color: &str) -> Result<i32> {
    if a.query
        .iter()
        .any(|s| s.starts_with('@') && s.contains(':') && handles::parse(s).is_err())
    {
        bail!("invalid result handle");
    }
    if a.semantic && !semantic::installed(&agrep_core::data::DataRoot::discover()?) {
        eprintln!("semantic model not installed: run agrep setup");
        return Ok(2);
    }
    if a.hits == Some(0) {
        bail!("--hits must be at least 1")
    }
    if a.budget > 0 && a.budget < 64 {
        bail!("a finite --budget must be at least 64 bytes")
    }
    if a.json && a.budget > 0 && a.budget < 2048 {
        bail!("a finite JSON --budget must be at least 2048 bytes")
    }
    if a.json && a.probe {
        bail!("--probe emits a pointer line and cannot be combined with --json")
    }
    let queries = if pack {
        a.query.clone()
    } else {
        vec![a.query.join(" ")]
    };
    if queries.iter().any(|q| q.trim().is_empty()) {
        bail!("a nonempty search pattern is required")
    }
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
    let (root, census) = {
        let _span = agrep_core::semantic_profile::span("freshness_prepare");
        ready(no_auto)?
    };
    let c = store::reader(&root)?;
    let mut semantic_coverage = None;
    let mut semantic_ran = false;
    let mut semantic_failed = false;
    let include_self = a.scope.include_self || (a.scope.chat.is_some() && !a.scope.no_self);
    let identity = Identity::detect();
    if !a.json && !a.scope.include_self && (a.scope.no_self || identity.agent_context) {
        identity.disclose(a.scope.no_self);
    }
    let caller = Caller::resolve(&c, identity)?;
    let mut filters = a.scope.filters()?;
    agrep_core::coverage::validate(&mut filters)?;
    let coverage = agrep_core::coverage::dimensions(&c, &filters)?;
    if let Some(chat) = filters
        .chat
        .as_ref()
        .filter(|_| !queries.iter().any(|q| handles::is_handle(q)))
    {
        let ids = handles::SessionIndex::load(&c)?.candidates(&c, chat)?;
        if ids.len() != 1 {
            bail!("chat is missing or ambiguous")
        }
        filters.chat = Some(ids[0].clone())
    }
    let direct_filters: Vec<_> = [
        ("--agent", filters.agent.is_some()),
        ("--project", filters.project.is_some()),
        ("--exclude-project", filters.exclude_project.is_some()),
        ("--model", filters.model.is_some()),
        ("--who", filters.who.is_some()),
        ("--no-who", filters.no_who.is_some()),
        ("--chat", filters.chat.is_some()),
        ("--since", filters.since.is_some()),
        ("--until", filters.until.is_some()),
    ]
    .into_iter()
    .filter(|(_, yes)| *yes)
    .map(|(name, _)| name)
    .collect();
    let override_filters =
        queries.iter().any(|q| handles::is_handle(q)) && !direct_filters.is_empty();
    if override_filters && !a.json {
        eprintln!(
            "direct handle overrides filters: {}",
            direct_filters.join(", ")
        );
    }
    let mut buckets = Vec::new();
    let mut known_sessions = HashSet::new();
    let mut totals_exact = true;
    let mut deepen = Vec::new();
    for query in &queries {
        if handles::is_handle(query) {
            let w = evidence::load(&c, &Request::new(query, None, Some(a.context as i64)))?;
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
                bail!("handle has no indexed row")
            };
            let mut handle = handles::SessionIndex::load(&c)?.mint(&w.session, w.center, &text);
            if let Some(e) = event {
                let (a, b) = claim.span.unwrap_or((0, e.text.chars().count()));
                handle += &format!("~{}:{a}-{b}", e.identity);
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
                handle,
                self_marker: caller.resolved && caller.session == w.session,
                ..Hit::default()
            };
            known_sessions.insert(w.session.clone());
            buckets.push(vec![h]);
            deepen.push(None);
            continue;
        }
        let options = Options {
            pattern: query.clone(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: target.saturating_mul(12).max(40),
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: !a.lexical,
            chats: false,
            tools: root.config()?.tools
                || filters
                    .who
                    .as_deref()
                    .is_some_and(|who| who.split(',').any(|w| w == "tool")),
            filters: filters.clone(),
        };
        let options = caller::options(&options, &caller, include_self, a.scope.no_self);
        let lane = if !semantic_failed
            && (a.semantic
                || (!a.lexical
                    && semantic::eligible(query)
                    && semantic::installed(&root)
                    && root.config()?.embeddings != "off"))
        {
            Some(semantic::begin(
                &root,
                &c,
                &options,
                !a.scope.all_side_chats,
            ))
        } else {
            None
        };
        // Selection reads only the head of each query's ranking, and a page as
        // deep as twice the target usually holds it at a fraction of the cost.
        // Merging with meaning results and the probe's session count need the
        // full page.
        let depth = if lane.is_none() && !a.probe {
            options.limit.min(target.saturating_mul(2))
        } else {
            options.limit
        };
        let keyword = if a.semantic && !semantic_failed {
            Default::default()
        } else {
            let _span = agrep_core::semantic_profile::span("keyword");
            let page = Options {
                limit: depth,
                ..options.clone()
            };
            let keyword = caller::search(&c, &page, &caller, include_self, a.scope.no_self)?;
            agrep_core::semantic_profile::event(serde_json::json!({
                "retrieval":keyword.timings,"examined":keyword.examined,"blocks":keyword.blocks
            }));
            keyword
        };
        let r = if let Some(lane) = lane {
            match lane.and_then(|work| semantic::finish(&c, work)) {
                Ok(mut found) => {
                    semantic_ran = true;
                    semantic_coverage = Some(found.coverage);
                    caller::decorate_semantic(&c, &mut found.result, &caller)?;
                    if a.semantic {
                        found.result
                    } else {
                        hybrid::merge(
                            keyword,
                            Some(found.result),
                            options.limit,
                            !a.scope.all_side_chats,
                            if pack {
                                hybrid::Lane::Pack
                            } else {
                                hybrid::Lane::Recall
                            },
                        )
                    }
                }
                Err(error) => {
                    agrep_core::logging::semantic_failure(&root, &error);
                    semantic_failed = true;
                    if a.semantic {
                        caller::search(&c, &options, &caller, include_self, a.scope.no_self)?
                    } else {
                        keyword
                    }
                }
            }
        } else {
            keyword
        };
        totals_exact &= r.total_basis == "exact";
        known_sessions.extend(r.hits.iter().map(|h| h.session.clone()));
        deepen.push((depth < options.limit && r.hits.len() == depth).then_some(options));
        buckets.push(r.hits);
    }
    let selected = select_deepening(
        &mut buckets,
        &mut deepen,
        target,
        a.scope.all_side_chats || filters.chat.is_some(),
        |options| {
            let _span = agrep_core::semantic_profile::span("keyword");
            Ok(caller::search(&c, &options, &caller, include_self, a.scope.no_self)?.hits)
        },
    )?;
    let requests: Vec<_> = selected
        .iter()
        .map(|(_, h)| Request::new(&h.handle, None, Some(a.context as i64)))
        .collect();
    let windows_span = agrep_core::semantic_profile::span("windows");
    let windows = evidence::windows(&c, &requests)?;
    // The caller's current window stays excluded after expansion reads neighboring turns.
    let before = (caller.resolved && !include_self && !handles::is_handle(&queries[0]))
        .then(|| (caller.session.clone(), caller.boundary.unwrap_or(0)));
    let query_of: HashMap<String, usize> = selected
        .iter()
        .map(|(query, h)| (h.handle.clone(), *query))
        .collect();
    let mut pairs: Vec<(Hit, Window)> = selected
        .into_iter()
        .map(|(_, hit)| hit)
        .zip(windows)
        .collect();
    if a.budget > 0 && !pairs.is_empty() {
        pairs = expand(
            &c,
            pairs,
            &queries,
            a.budget,
            a.context as i64,
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
    if !a.json {
        agrep_core::coverage::disclose(&coverage);
    }
    let pending_sources = {
        let _span = agrep_core::semantic_profile::span("freshness_join");
        crate::finish_census(&root, census)?
    };
    let _render_span = agrep_core::semantic_profile::span("render");
    let checked = agrep_core::render::checked(no_auto);
    let exit = if blocks.is_empty() {
        if checked {
            1
        } else {
            2
        }
    } else {
        0
    };
    if a.probe {
        let confident = blocks
            .iter()
            .find(|b| b.hit.matched == "phrase" || b.hit.matched == "semantic");
        let line = if let Some(b) = confident {
            let n = known_sessions.len().max(1);
            let lead = if totals_exact {
                format!(
                    "{n} past session{} match{}",
                    if n == 1 { "" } else { "s" },
                    if n == 1 { "es" } else { "" }
                )
            } else {
                format!(
                    "top results span {n} past session{}",
                    if n == 1 { "" } else { "s" }
                )
            };
            format!(
                "recall: {lead} · {} · pull: {}",
                head(&b.hit),
                surface::shell(
                    &std::iter::once("agrep".to_owned())
                        .chain(std::iter::once(if pack { "pack" } else { "recall" }.into()))
                        .chain(queries.clone())
                        .collect::<Vec<_>>()
                )
            )
        } else {
            let count: i64 = c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
            format!(
                "recall: no confident match in {count} past conversations{}",
                if checked {
                    ""
                } else {
                    "; freshness unchecked (--no-auto)"
                }
            )
        };
        if a.budget > 0 && line.len() + 1 > a.budget {
            if let Some(b) = confident {
                let pointer = format!("recall: {}\n", b.hit.handle);
                if pointer.len() > a.budget {
                    bail!(
                        "--budget needs at least {} bytes for the first reference",
                        pointer.len()
                    )
                }
                surface::write(&pointer)?
            } else {
                surface::line(surface::prefix(&line, a.budget - 1))?
            }
        } else {
            surface::line(&line)?
        }
        return Ok(if confident.is_some() {
            0
        } else if checked {
            1
        } else {
            2
        });
    }
    let color = color == "always" || (color == "auto" && std::io::stdout().is_terminal());
    let page = page::Page::new(&blocks, &queries, a.budget);
    if a.json {
        let mut value = json!({"query":if pack{json!(queries)}else{json!(queries[0])},"engine":"sqlite","hits":[],"semantic_status":{"state":if semantic_failed{"failed"}else if semantic_ran{"complete"}else{"not-run"},"reason":if semantic_failed{Some("retrieval-failed")}else if semantic_ran{None}else{Some(if a.lexical{"--lexical"}else if !semantic::installed(&root){"model-not-installed"}else{"query-ineligible"})}},"self_exclusion":caller.metadata(include_self,a.scope.no_self),"filter_coverage":coverage,"freshness":render::freshness(checked,pending_sources),"semantic_coverage":semantic_coverage});
        if override_filters {
            value["filter_override"] = json!({"supplied":direct_filters,"reason":"direct-handle"});
        }
        surface::write(&page::json(&page, &mut value)?)?;
    } else if blocks.is_empty() {
        let n: i64 = c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
        eprintln!("no hits across {n} indexed sessions.");
    } else {
        surface::write(&page::text(&page, color)?)?;
    }
    Ok(exit)
}

#[cfg(test)]
mod tests {
    use super::*;
    use agrep_core::evidence::Row;

    fn row(session: &str, turn: i64, who: &str, text: &str) -> Row {
        Row {
            session: session.into(),
            turn,
            who: who.into(),
            ts: 1_700_000_000_000 + turn,
            text: text.into(),
            model: String::new(),
            model_source: String::new(),
            source: 1,
            source_chars: text.chars().count(),
        }
    }
    fn window(session: &str, center: i64, turns: std::ops::RangeInclusive<i64>) -> Window {
        Window {
            target: session.into(),
            session: session.into(),
            agent: "pi".into(),
            project: "/work/agrep".into(),
            title: String::new(),
            family: session.into(),
            side: false,
            center,
            radius: Some(1),
            latest: false,
            first: 0,
            last: 20,
            rows: turns
                .map(|turn| row(session, turn, "user", &format!("turn {turn}")))
                .collect(),
            events: vec![],
            notes: vec![],
            claim: None,
        }
    }

    // Expected values below were computed by agrep 0.4.9's own `_content_terms`,
    // `_water_level` and `_hit_floors` (py/search.py, py/recall.py).
    #[test]
    fn content_terms_water_levels_and_floors_match_0_4_9() {
        for (query, terms) in [
            (
                "how did the drafting go last run?",
                vec!["drafting", "go", "last", "run"],
            ),
            (
                "release-workflow_cache",
                vec!["release", "workflow", "cache"],
            ),
            ("a I it's", vec!["it's"]),
            ("Flaky (windows) cache!", vec!["Flaky", "windows", "cache"]),
            ("x ab", vec!["ab"]),
        ] {
            assert_eq!(evidence::content_terms(query), terms, "{query}");
        }
        for (extras, budget, level) in [
            (vec![5, 1, 9], 12, 6),
            (vec![100, 200, 300], 1000, 1000),
            (vec![50, 50], 30, 15),
            (vec![7], 3, 3),
            (vec![], 5, 5),
        ] {
            assert_eq!(water_level(&extras, budget), level, "{extras:?} {budget}");
        }
        for (texts, budget, floors) in [
            (vec![100, 200], 5000, vec![234, 334]),
            (vec![1500, 1500, 1500], 1000, vec![500, 500, 0]),
            (vec![800, 50, 3000], 900, vec![716, 184, 0]),
            (vec![300, 300, 300, 300], 600, vec![434, 0, 0, 0]),
            (vec![2000], 150, vec![150]),
        ] {
            let needs: Vec<usize> = texts
                .iter()
                .map(|n| (n + 10 + "user".len()).min(JUDGEABLE_BLOCK) + HEAD_COST)
                .collect();
            let heads = vec![HEAD_COST; needs.len()];
            let lows = vec![0; needs.len()];
            assert_eq!(
                hit_floors(&needs, &heads, &lows, budget),
                floors,
                "{texts:?} {budget}"
            );
        }
    }

    #[test]
    fn windows_of_one_chat_merge_into_one_block_with_gap_lines() {
        let hit = |session: &str, turn: i64| Hit {
            session: session.into(),
            turn,
            handle: format!("@{session}:{turn}.abcd"),
            agent: "pi".into(),
            project: "/work/agrep".into(),
            matched: "phrase".into(),
            ..Default::default()
        };
        let query_of = HashMap::from([
            ("@chat-a:10.abcd".to_owned(), 1),
            ("@chat-b:4.abcd".to_owned(), 0),
            ("@chat-a:2.abcd".to_owned(), 0),
        ]);
        let merged = merge_chats(
            vec![
                (hit("chat-a", 10), window("chat-a", 10, 8..=12)),
                (hit("chat-b", 4), window("chat-b", 4, 3..=5)),
                (hit("chat-a", 2), window("chat-a", 2, 1..=3)),
                (hit("chat-a", 12), window("chat-a", 12, 11..=13)),
            ],
            &query_of,
        );
        assert_eq!(merged.len(), 2);
        let b = &merged[0];
        assert_eq!(b.hit_turns, BTreeSet::from([2, 10, 12]));
        assert_eq!(b.queries, [1, 0, 0]);
        assert_eq!(
            b.window.rows.iter().map(|r| r.turn).collect::<Vec<_>>(),
            [1, 2, 3, 8, 9, 10, 11, 12, 13]
        );
        let queries = ["turn".to_owned(), "turn".to_owned()];
        let text = page::text(&page::Page::new(&merged[..1], &queries, 0), false).unwrap();
        assert!(
            text.contains(
                "     3 user: turn 3\n       [turns 4-7 not shown]\n     8 user: turn 8\n"
            ),
            "{text}"
        );
    }

    #[test]
    fn deepening_exhausted_prefixes_selects_like_full_rankings() {
        let mut state = 7u64;
        let mut next = move |n: u64| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) % n
        };
        let key = |selected: &[(usize, Hit)]| {
            selected
                .iter()
                .map(|(i, h)| (*i, h.session.clone(), h.turn))
                .collect::<Vec<_>>()
        };
        for _ in 0..400 {
            let families = 1 + next(6);
            let full: Vec<Vec<Hit>> = (0..1 + next(3))
                .map(|_| {
                    (0..next(40))
                        .map(|_| {
                            let session = next(10);
                            Hit {
                                session: format!("s{session}"),
                                family: format!("f{}", session % families),
                                turn: next(4) as i64,
                                ..Default::default()
                            }
                        })
                        .collect()
                })
                .collect();
            let target = 1 + next(9) as usize;
            for per_session in [false, true] {
                let expected = key(&select(&full, target, per_session).0);
                for depth in 1..=24 {
                    let mut buckets: Vec<Vec<Hit>> = full
                        .iter()
                        .map(|b| b[..depth.min(b.len())].to_vec())
                        .collect();
                    let mut deepen: Vec<Option<usize>> = (0..full.len())
                        .map(|i| (full[i].len() > depth).then_some(i))
                        .collect();
                    let selected =
                        select_deepening(&mut buckets, &mut deepen, target, per_session, |i| {
                            Ok(full[i].clone())
                        })
                        .unwrap();
                    assert_eq!(key(&selected), expected, "depth {depth} target {target}");
                }
            }
        }
    }
}
