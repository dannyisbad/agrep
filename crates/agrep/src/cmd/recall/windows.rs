use super::*;
/// Bytes the hit's own turn takes as rendered rows, at most one judgeable block.
pub(super) fn hit_need(w: &Window) -> usize {
    page::hit_need(&page::turns(&w.rows), &BTreeSet::from([w.center]))
}
/// Coverage expansion: a hit session whose prose fits its share of the budget left after
/// every hit's own turn is shown whole; then turns elsewhere in the hit sessions holding
/// the most distinct query terms, newest first, get windows of one turn around them until
/// the page holds as many windows as the budget feeds (one per 2,500 bytes, at most 12).
pub(super) fn expand(
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
pub(super) fn merge_chats(
    pairs: Vec<(Hit, Window)>,
    query_of: &HashMap<String, usize>,
) -> Vec<Block> {
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
