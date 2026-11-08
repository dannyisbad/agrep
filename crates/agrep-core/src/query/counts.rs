use super::*;
/// A necessary token proximity condition, verified against canonical text later.
pub(super) fn tool_near(expression: &str, ordinals: &[u8], gap: i64) -> Option<String> {
    let words: Vec<_> = expression.split('"').skip(1).step_by(2).collect();
    let positions: Vec<_> = ordinals
        .chunks_exact(4)
        .map(|bytes| u32::from_le_bytes(bytes.try_into().unwrap()))
        .collect();
    if !(2..=3).contains(&words.len())
        || positions.len() != words.len()
        || positions.windows(2).any(|p| p[0] >= p[1])
        || words
            .iter()
            .any(|w| !w.bytes().all(|b| b.is_ascii_alphanumeric()))
        || words.iter().collect::<HashSet<_>>().len() != words.len()
    {
        return None;
    }
    let distance = i64::from(positions.last()? - positions.first()?)
        .saturating_mul(gap.saturating_add(1))
        .min(i64::from(i32::MAX));
    if gap == 0 && positions.windows(2).all(|pair| pair[1] == pair[0] + 1) {
        return Some(format!("\"{}\"", words.join(" ")));
    }
    Some(format!(
        "NEAR({}, {distance})",
        words
            .iter()
            .map(|word| format!("\"{word}\""))
            .collect::<Vec<_>>()
            .join(" ")
    ))
}
/// The rows of a selective scope that its lane's text index admits, using the
/// expressions unselective plans rely on as necessary conditions. Each run of
/// consecutive scope blocks is one rowid-range query, so a word's postings are
/// opened once per run rather than once per block. Blocks absent from the map
/// are not filtered.
pub(super) fn scope_candidates(
    c: &Connection,
    m: &Matcher,
    table: &str,
    expression: &str,
    span: Option<&[u8]>,
    scope: &[(i64, usize)],
) -> Result<HashMap<i64, Vec<i64>>> {
    let mut admitted = HashMap::new();
    let mut rest = scope;
    while let Some(&(first, _)) = rest.first() {
        let length = rest
            .iter()
            .enumerate()
            .take_while(|&(i, &(block, _))| block == first + i as i64)
            .count();
        let (run, tail) = rest.split_at(length);
        rest = tail;
        if run.iter().map(|&(_, rows)| rows).sum::<usize>() < SCOPE_INDEX_ROWS {
            continue;
        }
        let blocks: Vec<i64> = run.iter().map(|&(block, _)| block).collect();
        admit_run(
            c,
            m,
            table,
            expression,
            span,
            &blocks,
            None,
            &mut admitted,
            None,
        )?;
    }
    Ok(admitted)
}
/// Adds to `admitted` the rows of `blocks` (sorted) that the text index admits, read with
/// one rowid-range query over their span; other blocks inside it are left out. Tool rows
/// are bounded by `max_gap`, which a consecutive run may leave to its blocks' metadata.
#[allow(clippy::too_many_arguments)]
pub(super) fn admit_run(
    c: &Connection,
    m: &Matcher,
    table: &str,
    expression: &str,
    span: Option<&[u8]>,
    blocks: &[i64],
    max_gap: Option<i64>,
    admitted: &mut HashMap<i64, Vec<i64>>,
    mut occurrences: Option<&mut HashMap<i64, u32>>,
) -> Result<()> {
    let (first, last) = (blocks[0], blocks[blocks.len() - 1]);
    // Tool blocks hold their own words; a required group without any word
    // in the run admits no row of it.
    let own = if table != "tools" {
        None
    } else {
        let plan = if span.is_some() {
            m.word_positions(Some(blocks))
        } else {
            m.fts(Some(blocks))
                .map(|expression| (expression, Vec::new()))
        };
        let Some(plan) = plan else {
            admitted.extend(blocks.iter().map(|&block| (block, Vec::new())));
            return Ok(());
        };
        Some(plan)
    };
    let (expression, ordinals) = own.as_ref().map_or((expression, span), |(e, o)| {
        (e.as_str(), Some(o.as_slice()))
    });
    let near = (table == "tools" && span.is_some())
        .then(|| {
            ordinals.and_then(|o| {
                max_gap
                    .and_then(|gap| tool_near(expression, o, gap))
                    .map(|e| (e, o.to_vec()))
            })
        })
        .flatten();
    let (expression, ordinals) = near.as_ref().map_or((expression, ordinals), |(e, o)| {
        (e.as_str(), Some(o.as_slice()))
    });
    let span = if table == "tools" && near.as_ref().is_some_and(|(e, _)| e.starts_with('"')) {
        None
    } else {
        span
    };
    let mut sql = match (table, span) {
        ("tools", Some(_)) => "SELECT f.rowid FROM tools_fts f LEFT JOIN event_rows e INDEXED BY event_tool_id ON e.tool_id=f.rowid AND e.active=1 LEFT JOIN tool_blobs t ON t.id=e.blob WHERE tools_fts MATCH ?1 AND f.rowid>=?2 AND f.rowid<?3 AND agrep_word_span(tools_fts,?4,CASE WHEN t.ascii THEN 0 ELSE ?5 END)".to_owned(),
        (_, Some(_)) => format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?1 AND rowid>=?2 AND rowid<?3 AND agrep_word_span({table}_fts,?4,?5)"),
        (_, None) => format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?1 AND rowid>=?2 AND rowid<?3"),
    };
    let mut bind = vec![
        Value::Text(expression.to_owned()),
        Value::Integer(first * BLOCK),
        Value::Integer((last + 1) * BLOCK),
    ];
    if occurrences.is_some() {
        let words = m.group_words(&m.ordered_terms[0], Some(blocks));
        // Separate posting walks avoid merging hundreds of OR position lists
        // for every row. The union retains every ID and the same occurrence cap.
        if words.len() >= 32 {
            let counts =
                tool_rank::word_rows(c, &words, &m.terms[0][0], first * BLOCK, (last + 1) * BLOCK)?;
            let mut rows: HashMap<i64, Vec<i64>> =
                blocks.iter().map(|&b| (b, Vec::new())).collect();
            for (id, count) in counts {
                if let Some(ids) = rows.get_mut(&(id / BLOCK)) {
                    ids.push(id);
                    if let Some(occurrences) = occurrences.as_deref_mut() {
                        occurrences.insert(id, count);
                    }
                }
            }
            for ids in rows.values_mut() {
                ids.sort_unstable();
            }
            admitted.extend(rows);
            return Ok(());
        }
        let weights = words
            .iter()
            .flat_map(|w| (w.matches(m.terms[0][0].as_str()).count() as u32).to_le_bytes())
            .collect::<Vec<_>>();
        sql = "SELECT rowid,agrep_tool_occurrences(tools_fts,?4) FROM tools_fts WHERE tools_fts MATCH ?1 AND rowid>=?2 AND rowid<?3".into();
        bind.push(Value::Blob(weights));
    }
    if let Some(ordinals) = ordinals.filter(|_| span.is_some()) {
        // The widest gap of the run's blocks bounds every row in it.
        let max_gap = if table != "tools" {
            i64::from(i32::MAX)
        } else if let Some(max_gap) = max_gap {
            max_gap
        } else {
            c.query_row(
                "SELECT CASE WHEN count(*)=?3 THEN max(max_gap) ELSE 2147483647 END FROM tool_blocks WHERE id BETWEEN ?1 AND ?2",
                rusqlite::params![first, last, blocks.len() as i64],
                |r| r.get(0),
            )?
        };
        bind.push(Value::Blob(ordinals.to_vec()));
        bind.push(Value::Integer(max_gap));
    }
    let mut rows: HashMap<i64, Vec<i64>> =
        blocks.iter().map(|&block| (block, Vec::new())).collect();
    let mut query = c.prepare_cached(&sql)?;
    let ranked = occurrences.is_some();
    for row in query.query_map(params_from_iter(bind), |r| {
        Ok((
            r.get::<_, i64>(0)?,
            if ranked {
                r.get::<_, Option<u32>>(1)?
            } else {
                None
            },
        ))
    })? {
        let (id, count) = row?;
        if let Some(ids) = rows.get_mut(&(id / BLOCK)) {
            ids.push(id);
            if let (Some(count), Some(occurrences)) = (count, occurrences.as_deref_mut()) {
                occurrences.insert(id, count);
            }
        }
    }
    for ids in rows.values_mut() {
        ids.sort_unstable();
    }
    admitted.extend(rows);
    Ok(())
}
pub(super) fn count_tool_terms(
    c: &Connection,
    m: &Matcher,
    scope: &str,
    values: &[Value],
    unfiltered: bool,
) -> Result<usize> {
    let mut selected: Option<HashSet<i64>> = None;
    let mut query = c.prepare_cached("SELECT rowid FROM tools_fts WHERE tools_fts MATCH ?")?;
    for group in &m.ordered_terms {
        let words = m.group_words(group, None);
        let mut matched = HashSet::new();
        for word in words {
            let expression = format!("\"{}\"", word.replace('"', "\"\""));
            for row in query.query_map([expression], |r| r.get::<_, i64>(0))? {
                let id = row?;
                if selected.as_ref().is_none_or(|set| set.contains(&id)) {
                    matched.insert(id);
                }
            }
        }
        if matched.is_empty() {
            return Ok(0);
        }
        selected = Some(matched);
    }
    let ids = selected.unwrap_or_default();
    if unfiltered {
        return Ok(ids.len());
    }
    let mut ids: Vec<_> = ids.into_iter().collect();
    ids.sort_unstable();
    let mut count = 0;
    for chunk in ids.chunks(512) {
        let candidates = crate::tool_store::CANDIDATES_BY_ID;
        let sql=format!("WITH ids(id) AS (VALUES {}) SELECT count(*) FROM ids CROSS JOIN {candidates} p ON p.id=ids.id WHERE 1{scope}",vec!["(?)";chunk.len()].join(","));
        let mut bind: Vec<_> = chunk.iter().copied().map(Value::Integer).collect();
        bind.extend_from_slice(values);
        count += c
            .prepare_cached(&sql)?
            .query_row(params_from_iter(bind), |r| r.get::<_, usize>(0))?;
    }
    Ok(count)
}
/// The matches among a session-shaped scope's rows, which `rows` yields as (id, session)
/// through the session indexes. The text index answers which of them match, one rowid
/// range per run of consecutive blocks holding them, so the work follows the scope's
/// size rather than the pattern's matches across the corpus. Prose matches
/// `expression`; a tool run matches the words its own blocks hold. Each match is
/// tallied under its session in `tally` when one is given.
pub(super) fn count_scope(
    c: &Connection,
    m: &Matcher,
    table: &str,
    expression: Option<&str>,
    rows: &str,
    values: &[Value],
    tally: Option<&mut HashMap<String, usize>>,
) -> Result<usize> {
    let mut names: Vec<String> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    let mut ids: Vec<(i64, usize)> = Vec::new();
    {
        let mut stmt = c.prepare_cached(rows)?;
        let mut found = stmt.query(params_from_iter(values))?;
        while let Some(r) = found.next()? {
            let session = r.get_ref(1)?.as_str()?;
            let at = match index.get(session) {
                Some(&at) => at,
                None => {
                    names.push(session.to_owned());
                    index.insert(session.to_owned(), names.len() - 1);
                    names.len() - 1
                }
            };
            ids.push((r.get(0)?, at));
        }
    }
    ids.sort_unstable();
    let mut counts = vec![0usize; names.len()];
    let mut query = c.prepare_cached(&format!(
        "SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?1 AND rowid>=?2 AND rowid<?3"
    ))?;
    let mut rest = &ids[..];
    while let Some(&(first, _)) = rest.first() {
        let mut blocks = vec![first / BLOCK];
        let mut length = 1;
        while let Some(&(id, _)) = rest.get(length) {
            let (block, last) = (id / BLOCK, blocks[blocks.len() - 1]);
            if block > last + 1 {
                break;
            }
            if block > last {
                blocks.push(block);
            }
            length += 1;
        }
        let (run, tail) = rest.split_at(length);
        rest = tail;
        let own;
        let expression = match expression {
            Some(expression) => expression,
            None => match m.fts(Some(&blocks)) {
                Some(words) => {
                    own = words;
                    own.as_str()
                }
                None => continue,
            },
        };
        let bounds = (first, run[run.len() - 1].0 + 1);
        let bind = rusqlite::params![expression, bounds.0, bounds.1];
        for id in query.query_map(bind, |r| r.get::<_, i64>(0))? {
            if let Ok(at) = run.binary_search_by_key(&id?, |&(id, _)| id) {
                counts[run[at].1] += 1;
            }
        }
    }
    if let Some(tally) = tally {
        for (name, n) in names.into_iter().zip(&counts) {
            *tally.entry(name).or_default() += n;
        }
    }
    Ok(counts.iter().sum())
}
/// The matches among the rows a caller filter alone withholds: rows of its agent in the
/// sessions it names (and its family's, when it excludes the family) that it rejects.
pub(super) fn caller_withheld(
    c: &Connection,
    m: &Matcher,
    table: &str,
    expression: Option<&str>,
    caller: &CallerFilter,
) -> Result<usize> {
    let only = Filters {
        caller: Some(caller.clone()),
        ..Filters::default()
    };
    let (kept, kept_values) = sql_filters(&only, "p", c)?;
    if kept.is_empty() {
        return Ok(0);
    }
    let mut sessions = caller.excluded_sessions.clone();
    sessions.push(caller.session.clone());
    if caller.exclude_family {
        sessions.extend(
            c.prepare_cached("SELECT id FROM sessions WHERE agent=? AND family=?")?
                .query_map([&caller.agent, &caller.family], |r| r.get(0))?
                .collect::<rusqlite::Result<Vec<String>>>()?,
        );
    }
    sessions.sort_unstable();
    sessions.dedup();
    let rows = format!("SELECT p.id,p.session FROM {table} p WHERE p.visible=1 AND +p.agent=? AND p.session IN (SELECT value FROM json_each(?)) AND NOT (1{kept})");
    let mut values = vec![
        Value::Text(caller.agent.clone()),
        Value::Text(serde_json::to_string(&sessions)?),
    ];
    values.extend(kept_values);
    count_scope(c, m, table, expression, &rows, &values, None)
}
/// The exact matching rows of `o` in each of `sessions`, matched by exact id under any
/// agent, as 0.4.9 counted a chat listing's `session_hits`: over the query's filters, not
/// narrowed by `--no-meta`, which only withholds displayed rows.
pub fn session_counts(
    c: &Connection,
    o: &Options,
    sessions: &[String],
    plans: &mut Plans,
) -> Result<Vec<usize>> {
    if sessions.is_empty() {
        return Ok(Vec::new());
    }
    let mut scoped = o.clone();
    (scoped.chats, scoped.content, scoped.limit) = (false, false, 0);
    (scoped.count_only, scoped.count_by_tier, scoped.exact) = (true, false, true);
    scoped.filters.no_meta = false;
    let mut ids = sessions.to_vec();
    ids.sort_unstable();
    ids.dedup();
    scoped.filters.sessions = Some(ids);
    let found = search_with(c, &scoped, plans)?;
    Ok(sessions
        .iter()
        .map(|s| found.session_counts.get(s).copied().unwrap_or(0))
        .collect())
}
