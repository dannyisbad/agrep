use super::*;
/// 0.4.9's `_content_terms`: the query's tokens without edge punctuation, at least two
/// characters long and not connectives.
pub(super) fn content_words(pattern: &str) -> Vec<String> {
    const STOP:&str="a an and are as at be been but by can could did do does for from had has have how i if in is it its me my not of on or our should so that the their them they this these those to was we were what when where which who why will with would you your whats what's im i'm dont don't isnt isn't didnt didn't";
    boundary_rank::query_tokens(pattern)
        .into_iter()
        .map(|t| t.trim_matches(|c: char| c.is_ascii_punctuation()))
        .filter(|t| {
            t.chars().count() >= 2
                && !STOP
                    .split_whitespace()
                    .any(|s| s == crate::unicode_v16::lowercase(t))
        })
        .map(crate::unicode_v16::lowercase)
        .filter(|t| !t.is_empty())
        .collect()
}
/// The content fallback's terms as 0.4.9's content lane read them (py/corpusdb.py:4871):
/// lowercased, deduplicated, each matching literally without spelling variants.
pub(super) fn content_terms(pattern: &str) -> Vec<Vec<String>> {
    let mut seen = HashSet::new();
    content_words(pattern)
        .into_iter()
        .filter(|t| seen.insert(t.clone()))
        .map(|t| vec![t])
        .collect()
}
/// 0.4.9 drew content candidates from the three rarest terms of three or more characters
/// (py/corpusdb.py:4885-4886).
pub(super) fn content_anchors(terms: &[Vec<String>]) -> usize {
    terms
        .iter()
        .filter(|v| v[0].chars().count() >= 3)
        .count()
        .min(3)
}
pub(super) fn automatic_content(pattern: &str) -> bool {
    if !pattern.trim().contains(' ') {
        return false;
    }
    let words = content_words(pattern);
    !words.is_empty()
        && words
            != boundary_rank::query_tokens(pattern)
                .iter()
                .map(|s| crate::unicode_v16::lowercase(s))
                .collect::<Vec<_>>()
}
/// 0.4.9's content-lane term weights (py/corpusdb.py:4863-4884): `ln(1 + N / (df + 1))`,
/// where `N` counts the lane's rows and `df` the rows holding the term; a term under three
/// characters counts as in every row. Prose rows are counted exactly through the trigram
/// index; tool rows, when in scope, by their word dictionary's frequency total.
pub(super) fn content_idf(
    c: &Connection,
    terms: &[Vec<String>],
    prose_only: bool,
    cache: &mut PlanCache,
) -> Result<Vec<f64>> {
    let count = store::counts(c)?;
    let prose = count["prose"].as_f64().unwrap_or(0.);
    let n = if prose_only {
        prose
    } else {
        prose + count["tools"].as_f64().unwrap_or(0.)
    };
    let n = if n == 0. { 1. } else { n };
    terms
        .iter()
        .map(|variants| {
            let term = &variants[0];
            let df = if term.chars().count() < 3 {
                n
            } else {
                let rows: f64 = c.query_row(
                    "SELECT count(*) FROM prose_fts WHERE prose_fts MATCH ?",
                    [format!("\"{}\"", term.replace('"', "\"\""))],
                    |r| r.get::<_, i64>(0),
                )? as f64;
                if prose_only {
                    rows
                } else {
                    (rows + cache.tool_doc(c, term)?.map_or(n, |doc| doc as f64)).min(n)
                }
            };
            Ok((1. + n / (df + 1.)).ln())
        })
        .collect()
}
/// 0.4.9's forced coverage lane (py/corpusdb.py:5154-5213, py/search.py:6353-6445): the
/// 400 rows SQLite's bm25 ranks best for any of the query's words of three characters or
/// more, without rows quoting the whole query, then one per chat and one per chat family,
/// at most 20. Presentation prints them after the page; they never count toward its total
/// or exit status.
pub fn coverage(c: &Connection, o: &Options) -> Result<Results> {
    if o.regex || o.word || o.count_only || o.count_by_tier || o.sort != "score" {
        bail!("coverage requires a keyword score query without counts");
    }
    let started = Instant::now();
    let raw: Vec<&str> = o
        .pattern
        .trim()
        .split(|c: char| c.is_whitespace() || c == '-' || c == '_')
        .filter(|w| !w.is_empty())
        .collect();
    let mut words: Vec<String> = vec![];
    for word in raw.iter().map(|w| crate::unicode_v16::lowercase(w)) {
        if !word.is_empty() && !words.contains(&word) {
            words.push(word);
        }
    }
    let long: Vec<&String> = words.iter().filter(|w| w.chars().count() >= 3).collect();
    let mut result = Results {
        total_basis: "exact".into(),
        ..Default::default()
    };
    if raw.len() < 2 || long.is_empty() {
        return Ok(result);
    }
    let lanes = coverage_lanes(c, o, &long)?;
    let mut ranked: Vec<(f64, &str, i64)> = vec![];
    for (table, expression) in &lanes {
        let (filters, mut values) = sql_filters(&o.filters, "r", c)?;
        values.insert(0, Value::Text(expression.clone()));
        let mut q = c.prepare(&format!(
            "SELECT r.id,bm25({table}_fts) FROM {table}_fts JOIN {table} r ON r.id={table}_fts.rowid WHERE {table}_fts MATCH ? AND r.visible=1{filters} ORDER BY 2 LIMIT 400"
        ))?;
        let rows = q.query_map(rusqlite::params_from_iter(values), |r| {
            Ok((r.get::<_, f64>(1)?, *table, r.get::<_, i64>(0)?))
        })?;
        for row in rows {
            ranked.push(row?);
        }
    }
    ranked.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(b.1)).then(a.2.cmp(&b.2)));
    ranked.truncate(400);
    let echo = regex::Regex::new(
        &raw.iter()
            .map(|w| crate::pattern::literal(w))
            .collect::<Vec<_>>()
            .join(crate::unicode_v16::keyword_gap()),
    )?;
    let (mut chats, mut families) = (HashSet::new(), HashSet::new());
    for (bm25, table, id) in ranked {
        let mut h = c.query_row(
            &format!("SELECT r.session,r.agent,r.project,r.position,r.who,r.model,r.model_source,r.ts,r.text,s.family,s.side FROM {table} r JOIN sessions s ON s.id=r.session AND s.agent=r.agent WHERE r.id=?"),
            [id],
            |r| {
                Ok(Hit {
                    rowid: id,
                    session: r.get(0)?,
                    agent: r.get(1)?,
                    project: r.get(2)?,
                    turn: r.get(3)?,
                    who: r.get(4)?,
                    model: r.get(5)?,
                    model_source: r.get(6)?,
                    ts: r.get(7)?,
                    text: store::decode(table, r.get::<_, Vec<u8>>(8)?).map_err(|e| {
                        rusqlite::Error::FromSqlConversionFailure(
                            8,
                            rusqlite::types::Type::Blob,
                            e.into_boxed_dyn_error(),
                        )
                    })?,
                    family: r.get(9)?,
                    side: r.get(10)?,
                    score: -bm25,
                    matched: "content-terms".into(),
                    tier: 2,
                    ..Default::default()
                })
            },
        )?;
        let (span, echoed) = {
            let row = Folded::lowercase(&h.text);
            // Trigram candidates are a superset; the lead is the earliest word the row holds.
            let Some((at, end)) = words
                .iter()
                .filter_map(|w| row.low.find(w.as_str()).map(|at| (at, at + w.len())))
                .min()
            else {
                continue;
            };
            (row.start(at)..row.end(end), echo.is_match(&h.text))
        };
        let family = if h.family.is_empty() {
            h.session.clone()
        } else {
            h.family.clone()
        };
        if echoed
            || !chats.insert((h.agent.clone(), h.session.clone()))
            || !families.insert((h.agent.clone(), family))
        {
            continue;
        }
        h.span = Some((
            h.text[..span.start].chars().count(),
            h.text[..span.end].chars().count(),
        ));
        h.snippet = Snip::new(&h.text, std::slice::from_ref(&span)).text;
        result.hits.push(h);
        if result.hits.len() == 20 {
            break;
        }
    }
    result.total = result.hits.len();
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
/// Below this many scope rows, decoding a run's rows costs less than reading
/// its postings.
pub(super) const SCOPE_INDEX_ROWS: usize = 32;
/// The most blocks a bounded scan reads its admitted rows for at once. One rowid-range
/// query opens each word's postings once for the run instead of once per block, and
/// larger runs widen the word union faster than they save.
pub(super) const RUN_BLOCKS: usize = 64;
/// Consecutive blocks a run may step over: ones without a ceiling, already read, or
/// below the page's cutoff.
pub(super) const RUN_GAP: usize = 8;

fn coverage_lanes(
    c: &Connection,
    o: &Options,
    long: &[&String],
) -> Result<Vec<(&'static str, String)>> {
    let quote = |w: &str| format!("\"{}\"", store::search_fold(w).replace('"', "\"\""));
    let who = |role: &str| {
        o.filters
            .who
            .as_deref()
            .is_none_or(|who| who.split(',').any(|w| (w == "tool") == (role == "tool")))
            && !o
                .filters
                .no_who
                .as_deref()
                .is_some_and(|who| role == "tool" && who.split(',').any(|w| w == "tool"))
    };
    let mut lanes = vec![];
    if who("prose") {
        lanes.push((
            "prose",
            long.iter()
                .map(|w| quote(w))
                .collect::<Vec<_>>()
                .join(" OR "),
        ));
    }
    // 0.4.9's lane read every row its speaker filters admit, tool output included whatever
    // the tools setting said (py/corpusdb.py:4665-4674, py/search.py:6386-6391).
    if who("tool") {
        // Tool rows are indexed by word: a query word matches the words holding it.
        let mut expanded = vec![];
        for w in long {
            for word in crate::tool_index::expand(c, w)?.unwrap_or_default() {
                expanded.push(quote(&word.term));
            }
        }
        if !expanded.is_empty() {
            lanes.push(("tools", expanded.join(" OR ")));
        }
    }
    Ok(lanes)
}
