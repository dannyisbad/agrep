use super::*;
/// Whether 0.4.9's recall read tool rows for a query: its prose lane ran first and fell
/// back to tools when fewer than `target` chats (families, unless `by_session`) held a
/// prose match or none of those matches was a phrase. `rows` is the head, `depth` rows
/// deep, of the same tier-first ranking with tools included; `deeper` reads that many
/// prose rows when the head cannot decide.
pub(super) fn tool_lane(
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
/// 0.4.9's candidates for one query (py/recall.py:983-994, 2025-2035): each family's best
/// row in rank order, its session's under `per_session`, then lived evidence before ~meta.
pub(super) fn heads(hits: Vec<Hit>, per_session: bool) -> Vec<Hit> {
    let mut seen = HashSet::new();
    let mut heads: Vec<Hit> = hits
        .into_iter()
        .filter(|h| seen.insert(family_key(h, per_session).to_owned()))
        .collect();
    heads.sort_by_key(meta_row);
    heads
}
/// 0.4.9's recall under `--no-meta` selected from heads, not rows: the best row of each
/// of up to `max(target, 40)` families (chats under `per_session`) from the prose lane,
/// plus the tool lane's when prose held fewer than `target` or only bag-of-words
/// heads. Proven ~meta heads were dropped, the best one kept when every head was one,
/// and the rest ordered lived first, then by strength, lane and score.
pub(super) struct MetaHeads {
    pub(super) heads: Vec<Hit>,
    pub(super) dropped: usize,
    pub(super) retained: bool,
}
/// Takes each query's next hit in turn, skipping turns already taken and
/// families that query already supplied, until `target` hits. Also reports,
/// per query, whether selection asked for a hit past the end of its ranking.
pub(super) fn select(
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
pub(super) fn reserve(
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
pub(super) fn select_deepening<T>(
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
pub(super) const HEAD_COST: usize = 120;
/// Hit-turn bytes beyond which a block needs no more to be judged.
pub(super) const JUDGEABLE_BLOCK: usize = 900;
/// Visible hit-turn bytes below which a block is a stub rather than evidence.
pub(super) const JUDGEABLE_HIT: usize = 200;
/// What one `[+N chars - agrep around …]` marker costs.
pub(super) const CAP_MARKER_RESERVE: usize = 120;
/// The largest level with the sum of `min(extra, level)` within `budget`.
pub(super) fn water_level(extras: &[i64], budget: i64) -> i64 {
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
pub(super) fn hit_floors(
    needs: &[usize],
    heads: &[usize],
    lows: &[usize],
    budget: usize,
) -> Vec<usize> {
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

impl MetaHeads {
    /// `chats(limit, lane)` ranks chat heads: `Some(false)` prose, `Some(true)` tool rows,
    /// `None` the scope's own `--who` lane.
    pub(super) fn rank(
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
