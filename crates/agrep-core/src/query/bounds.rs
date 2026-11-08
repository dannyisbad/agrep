use super::*;

/// Each block's ceiling on the raw scores of this pass's rows; their rounding and lane
/// folding are applied where pages compare them.
///
/// A phrase row scores `tight * (1 - 0.5^n) * R * W * B` over its snippet. Every phrase
/// match counted there is a match of the row's folded text, so it holds a distinct
/// occurrence of each token's grams: `n` is at most the least gram maximum. A match
/// spans at least the query's characters plus one per adjacent token pair whose
/// bridging gram the block lacks (`Matcher::phrase_strength` never measures it
/// shorter), which bounds `tight`. `R * W` is at most each gram's newest weighted
/// recency, and `B <= 1`. An explicit pattern's snippet matches each begin or end with
/// its required literals, so they too hold distinct occurrences of its grams; a pattern
/// that matches a snippet at most once (`matches_once`) has `n <= 1`.
pub(super) fn block_bounds(
    c: &Connection,
    m: &Matcher,
    now: i64,
    table: &str,
    cache: &mut PlanCache,
    coverage: Option<&CoverageBounds>,
) -> Result<Vec<BlockBound>> {
    let (lane, metadata) = if table == "tools" {
        (1, "tool_blocks")
    } else {
        (0, "blocks")
    };
    let grams = m.bound_grams();
    let bridges = m.phrase_bridges();
    let all_terms = if m.phase == 2 && !m.partial && m.explicit.is_none() {
        m.all_terms_ceiling()
    } else {
        None
    };
    let map = posting_map(c, cache, lane, &grams, &bridges, &all_terms)?;
    let mut q = c.prepare(&format!(
        "SELECT id,max_ts,user_ts,weight,min_session,min_turn,max_gap,all_ascii FROM {metadata}"
    ))?;
    let rows = q.query_map([], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, Option<i64>>(2)?,
            r.get::<_, f64>(3)?,
            r.get::<_, String>(4)?,
            r.get::<_, i64>(5)?,
            r.get::<_, i64>(6)?,
            r.get::<_, bool>(7)?,
        ))
    })?;
    let mut out = vec![];
    for r in rows {
        let (id, ts, user_ts, w, min_session, min_turn, max_gap, all_ascii) = r?;
        let global_clock = if m.explicit.is_some() {
            w * recency(ts, now).max(0.5)
        } else {
            (w * recency(ts, now)).max(user_ts.map_or(0., |t| recency(t, now).max(0.35)))
        };
        let mut matching_clock = global_clock;
        let mut matching_ts = ts;
        let mut n = if m.partial { 0 } else { u32::MAX };
        let mut possible_weight = 0.;
        let mut selected_possible = m.coverage_term.is_none();
        for (i, variants) in grams.iter().enumerate() {
            let (mut sum, mut group_clock, mut group_ts) =
                group_bound(m, now, id, ts, global_clock, variants, &map);
            // A tool block without any expanded word cannot contain the term,
            // even when its trigrams occur separately.
            if m.tool_presence
                .get(i)
                .and_then(Option::as_ref)
                .is_some_and(|blocks| !has_block(blocks, id))
            {
                sum = 0;
                group_clock = 0.;
                group_ts = 0;
            }
            if m.coverage_term == Some(i) && sum > 0 {
                selected_possible = true;
            }
            if m.partial {
                n = n.saturating_add(sum);
                if sum > 0 {
                    possible_weight += m.idf.get(i).copied().unwrap_or(1.);
                }
            } else {
                n = n.min(sum);
                matching_clock = matching_clock.min(group_clock);
                matching_ts = matching_ts.min(group_ts);
            }
        }
        if n == 0 || !selected_possible {
            continue;
        }
        if let Some(coverage) = coverage {
            let Some(weight) = coverage.blocks.get(&id) else {
                continue;
            };
            possible_weight = *weight;
        }
        // All-terms scores have no repetition penalty. Their finite ceiling
        // must therefore use one, independently of the phrase occurrence cap.
        let repetition = if m.phase == 2 {
            1.
        } else {
            1. - 2f64.powi(-(n.min(if m.explicit_once { 1 } else { 60 }) as i32))
        };
        let absent = |g: &String| {
            !map.get(g)
                .is_some_and(|postings| postings.contains_key(&id))
        };
        // If a cross-token gram is absent, this block cannot contain that
        // pair with zero separator characters.
        let gaps = bridges
            .iter()
            .filter(|bridge| !bridge.is_empty() && bridge.iter().any(absent))
            .count();
        let phrase_tightness = if m.phase == 2 {
            match &all_terms {
                Some((ceiling, joined))
                    if all_ascii && joined.iter().all(|bridge| bridge.iter().any(absent)) =>
                {
                    *ceiling
                }
                _ => 1.,
            }
        } else if bridges.is_empty() {
            1.
        } else {
            (m.query_chars as f64 / (m.query_chars + gaps).max(1) as f64).min(1.)
        };
        let rw = if m.partial {
            global_clock
        } else {
            matching_clock
        };
        let ts = if m.partial { ts } else { matching_ts };
        out.push((
            id,
            if m.partial {
                rw * m.partial_share(possible_weight)
            } else {
                phrase_tightness * repetition * rw
            },
            ts,
            min_session,
            min_turn,
            max_gap,
        ));
    }
    out.sort_by(|a, b| {
        b.1.total_cmp(&a.1)
            .then(b.2.cmp(&a.2))
            .then(a.3.cmp(&b.3))
            .then(a.4.cmp(&b.4))
            .then(a.0.cmp(&b.0))
    });
    Ok(out)
}

type PostingMap = HashMap<String, HashMap<i64, store::Posting>>;
fn posting_map(
    c: &Connection,
    cache: &mut PlanCache,
    lane: i64,
    grams: &[Vec<Vec<String>>],
    bridges: &[Vec<String>],
    all_terms: &Option<(f64, Vec<Vec<String>>)>,
) -> Result<PostingMap> {
    let mut map: HashMap<String, HashMap<i64, store::Posting>> = HashMap::new();
    for g in grams
        .iter()
        .flatten()
        .flatten()
        .chain(bridges.iter().flatten())
        .chain(
            all_terms
                .iter()
                .flat_map(|(_, joined)| joined.iter().flatten()),
        )
    {
        if !store::indexed_gram(g) || map.contains_key(g) {
            continue;
        }

        let key = (lane, g.clone());
        if !cache.postings.contains_key(&key) {
            cache
                .postings
                .insert(key.clone(), store::posting_list(c, lane, g)?);
        }
        map.insert(
            g.clone(),
            cache.postings[&key]
                .iter()
                .map(|&posting| (i64::from(posting.block), posting))
                .collect(),
        );
    }
    Ok(map)
}

fn group_bound(
    m: &Matcher,
    now: i64,
    id: i64,
    ts: i64,
    global_clock: f64,
    variants: &[Vec<String>],
    map: &PostingMap,
) -> (u32, f64, i64) {
    let mut sum = 0u32;
    let mut group_clock: f64 = 0.;
    let mut group_ts = 0;
    for terms in variants {
        let mut count = u32::MAX;
        let mut clock = global_clock;
        let mut timestamp = ts;
        for g in terms {
            if !store::indexed_gram(g) {
                continue;
            }
            if let Some(posting) = map.get(g).and_then(|rows| rows.get(&id)) {
                count = count.min(posting.frequency);
                timestamp = timestamp.min(posting.max_ts);
                let weight = store::posting_weight(posting.weight);
                let upper = if m.explicit.is_some() {
                    weight * recency(posting.max_ts, now).max(0.5)
                } else {
                    (weight * recency(posting.max_ts, now)).max(if posting.weight == 6 {
                        0.35
                    } else {
                        0.
                    })
                };
                clock = clock.min(upper);
            } else {
                count = 0;
                clock = 0.;
                timestamp = 0;
                break;
            }
        }
        sum = sum.saturating_add(count);
        if count > 0 {
            group_clock = group_clock.max(clock);
            group_ts = group_ts.max(timestamp);
        }
    }
    (sum, group_clock, group_ts)
}
