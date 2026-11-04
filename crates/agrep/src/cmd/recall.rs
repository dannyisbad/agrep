mod page;
use crate::{ready, Recall};
use agrep_core::{
    caller::{self, Caller, Identity},
    evidence::{self, Request, Window},
    handles, hybrid,
    query::{Hit, Options, Results},
    render, semantic, store, surface,
};
use anyhow::{bail, Result};
use serde_json::{json, Value};
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
    scatter(h) || (h.matched == "semantic" && h.sem_weak)
}
/// Whether 0.4.9's recall read tool rows for a query: its prose lane ran first and fell
/// back to tools when fewer than `target` chats (families, unless `by_session`) held a
/// prose match or none of those matches was a phrase. `rows` is the head, `depth` rows
/// deep, of the same tier-first ranking with tools included; `deeper` reads that many
/// prose rows when the head cannot decide.
fn tool_lane(
    rows: &[Hit],
    depth: usize,
    target: usize,
    by_session: bool,
    deeper: impl FnOnce(usize) -> Result<Vec<Hit>>,
) -> Result<bool> {
    let decide = |rows: &[Hit], complete: bool| {
        let prose: Vec<_> = rows.iter().filter(|h| h.who != "tool").collect();
        let chats = prose
            .iter()
            .map(|h| (&h.agent, if by_session { &h.session } else { &h.family }))
            .collect::<HashSet<_>>()
            .len();
        let strong = prose.first().is_some_and(|h| h.matched == "phrase");
        // Phrase rows rank first, so once a weaker row shows none is left unread.
        let settled = complete || rows.last().is_some_and(|h| h.matched != "phrase");
        if chats >= target && strong {
            Some(false)
        } else if complete || (settled && !strong) {
            Some(true)
        } else {
            None
        }
    };
    if let Some(decided) = decide(rows, rows.len() < depth) {
        return Ok(decided);
    }
    let limit = depth.max(target).saturating_mul(8);
    let rows = deeper(limit)?;
    Ok(decide(&rows, rows.len() < limit).unwrap_or(true))
}
/// A block's head: handle · agent · project · when · meaning label · provenance marks.
fn head(h: &Hit) -> String {
    let meaning = if h.matched == "semantic" && h.sem_weak {
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
fn family_key(h: &Hit, per_session: bool) -> &str {
    if per_session {
        &h.session
    } else {
        &h.family
    }
}
/// 0.4.9's `_weak_scatter`: a bag-of-words row, never strong evidence.
fn scatter(h: &Hit) -> bool {
    h.matched == "all-terms" || h.matched == "content-terms"
}
/// 0.4.9's `_meta_row` from stored provenance: machinery roles and spawned task prompts.
fn meta_row(h: &Hit) -> bool {
    h.meta_marker
        || matches!(h.who.as_str(), "harness" | "synthetic")
        || (h.who == "subagent" && h.turn == 0)
}
/// 0.4.9's recall under `--no-meta` selected from heads, not rows: the best row of each
/// of up to `max(target, 40)` families (chats under `per_session`) from the prose lane,
/// plus the tool lane's when prose held fewer than `target` or only bag-of-words
/// heads. Proven ~meta heads were dropped, the best one kept when every head was one,
/// and the rest ordered lived first, then by strength, lane and score.
struct MetaHeads {
    heads: Vec<Hit>,
    dropped: usize,
    retained: bool,
}
impl MetaHeads {
    /// `chats(limit, lane)` ranks chat heads: `Some(false)` prose, `Some(true)` tool rows,
    /// `None` the scope's own `--who` lane.
    fn rank(
        c: &rusqlite::Connection,
        query: &str,
        target: usize,
        per_session: bool,
        who: bool,
        chats: impl Fn(usize, Option<bool>) -> Result<Results>,
    ) -> Result<(Results, Self)> {
        let want = target.max(40);
        let lane = |tools: Option<bool>| -> Result<(Results, Vec<Hit>)> {
            let mut limit = want;
            loop {
                let mut ranked = chats(limit, tools)?;
                let all = ranked.hits.len() < limit;
                let mut seen = HashSet::new();
                let heads: Vec<Hit> = std::mem::take(&mut ranked.hits)
                    .into_iter()
                    .filter(|h| seen.insert(family_key(h, per_session).to_owned()))
                    .take(want)
                    .collect();
                if all || heads.len() == want {
                    return Ok((ranked, heads));
                }
                limit *= 2;
            }
        };
        let (base, mut heads) = lane((!who).then_some(false))?;
        if !who && (heads.len() < target || heads.iter().all(scatter)) {
            heads.extend(lane(Some(true))?.1);
        }
        let meta = agrep_core::query::meta_rows(c, &heads, query)?;
        let mut ranked: Vec<(Hit, bool)> = heads.into_iter().zip(meta).collect();
        ranked.sort_by(|(a, a_meta), (b, b_meta)| {
            a_meta
                .cmp(b_meta)
                .then(scatter(a).cmp(&scatter(b)))
                .then((!who && a.who == "tool").cmp(&(!who && b.who == "tool")))
                .then((a.matched == "all-terms").cmp(&(b.matched == "all-terms")))
                .then(b.score.total_cmp(&a.score))
        });
        let lived = ranked.iter().any(|(_, meta)| !meta);
        let mut out = MetaHeads {
            heads: Vec::new(),
            dropped: 0,
            retained: false,
        };
        for (mut h, meta) in ranked {
            if !meta {
                out.heads.push(h);
            } else if !lived && !out.retained {
                h.meta_marker = true;
                out.heads.push(h);
                out.retained = true;
            } else {
                out.dropped += 1;
            }
        }
        Ok((base, out))
    }
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
/// 0.4.9's `_reserve_semantic_hits` for a pack (py/recall.py:891-932): a query whose
/// selection holds no meaning row gets its best remaining one over its own family's row,
/// in a free slot, or over its weakest row when it holds two.
fn reserve(
    mut out: Vec<(usize, Hit)>,
    meanings: &[Vec<Hit>],
    target: usize,
    per_session: bool,
) -> Vec<(usize, Hit)> {
    if target <= meanings.len() {
        return out;
    }
    for (query, ranked) in meanings.iter().enumerate() {
        let positions: Vec<usize> = (0..out.len()).filter(|&i| out[i].0 == query).collect();
        if positions.iter().any(|&i| out[i].1.matched == "semantic") {
            continue;
        }
        let seen: HashSet<(&str, i64)> = out
            .iter()
            .map(|(_, h)| (h.session.as_str(), h.turn))
            .collect();
        let seen_text: HashSet<String> = out.iter().map(|(_, h)| hybrid::text_key(h)).collect();
        let others: HashSet<&str> = (0..out.len())
            .filter(|i| !positions.contains(i))
            .map(|i| out[i].1.session.as_str())
            .collect();
        let Some(meaning) = ranked
            .iter()
            .find(|h| {
                !others.contains(h.session.as_str())
                    && !seen.contains(&(h.session.as_str(), h.turn))
                    && {
                        let key = hybrid::text_key(h);
                        key.is_empty() || !seen_text.contains(&key)
                    }
            })
            .cloned()
        else {
            continue;
        };
        let family = hybrid::family(&meaning, !per_session).to_owned();
        if let Some(&i) = positions
            .iter()
            .find(|&&i| hybrid::family(&out[i].1, !per_session) == family)
        {
            out[i] = (query, meaning);
        } else if out.len() < target {
            out.push((query, meaning));
        } else if positions.len() >= 2 {
            let replace = positions
                .iter()
                .copied()
                .max_by_key(|&i| (out[i].1.who == "tool", scatter(&out[i].1), i))
                .unwrap_or_default();
            out[replace] = (query, meaning);
        }
    }
    out
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
/// 0.4.9's disclosure that an explicit handle bypassed the supplied filters, naming those
/// whose own predicates would have excluded it (py/recall.py:1540-1574,
/// py/surface_policy.py:2424-2432).
fn handle_filter_note(
    flags: &[&str],
    f: &agrep_core::query::Filters,
    w: &Window,
    ts: i64,
) -> String {
    use agrep_core::history::project_matches;
    let mut mismatched = Vec::new();
    if f.chat.as_deref().is_some_and(|chat| chat != w.session) {
        mismatched.push("--chat");
    }
    if f.agent
        .as_deref()
        .is_some_and(|agent| !w.agent.to_lowercase().contains(&agent.to_lowercase()))
    {
        mismatched.push("--agent");
    }
    if f.project
        .as_deref()
        .is_some_and(|p| !project_matches(&w.project, p))
    {
        mismatched.push("--project");
    }
    if f.exclude_project
        .as_deref()
        .is_some_and(|p| project_matches(&w.project, p))
    {
        mismatched.push("--exclude-project");
    }
    if f.since.is_some_and(|since| ts < since) {
        mismatched.push("--since");
    }
    if f.until.is_some_and(|until| ts >= until) {
        mismatched.push("--until");
    }
    let mut line = format!(
        "explicit result handle wins: {} not applied",
        flags.join(", ")
    );
    if !mismatched.is_empty() {
        line += &format!(" ({} would have excluded it)", mismatched.join(", "));
    }
    line
}
/// 0.4.9's envelope fields for the queries whose meaning lane ran (py/recall.py:2169-2238,
/// 2572-2582): one query's status or the aggregate with the per-query list, and `hybrid`
/// when a meaning row made the page; `reduce` keeps only the status of a keyword page whose
/// meaning rows all lost the merge.
fn semantic_meta(
    runs: &[(String, Value, Option<semantic::Coverage>)],
    multi: bool,
    visible: bool,
    reduce: bool,
) -> serde_json::Map<String, Value> {
    let mut meta = serde_json::Map::new();
    let Some((_, first, _)) = runs.first() else {
        return meta;
    };
    let all = |key: &str| runs.iter().all(|(_, s, _)| s[key] == true);
    let kinds: BTreeSet<_> = runs
        .iter()
        .map(|(_, s, _)| s["score_kind"].as_str())
        .collect();
    let coverage = runs.iter().rev().find_map(|(_, _, c)| c.as_ref());
    meta.insert(
        "score_kind".into(),
        if kinds.len() == 1 {
            first["score_kind"].clone()
        } else {
            json!("mixed")
        },
    );
    meta.insert("semantic_coverage".into(), json!(coverage));
    meta.insert(
        "partial".into(),
        json!(coverage.is_none_or(|c| c.partial())),
    );
    let status = if runs.len() == 1 {
        first.clone()
    } else {
        let states: BTreeSet<_> = runs.iter().map(|(_, s, _)| s["state"].as_str()).collect();
        let mut status = json!({"state":if states.len() == 1 {first["state"].clone()} else {json!("mixed")},"complete":all("complete"),"fallback_recommended":all("fallback_recommended")});
        let reasons: BTreeSet<_> = runs
            .iter()
            .filter(|(_, s, _)| s["fallback_recommended"] == true || s["state"] == "unavailable")
            .filter_map(|(_, s, _)| s["reason"].as_str().filter(|r| !r.is_empty()))
            .collect();
        if reasons.len() == 1 {
            status["reason"] = json!(reasons.first());
        }
        status
    };
    meta.insert("semantic_status".into(), status);
    if multi {
        meta.insert(
            "semantic_queries".into(),
            runs.iter()
                .map(|(query, status, coverage)| json!({"query":query,"status":status,"coverage":coverage}))
                .collect(),
        );
    }
    if visible {
        meta.insert("hybrid".into(), json!(true));
    } else if reduce {
        meta.retain(|key, _| key == "semantic_status");
    }
    meta
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
    // 0.4.9 disclosed raising an explicit --hits to one slot per query (py/recall.py:1988-1997).
    let hits_floored = a
        .hits
        .filter(|&n| !a.probe && queries.len() > n)
        .map(|n| json!({"requested":n,"raised_to":queries.len()}));
    if let Some(floored) = hits_floored.as_ref().filter(|_| !a.json) {
        eprintln!(
            "--hits {} raised to {}: pack reserves one slot per query",
            floored["requested"], floored["raised_to"]
        );
    }
    // 0.4.9 kept a lone query's meaning lane off a page too small to hold it beside its
    // keyword lead, and never searched meaning for tool rows (py/recall.py:1999, 2331-2336).
    let meaning_room = target >= queries.len().max(2);
    // How deep 0.4.9 read each query's lanes (py/recall.py:2000-2001, 2255-2259).
    let query_target = target.max(if a.scope.no_meta { 40 } else { 8 });
    let mut meanings: Vec<Vec<Hit>> = Vec::new();
    let (root, census) = {
        let _span = agrep_core::semantic_profile::span("freshness_prepare");
        ready(no_auto)?
    };
    let c = store::reader(&root)?;
    let mut semantic_ran = false;
    let mut semantic_failed = false;
    let mut hybrid_used = false;
    let mut lane_runs: Vec<(String, Value, Option<semantic::Coverage>)> = Vec::new();
    let include_self = a.scope.include_self || (a.scope.chat.is_some() && !a.scope.no_self);
    let identity = Identity::detect();
    if !a.json && !a.scope.include_self && (a.scope.no_self || identity.agent_context) {
        identity.disclose(a.scope.no_self, true);
    }
    let caller = Caller::resolve(&c, identity)?;
    let request = caller::Request {
        include_self: a.scope.include_self,
        no_self: a.scope.no_self,
        waived: a.scope.chat.is_some().then_some("chat-filter"),
    };
    let policy = caller.policy(request);
    let mut excluded = None;
    let mut filters = a.scope.filters()?;
    agrep_core::coverage::validate(&mut filters)?;
    let tools_only = filters.who.as_deref() == Some("tool");
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
    let mut filter_note: Option<String> = None;
    let mut handle_notes: Vec<Value> = Vec::new();
    let handle_only = queries.iter().all(|q| handles::is_handle(q));
    let mut buckets = Vec::new();
    let mut known_sessions = HashSet::new();
    let mut totals_exact = true;
    let (mut meta_dropped, mut meta_retained) = (0, 0);
    let mut deepen = Vec::new();
    for query in &queries {
        if handles::is_handle(query) {
            let w = evidence::load(&c, &Request::new(query, None, Some(a.context as i64)))?;
            // 0.4.9 printed each way the served turn differs from the cited one, JSON or not,
            // and carried them as `served` (py/recall.py:1941-1946, 2898-2902).
            for note in &w.notes {
                eprintln!("{}", note["note"].as_str().unwrap_or_default());
            }
            handle_notes.extend(w.notes.iter().cloned());
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
            if override_filters && filter_note.is_none() {
                filter_note = Some(handle_filter_note(&direct_filters, &filters, &w, ts));
                if !a.json {
                    eprintln!("{}", filter_note.as_deref().unwrap_or_default());
                }
            }
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
                score: 1.0,
                handle,
                self_marker: caller.resolved && caller.session == w.session,
                ..Hit::default()
            };
            known_sessions.insert(w.session.clone());
            // 0.4.9 hides a handle into the caller's own window and counts it withheld
            // (py/recall.py:2452-2471).
            let hidden = policy.is_some()
                && caller::excludes(
                    &caller,
                    &h.agent,
                    &h.session,
                    h.turn,
                    include_self,
                    a.scope.no_self,
                );
            if hidden {
                excluded = Some(excluded.unwrap_or(0) + 1);
            }
            buckets.push(if hidden { vec![] } else { vec![h] });
            deepen.push(None);
            meanings.push(Vec::new());
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
        // The withheld-row probes drop --no-meta's stored tooling roles themselves; the
        // searches leave ~meta rows to the family heads below.
        let probes = options.clone();
        let mut options = options;
        options.filters.no_meta = false;
        let per_session = a.scope.all_side_chats || filters.chat.is_some();
        let mut meta = if a.scope.no_meta && !a.semantic {
            let who = options.filters.who.is_some();
            Some(MetaHeads::rank(
                &c,
                query,
                target,
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
                    caller::search(&c, &heads, &caller, include_self, a.scope.no_self)
                },
            )?)
        } else {
            None
        };
        let lane = if !semantic_failed
            && (a.semantic
                || (!a.lexical
                    && !tools_only
                    && (a.probe || meaning_room)
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
        } else if let Some((base, heads)) = meta.as_mut() {
            meta_dropped += heads.dropped;
            meta_retained += usize::from(heads.retained);
            let mut keyword = std::mem::take(base);
            keyword.hits = std::mem::take(&mut heads.heads);
            keyword
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
                    hybrid_used |= !a.semantic && !found.result.hits.is_empty();
                    lane_runs.push((
                        query.clone(),
                        semantic::status(Some(&found.coverage), !found.result.hits.is_empty()),
                        Some(found.coverage),
                    ));
                    caller::decorate_semantic(&c, &mut found.result, &caller)?;
                    semantic::mark_weak(
                        &c,
                        &options,
                        &mut found.result.hits,
                        !keyword.hits.is_empty(),
                    )?;
                    if a.semantic {
                        found.result
                    } else {
                        // 0.4.9 read `query_target` meaning rows and ranked them lived
                        // first, then strong first, each band by cosine
                        // (py/recall.py:983-994).
                        let mut rows = std::mem::take(&mut found.result.hits);
                        rows.truncate(query_target);
                        rows.sort_by_key(|h| (meta_row(h), h.sem_weak));
                        meanings.push(rows);
                        keyword
                    }
                }
                Err(error) => {
                    agrep_core::logging::semantic_failure(&root, &error);
                    semantic_failed = true;
                    lane_runs.push((query.clone(), semantic::status(None, false), None));
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
        // 0.4.9 counted withheld rows only for a single keyword query; packs and lanes
        // fused with meaning results have no exact count.
        if let Some(policy) = policy
            .as_ref()
            .filter(|_| queries.len() == 1 && !a.semantic && !semantic_ran)
        {
            if let Some(withheld) = policy.withheld(&c, &probes, filters.chat.as_deref())? {
                let tools = filters.who.is_some()
                    || filters.no_who.is_some()
                    || tool_lane(&r.hits, depth, target, a.scope.all_side_chats, |limit| {
                        let prose = Options {
                            tools: false,
                            limit,
                            ..options.clone()
                        };
                        Ok(
                            caller::search(&c, &prose, &caller, include_self, a.scope.no_self)?
                                .hits,
                        )
                    })?;
                excluded = Some(withheld.prose + if tools { withheld.tools } else { 0 });
            }
        }
        if meanings.len() < buckets.len() + 1 {
            meanings.push(Vec::new());
        }
        let meaning = meanings.last().map(Vec::as_slice).unwrap_or_default();
        totals_exact &= r.total_basis == "exact" && meaning.is_empty();
        known_sessions.extend(r.hits.iter().chain(meaning).map(|h| h.session.clone()));
        // A --no-meta bucket already holds every head 0.4.9 selected from.
        deepen.push(
            (meta.is_none() && depth < options.limit && r.hits.len() == depth).then_some(options),
        );
        buckets.push(r.hits);
    }
    // 0.4.9's JSON note for a keyword lane that fell back to its any-order tiers
    // (py/recall.py:2379-2382, 2916-2917).
    let fell_back = buckets
        .iter()
        .flatten()
        .any(|h| h.matched == "all-terms" || h.matched == "content-terms")
        && !buckets.iter().flatten().any(|h| h.matched == "phrase");
    let per_session = a.scope.all_side_chats || filters.chat.is_some();
    let lone_hybrid = queries.len() == 1 && meanings.iter().any(|m| !m.is_empty());
    let selected = if lone_hybrid && a.probe {
        // 0.4.9's probe judged the keyword lane's pick and the meaning lane's
        // (py/recall.py:2552-2561).
        let mut picked = select(&buckets, target, per_session).0;
        picked.extend(select(&meanings, target, per_session).0);
        picked
    } else if lone_hybrid {
        // A lone query's page is its keyword chats merged with its meaning rows
        // (py/recall.py:883-890).
        let mut lexical = Results::default();
        lexical.hits = select(&buckets, query_target, per_session)
            .0
            .into_iter()
            .map(|(_, h)| h)
            .collect();
        let mut meaning = Results::default();
        meaning.hits = std::mem::take(&mut meanings[0]);
        hybrid::merge(lexical, Some(meaning), target, !per_session)
            .hits
            .into_iter()
            .map(|h| (0, h))
            .collect()
    } else {
        // A pack ranks each query's meaning rows after its lived prose phrases, ahead of
        // tool rows and bag-of-words scatter, then reserves a meaning slot per query
        // (py/recall.py:983-994, 2566-2571).
        for (bucket, meaning) in buckets.iter_mut().zip(&meanings) {
            if !meaning.is_empty() {
                bucket.extend(meaning.iter().cloned());
                bucket.sort_by_key(|h| {
                    (
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
        let selected =
            select_deepening(&mut buckets, &mut deepen, target, per_session, |options| {
                let _span = agrep_core::semantic_profile::span("keyword");
                Ok(caller::search(&c, &options, &caller, include_self, a.scope.no_self)?.hits)
            })?;
        reserve(selected, &meanings, target, per_session)
    };
    let hybrid_visible = selected.iter().any(|(_, h)| h.matched == "semantic");
    let requests: Vec<_> = selected
        .iter()
        .map(|(_, h)| Request::new(&h.handle, None, Some(a.context as i64)))
        .collect();
    let windows_span = agrep_core::semantic_profile::span("windows");
    let windows = evidence::windows(&c, &requests)?;
    // The caller's current window stays excluded after expansion reads neighboring turns.
    let before = (caller.resolved && !include_self)
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
    if let Some(line) = agrep_core::query::meta_notice(meta_dropped, meta_retained) {
        eprintln!("{line}");
    }
    // 0.4.9 named an empty filter dimension after the page or its zero line.
    let disclose = || {
        if !a.json {
            agrep_core::coverage::disclose(&coverage);
        }
    };
    if a.probe {
        disclose();
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
    let notice = policy
        .as_ref()
        .zip(excluded)
        .and_then(|(policy, n)| policy.notice(n, "hit"))
        .filter(|_| !a.json);
    if a.probe {
        if let Some(notice) = &notice {
            eprintln!("{notice}");
        }
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
            format!("recall: no confident match in {count} past conversations")
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
        render::note_no_auto(no_auto);
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
        let mut value = json!({"query":if pack{json!(queries)}else{json!(queries[0])},"engine":if handle_only{"handle"}else{"sqlite"},"hits":[]});
        if !handle_notes.is_empty() {
            value["served"] = json!({"served_as_requested":false,"divergences":handle_notes});
        }
        if let Some(floored) = hits_floored {
            value["hits_floored"] = floored;
        }
        let meta = semantic_meta(&lane_runs, queries.len() > 1, hybrid_visible, hybrid_used);
        let lane_coverage = meta.get("semantic_coverage").cloned().unwrap_or_default();
        for (key, field) in meta {
            value[key] = field;
        }
        if lane_runs.is_empty() {
            // 0.4.9 separated a lane that never ran from one that searched (py/recall.py:2662-2676).
            let reason = if handle_only {
                "handle-lookup"
            } else if a.lexical {
                "--lexical"
            } else if tools_only {
                "tool-lane"
            } else if root.config()?.embeddings == "off" {
                "embeddings-off"
            } else if !semantic::installed(&root) {
                "model-not-installed"
            } else if !meaning_room {
                "no-meaning-room"
            } else {
                "query-not-language-shaped"
            };
            value["semantic_status"] = json!({"state":"not-run","reason":reason});
        }
        value["self_exclusion"] = json!(caller.metadata(request, excluded));
        value["filter_coverage"] = match &filter_note {
            Some(note) => json!({"empty_dimensions":[],"checked":false,"reason":note}),
            None => json!(coverage),
        };
        value["freshness"] = render::machine_freshness(no_auto, pending_sources);
        value["semantic_coverage"] = lane_coverage;
        if fell_back {
            value["note"] = json!("no exact phrase match; showing hits containing all terms");
        }
        surface::write(&page::json(&page, &mut value)?)?;
    } else if blocks.is_empty() {
        let n: i64 = c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
        eprintln!(
            "no hits across {} indexed session{}.",
            surface::number(n.max(0) as usize),
            if n == 1 { "" } else { "s" }
        );
        disclose();
    } else {
        surface::write(&page::text(&page, color)?)?;
        disclose();
    }
    render::note_no_auto(no_auto);
    if let Some(notice) = &notice {
        eprintln!("{notice}");
    }
    Ok(exit)
}

#[cfg(test)]
mod tests {
    use super::*;
    use agrep_core::evidence::Row;

    fn coverage() -> semantic::Coverage {
        semantic::Coverage {
            embedded: 24,
            total: 24,
        }
    }
    fn pick(session: &str, turn: i64, matched: &str) -> Hit {
        Hit {
            session: session.into(),
            family: session.into(),
            turn,
            matched: matched.into(),
            who: "user".into(),
            snippet: format!("{session} {turn}"),
            ..Hit::default()
        }
    }
    // py/recall.py:891-932: a query without a meaning row takes its best one over its own
    // family's row, else over its weakest row once it holds two.
    #[test]
    fn packs_reserve_a_meaning_slot_per_query_as_0_4_9() {
        let picked = vec![
            (0, pick("s1", 0, "phrase")),
            (1, pick("s2", 0, "phrase")),
            (0, pick("s3", 0, "all-terms")),
            (1, pick("s4", 0, "phrase")),
        ];
        let meanings = vec![
            vec![pick("s2", 7, "semantic"), pick("s9", 1, "semantic")],
            vec![pick("s2", 5, "semantic")],
        ];
        let out: Vec<(usize, String, i64)> = reserve(picked.clone(), &meanings, 4, false)
            .into_iter()
            .map(|(q, h)| (q, h.session, h.turn))
            .collect();
        assert_eq!(
            out,
            [
                (0, "s1".to_owned(), 0),
                (1, "s2".to_owned(), 5),
                (0, "s9".to_owned(), 1),
                (1, "s4".to_owned(), 0),
            ]
        );
        let unchanged: Vec<_> = reserve(picked.clone(), &meanings, 2, false)
            .into_iter()
            .map(|(q, h)| (q, h.session))
            .collect();
        assert_eq!(
            unchanged,
            picked
                .into_iter()
                .map(|(q, h)| (q, h.session))
                .collect::<Vec<_>>()
        );
    }
    // Expected values are agrep 0.4.9's `recall --json` and `pack --json` output on a fixture
    // with 24 embedded rows, without the keys PORTING.md lists as not carried.
    #[test]
    fn semantic_envelope_matches_0_4_9() {
        let full = json!({"indexed":24,"total":24,"pending":0,"fraction":1.0,"complete":true,"order":"newest-first"});
        let ready = json!({"state":"ready","eligible":true,"reason":null,"min_score":0.82,"score_kind":"cosine","coverage":full,"partial":false,"complete":true,"fallback_recommended":false});
        assert_eq!(semantic::status(Some(&coverage()), true), ready);
        let none = json!({"state":"no-confident-match","eligible":true,"reason":null,"min_score":0.82,"score_kind":"cosine","coverage":full,"partial":false,"complete":true,"fallback_recommended":false});
        let runs = vec![(
            "zebra quantum lighthouse marmalade".to_owned(),
            semantic::status(Some(&coverage()), false),
            Some(coverage()),
        )];
        assert_eq!(
            Value::Object(semantic_meta(&runs, false, false, false)),
            json!({"score_kind":"cosine","semantic_coverage":full,"partial":false,"semantic_status":none})
        );
        let runs = vec![
            (
                "how do we rotate secret credentials".to_owned(),
                ready.clone(),
                Some(coverage()),
            ),
            ("retry backoff".to_owned(), ready.clone(), Some(coverage())),
        ];
        assert_eq!(
            Value::Object(semantic_meta(&runs, true, true, false)),
            json!({"score_kind":"cosine","semantic_coverage":full,"partial":false,
                "semantic_status":{"state":"ready","complete":true,"fallback_recommended":false},
                "semantic_queries":[
                    {"query":"how do we rotate secret credentials","status":ready,"coverage":full},
                    {"query":"retry backoff","status":ready,"coverage":full}],
                "hybrid":true})
        );
        // py/recall.py:2575-2580: meaning rows that lost the merge leave only the status.
        assert_eq!(
            Value::Object(semantic_meta(&runs[..1], false, false, true)),
            json!({"semantic_status":ready})
        );
        // py/search.py:581-599: a lane that failed reports unavailable and partial.
        let failed = vec![("q".to_owned(), semantic::status(None, false), None)];
        let meta = semantic_meta(&failed, false, false, false);
        assert_eq!(
            (
                &meta["score_kind"],
                &meta["partial"],
                &meta["semantic_coverage"]
            ),
            (&json!("unavailable"), &json!(true), &Value::Null)
        );
        assert_eq!(
            (
                &meta["semantic_status"]["state"],
                &meta["semantic_status"]["fallback_recommended"]
            ),
            (&json!("unavailable"), &json!(true))
        );
    }
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
