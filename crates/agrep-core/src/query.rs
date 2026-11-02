//! Substring retrieval with admissible, query-specific block bounds.
use crate::{
    boundary_rank::{self, PreparedQuery, Stats},
    store::{self, BLOCK},
};
use anyhow::{bail, Result};
use rusqlite::{params_from_iter, types::Value, Connection};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
#[derive(Default, Clone, Debug, Serialize, Deserialize)]
pub struct Filters {
    pub agent: Option<String>,
    pub project: Option<String>,
    pub exclude_project: Option<String>,
    pub model: Option<String>,
    pub model_soft: bool,
    pub who: Option<String>,
    pub no_who: Option<String>,
    pub no_meta: bool,
    pub chat: Option<String>,
    pub since: Option<i64>,
    pub until: Option<i64>,
    pub no_side: bool,
}
#[derive(Clone, Debug)]
pub struct Options {
    pub pattern: String,
    pub regex: bool,
    pub word: bool,
    pub sort: String,
    pub limit: usize,
    pub exact: bool,
    pub count_only: bool,
    pub count_by_tier: bool,
    pub content: bool,
    pub chats: bool,
    pub tools: bool,
    pub filters: Filters,
}
#[derive(Default, Serialize, Deserialize, Clone, Debug)]
pub struct Hit {
    pub session: String,
    pub agent: String,
    pub project: String,
    pub family: String,
    pub side: bool,
    pub turn: i64,
    pub who: String,
    pub model: String,
    pub model_source: String,
    pub ts: i64,
    pub text: String,
    pub snippet: String,
    pub score: f64,
    pub matched: String,
    pub boundary_class: String,
    pub handle: String,
    #[serde(rename = "self")]
    pub self_marker: bool,
    #[serde(skip)]
    pub rowid: i64,
    #[serde(skip)]
    pub tier: u8,
}
#[derive(Default, Serialize, Deserialize, Debug)]
pub struct Results {
    pub hits: Vec<Hit>,
    pub total: usize,
    pub total_basis: String,
    pub examined: usize,
    pub blocks: usize,
    pub tiers: HashMap<String, usize>,
    pub tool_rows: usize,
    pub tools_excluded: bool,
    #[serde(skip)]
    session_keys: HashSet<(String, String)>,
}
pub fn weight(who: &str) -> f64 {
    match who {
        "user" => 1.,
        "agent" => 0.8,
        "subagent" => 0.7225,
        "recap" => 0.325,
        "tool" => 0.22,
        "control" => 0.325,
        "synthetic" | "harness" => 0.3,
        _ => 0.5,
    }
}
pub fn recency(ts: i64, now: i64) -> f64 {
    2f64.powf(-((now - ts).max(0) as f64) / (14. * 86400000.))
}
struct Matcher {
    quick_count: bool,
    partial: bool,
    phase: u8,
    terms: Vec<Vec<String>>,
    phrase: Option<regex::Regex>,
    explicit: Option<fancy_regex::Regex>,
    boundary: PreparedQuery,
    query_chars: usize,
    required: Vec<String>,
}
fn literal_terms(s: &str) -> Vec<String> {
    let Ok(hir) = regex_syntax::ParserBuilder::new().build().parse(s) else {
        return vec![];
    };
    let seq = regex_syntax::hir::literal::Extractor::new().extract(&hir);
    let Some(lits) = seq.literals() else {
        return vec![];
    };
    if lits.len() != 1 {
        return vec![];
    }
    let s = String::from_utf8_lossy(lits[0].as_bytes()).into_owned();
    if s.is_empty() {
        vec![]
    } else {
        vec![store::normalize(&s)]
    }
}
impl Matcher {
    fn new(o: &Options, c: &Connection) -> Result<Self> {
        let terms: Vec<Vec<String>> = boundary_rank::query_tokens(&o.pattern)
            .iter()
            .map(|t| boundary_rank::term_variants(t))
            .collect();
        if o.pattern.is_empty() || (terms.is_empty() && !o.regex && !o.word) {
            bail!("a nonempty search pattern is required")
        }
        let (explicit, required, phrase) = if o.regex || o.word {
            let pattern = if o.word {
                format!(r"\b(?:{})\b", regex::escape(&o.pattern))
            } else {
                o.pattern.clone()
            };
            let mut b = fancy_regex::RegexBuilder::new(&format!("(?i:{pattern})"));
            b.backtrack_limit(100_000);
            (
                Some(b.build()?),
                if o.word {
                    vec![store::normalize(&o.pattern)]
                } else {
                    literal_terms(&o.pattern)
                },
                None,
            )
        } else {
            let pattern = boundary_rank::query_tokens(&o.pattern)
                .iter()
                .map(|t| regex::escape(&store::normalize(t)))
                .collect::<Vec<_>>()
                .join(r"[\W_]*");
            (None, vec![], Some(regex::Regex::new(&pattern)?))
        };
        let mut stats = Stats::new();
        for token in boundary_rank::query_tokens(&o.pattern) {
            let normalized = store::normalize(token);
            let v = c.query_row(
                "SELECT n,s FROM term_stats WHERE token=?",
                [&normalized],
                |r| Ok([r.get(0)?, r.get(1)?]),
            );
            if let Ok(v) = v {
                stats.insert(normalized, boundary_rank::StatValue::Pair(v));
            }
        }
        Ok(Self {
            quick_count: o.count_only && !o.count_by_tier,
            partial: false,
            phase: 0,
            terms,
            phrase,
            explicit,
            boundary: boundary_rank::prepare_query(&o.pattern, &stats),
            query_chars: boundary_rank::query_tokens(&o.pattern)
                .iter()
                .map(|t| t.chars().count())
                .sum(),
            required,
        })
    }
    fn evaluate(
        &self,
        text: &str,
        who: &str,
        ts: i64,
        now: i64,
        cutoff: Option<(u8, f64, bool)>,
    ) -> Result<Option<(u8, f64, String)>> {
        let rec = recency(ts, now).max(if who == "user" { 0.35 } else { 0. });
        if let Some(re) = &self.explicit {
            return match re.find(text)? {
                Some(_) => Ok(Some((0, weight(who) * rec.max(0.5), "explicit".into()))),
                None => Ok(None),
            };
        }
        let low = store::normalize(text);
        if self.quick_count {
            let mut matches = self
                .terms
                .iter()
                .map(|variants| variants.iter().any(|v| low.contains(v)));
            let found = if self.partial {
                matches.any(|v| v)
            } else {
                matches.all(|v| v)
            };
            return Ok(found.then(|| (0, 0., "counted".into())));
        }
        let mut occurrences = usize::MAX;
        let mut found = Vec::new();
        for variants in &self.terms {
            let n = variants.iter().map(|v| low.match_indices(v).count()).sum();
            if n == 0 {
                if self.partial {
                    continue;
                } else {
                    return Ok(None);
                }
            }
            found.push(variants[0].clone());
            occurrences = occurrences.min(n);
        }
        if found.is_empty() {
            return Ok(None);
        }
        let phrase = self.phrase.as_ref().unwrap();
        let matches: Vec<_> = phrase.find_iter(&low).collect();
        let is_phrase = !self.partial && !matches.is_empty();
        if (self.phase == 1 && !is_phrase) || (self.phase == 2 && is_phrase) {
            return Ok(None);
        }
        if is_phrase {
            occurrences = matches.len();
        }
        let tier = if self.partial {
            2
        } else {
            u8::from(!is_phrase)
        };
        let upper = if is_phrase {
            (1. - 2f64.powi(-(occurrences.min(60) as i32)))
                * if low.is_ascii() {
                    (self.query_chars as f64
                        / matches
                            .iter()
                            .map(|m| m.end() - m.start())
                            .min()
                            .unwrap_or(1) as f64)
                        .min(1.)
                } else {
                    1.
                }
        } else if self.partial {
            1.
        } else {
            proximity_upper(&low, &self.terms, self.query_chars)
        } * rec
            * weight(who);
        if cutoff.is_some_and(|(t, score, tied)| {
            let ceiling = upper
                * if t == 255 {
                    match tier {
                        1 => 0.7,
                        2 => 0.5,
                        _ => 1.,
                    }
                } else {
                    1.
                };
            (t != 255 && tier > t)
                || ((t == 255 || tier == t) && (ceiling < score || (tied && ceiling == score)))
        }) {
            return Ok(Some((tier, 0., "unscored".into())));
        }
        let b = if self.partial {
            boundary_rank::prepare_query(&found.join(" "), &Stats::new())
                .evaluate(text, None, true)?
        } else {
            self.boundary.evaluate(text, None, true)?
        };
        let span = if is_phrase {
            matches
                .iter()
                .map(|m| low[m.start()..m.end()].chars().count())
                .min()
                .unwrap_or(1)
        } else {
            let spans: Vec<_> = b.spans.iter().flatten().collect();
            let lo = spans.iter().map(|s| s[0]).min().unwrap_or(0);
            let hi = spans.iter().map(|s| s[1]).max().unwrap_or(1);
            (hi - lo).max(1) as usize
        };
        let mut tight = (self.query_chars as f64 / span as f64).min(1.);
        if !is_phrase {
            let quality_scale = b
                .qualities
                .iter()
                .copied()
                .map(|q| (2. * q - 1.).max(0.))
                .fold(1., f64::min);
            tight = 0.5 + 0.5 * tight * quality_scale;
        }
        let repetition = if is_phrase {
            1. - 2f64.powi(-(occurrences.min(60) as i32))
        } else {
            1.
        };
        let score = tight
            * repetition
            * rec
            * weight(who)
            * b.factor
            * if self.partial {
                0.5 * found.len() as f64 / self.terms.len() as f64
            } else {
                1.
            };
        Ok(Some((tier, score, b.match_class.into())))
    }
    // Each alternative keeps its own upper count. Min across terms is a sound occurrence bound.
    fn bound_grams(&self) -> Vec<Vec<Vec<String>>> {
        let ts = if self.explicit.is_some() {
            self.required.iter().map(|s| vec![s.clone()]).collect()
        } else {
            self.terms.clone()
        };
        ts.into_iter()
            .map(|vs| {
                vs.into_iter()
                    .map(|s| {
                        let n = s.chars().count().min(3);
                        let cs: Vec<_> = s.chars().collect();
                        cs.windows(n.max(1)).map(|c| c.iter().collect()).collect()
                    })
                    .collect()
            })
            .collect()
    }
    fn prose_fts(&self, pattern: &str) -> Option<String> {
        if self.phase == 1 {
            let query = store::joined(&store::normalize(pattern));
            return (query.chars().count() >= 3)
                .then(|| format!("joined : \"{}\"", query.replace('"', "\"\"")));
        }
        let groups = if self.explicit.is_some() {
            self.required.iter().map(|v| vec![v.clone()]).collect()
        } else {
            self.terms.clone()
        };
        if groups.is_empty() {
            return None;
        }
        let mut out = vec![];
        for group in groups {
            if group.iter().any(|v| v.chars().count() < 3) {
                // AND may omit a short-term clause and verify it on candidates;
                // OR cannot omit one without losing its possible matches.
                if self.partial {
                    return None;
                }
                continue;
            }
            out.push(format!(
                "({})",
                group
                    .iter()
                    .map(|v| format!("\"{}\"", v.replace('"', "\"\"")))
                    .collect::<Vec<_>>()
                    .join(" OR ")
            ));
        }
        (!out.is_empty()).then(|| {
            format!(
                "text : ({})",
                out.join(if self.partial { " OR " } else { " AND " })
            )
        })
    }
    fn fts(&self) -> Option<String> {
        let groups = self.bound_grams();
        if groups.is_empty() {
            return None;
        }
        let mut out = vec![];
        for vars in groups {
            if vars
                .iter()
                .any(|grams| grams.iter().all(|g| g.chars().count() < 3))
            {
                if self.partial {
                    return None;
                }
                continue;
            }
            let mut alternatives = vec![];
            for grams in vars {
                let terms: Vec<_> = grams
                    .into_iter()
                    .filter(|g| g.chars().count() >= 3)
                    .collect();
                if terms.is_empty() {
                    return None;
                }
                alternatives.push(format!(
                    "({})",
                    terms
                        .iter()
                        .map(|s| format!("\"{}\"", s.replace('"', "\"\"")))
                        .collect::<Vec<_>>()
                        .join(" AND ")
                ));
            }
            out.push(format!("({})", alternatives.join(" OR ")));
        }
        (!out.is_empty()).then(|| out.join(if self.partial { " OR " } else { " AND " }))
    }
}
fn sql_filters(f: &Filters, alias: &str) -> (String, Vec<Value>) {
    let mut clauses = vec![];
    let mut values = vec![];
    if let Some(v) = &f.agent {
        let agents: Vec<_> = v.split(',').collect();
        clauses.push(format!(
            "{alias}.agent IN ({})",
            vec!["?"; agents.len()].join(",")
        ));
        values.extend(agents.into_iter().map(|v| Value::Text(v.into())));
    }
    // add's borrow is not used after the separate agent clause.
    let mut add = |col: &str, op: &str, value: Value| {
        clauses.push(format!("{alias}.{col} {op} ?"));
        values.push(value);
    };
    if let Some(v) = &f.chat {
        add("session", "LIKE", Value::Text(format!("{v}%")));
    }
    if let Some(v) = f.since {
        add("ts", ">=", v.into());
    }
    if let Some(v) = f.until {
        add("ts", "<", v.into());
    }
    if let Some(v) = &f.model {
        add(
            "model",
            if f.model_soft { "LIKE" } else { "=" },
            Value::Text(if f.model_soft {
                format!("%{v}%")
            } else {
                v.clone()
            }),
        );
    }
    if let Some(v) = &f.who {
        let terms: Vec<_> = v.split(',').collect();
        clauses.push(format!(
            "{alias}.who IN ({})",
            vec!["?"; terms.len()].join(",")
        ));
        values.extend(terms.into_iter().map(|v| Value::Text(v.into())));
    }
    if let Some(v) = &f.no_who {
        let terms: Vec<_> = v.split(',').collect();
        clauses.push(format!(
            "{alias}.who NOT IN ({})",
            vec!["?"; terms.len()].join(",")
        ));
        values.extend(terms.into_iter().map(|v| Value::Text(v.into())));
    }
    if f.no_meta {
        clauses.push(format!(
            "{alias}.who NOT IN ('control','harness','synthetic')"
        ));
    }
    if f.no_side {
        clauses.push(format!("NOT EXISTS(SELECT 1 FROM sessions s WHERE s.agent={alias}.agent AND s.id={alias}.session AND s.side=1)"));
    }
    (
        if clauses.is_empty() {
            String::new()
        } else {
            format!(" AND {}", clauses.join(" AND "))
        },
        values,
    )
}
fn project_ok(value: &str, pattern: &str) -> bool {
    let p = pattern.to_lowercase();
    let v = value.to_lowercase();
    if p.contains(['*', '?', '[']) {
        globset::GlobBuilder::new(&p)
            .literal_separator(false)
            .build()
            .is_ok_and(|g| g.compile_matcher().is_match(&v))
    } else {
        v == p || v.rsplit(['/', '\\']).next() == Some(p.as_str())
    }
}
fn compare(a: &Hit, b: &Hit, sort: &str, chats: bool) -> std::cmp::Ordering {
    let score = |h: &Hit| h.score * if chats && h.tier == 1 { 0.7 } else { 1. };
    let order = match sort {
        "time" => b.ts.cmp(&a.ts),
        "position" => a.session.cmp(&b.session).then(a.turn.cmp(&b.turn)),
        _ => {
            if chats {
                score(b).total_cmp(&score(a))
            } else {
                a.tier.cmp(&b.tier).then(b.score.total_cmp(&a.score))
            }
        }
    };
    order
        .then(b.ts.cmp(&a.ts))
        .then(a.session.cmp(&b.session))
        .then(a.turn.cmp(&b.turn))
        .then(a.who.cmp(&b.who))
        .then(a.rowid.cmp(&b.rowid))
}
type BlockBound = (i64, f64, i64, String, i64);
fn block_bounds(
    c: &Connection,
    m: &Matcher,
    now: i64,
    folded: bool,
    table: &str,
) -> Result<Vec<BlockBound>> {
    let (bounds, metadata) = if table == "tools" {
        ("tool_bounds", "tool_blocks")
    } else {
        ("bounds", "blocks")
    };
    let grams = m.bound_grams();
    let mut map: HashMap<String, HashMap<i64, u32>> = HashMap::new();
    for g in grams.iter().flatten().flatten() {
        if map.contains_key(g) {
            continue;
        }
        let mut q = c.prepare_cached(&format!("SELECT block,tf FROM {bounds} WHERE gram=?"))?;
        map.insert(
            g.clone(),
            q.query_map([g], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?,
        );
    }
    let mut q = c.prepare(&format!(
        "SELECT id,max_ts,user_ts,weight,min_session,min_turn FROM {metadata}"
    ))?;
    let rows = q.query_map([], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, Option<i64>>(2)?,
            r.get::<_, f64>(3)?,
            r.get::<_, String>(4)?,
            r.get::<_, i64>(5)?,
        ))
    })?;
    let mut out = vec![];
    for r in rows {
        let (id, ts, user_ts, w, min_session, min_turn) = r?;
        let mut n = u32::MAX;
        for variants in &grams {
            let mut sum = 0u32;
            for terms in variants {
                let count = terms
                    .iter()
                    .map(|g| map.get(g).and_then(|p| p.get(&id)).copied().unwrap_or(0))
                    .min()
                    .unwrap_or(u32::MAX);
                sum = sum.saturating_add(count);
            }
            n = n.min(sum);
        }
        if n == 0 {
            continue;
        }
        // All-terms scores have no repetition penalty. Their finite ceiling
        // must therefore use one, independently of the phrase occurrence cap.
        let repetition = if m.explicit.is_some() || m.phase == 2 {
            1.
        } else {
            1. - 2f64.powi(-(n.min(60) as i32))
        };
        let rw = (w * recency(ts, now)).max(user_ts.map_or(0., |t| recency(t, now).max(0.35)));
        out.push((
            id,
            if folded && m.phase == 2 {
                0.7 * rw
            } else if folded {
                (repetition * rw).max(0.7 * rw)
            } else {
                repetition * rw
            },
            ts,
            min_session,
            min_turn,
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
pub fn search(c: &Connection, o: &Options) -> Result<Results> {
    let optimized = !o.regex && !o.word && o.limit > 0 && !o.exact && o.sort == "score";
    let result = if optimized {
        let mut phrase = search_pass(c, o, false, 1, None)?;
        if !o.chats && phrase.hits.len() >= o.limit {
            // Lower tiers have not been counted, even if the phrase stream ended.
            phrase.total_basis = "floor".into();
            phrase
        } else {
            search_pass(c, o, false, 2, Some(phrase))?
        }
    } else {
        search_pass(c, o, false, 0, None)?
    };
    if result.total == 0
        && o.content
        && !o.regex
        && !o.word
        && boundary_rank::query_tokens(&o.pattern).len() >= 3
    {
        return search_pass(c, o, true, 0, None);
    }
    Ok(result)
}
fn search_pass(
    c: &Connection,
    o: &Options,
    partial: bool,
    phase: u8,
    seed: Option<Results>,
) -> Result<Results> {
    let mut m = Matcher::new(o, c)?;
    m.partial = partial;
    m.phase = phase;
    if partial {
        m.terms.retain(|v| {
            v[0].len() >= 3
                && !matches!(
                    v[0].as_str(),
                    "the"
                        | "how"
                        | "did"
                        | "was"
                        | "were"
                        | "and"
                        | "for"
                        | "that"
                        | "this"
                        | "with"
                        | "what"
                        | "can"
                        | "you"
                )
        });
        if m.terms.is_empty() {
            return Ok(Results {
                total_basis: "exact".into(),
                ..Default::default()
            });
        }
    }
    let now = chrono::Utc::now().timestamp_millis();
    let mut sessions: HashSet<_> = seed
        .as_ref()
        .into_iter()
        .flat_map(|r| r.session_keys.iter().cloned())
        .collect();
    let mut result = seed.unwrap_or_else(|| Results {
        total_basis: "exact".into(),
        ..Default::default()
    });
    result.tools_excluded = !o.tools && o.filters.who.as_deref() != Some("tool");

    let (scope, values) = sql_filters(&o.filters, "p");
    let scope = format!(" AND p.visible=1{scope}");
    let only_tools = o.filters.who.as_deref() == Some("tool");
    let include_tools = o.tools
        && o.filters
            .who
            .as_deref()
            .is_none_or(|who| who.split(',').any(|w| w == "tool"))
        && !o
            .filters
            .no_who
            .as_deref()
            .is_some_and(|who| who.split(',').any(|w| w == "tool"));
    result.tools_excluded = !only_tools && !include_tools;
    let tables = if only_tools {
        vec!["tools"]
    } else if include_tools {
        vec!["prose", "tools"]
    } else {
        vec!["prose"]
    };
    for table in tables {
        let fts = if table == "prose" {
            m.prose_fts(&o.pattern)
        } else {
            m.fts()
        };
        let has_scope = o.filters.agent.is_some()
            || o.filters.chat.is_some()
            || o.filters.since.is_some()
            || o.filters.model.is_some();
        let selective = if has_scope {
            let sql =
                format!("SELECT count(*) FROM (SELECT 1 FROM {table} p WHERE 1{scope} LIMIT 4097)");
            let count: i64 = c.query_row(&sql, params_from_iter(values.clone()), |r| r.get(0))?;
            count <= 4096
        } else {
            false
        };
        let bounded = !partial
            && !selective
            && !o.exact
            && o.limit > 0
            && o.sort == "score"
            && m.explicit.is_none();
        let blocks = if bounded {
            block_bounds(c, &m, now, o.chats, table)?
        } else {
            vec![(-1, f64::INFINITY, i64::MAX, String::new(), 0)]
        };
        for (block, bound, max_ts, min_session, min_turn) in blocks {
            if bounded && result.hits.len() >= o.limit {
                let last = &result.hits[o.limit - 1];
                let ceiling = if m.phase == 2 || o.chats || last.tier == 0 {
                    bound
                } else {
                    f64::INFINITY
                };
                let threshold = last.score
                    * if o.chats {
                        match last.tier {
                            1 => 0.7,
                            2 => 0.5,
                            _ => 1.,
                        }
                    } else {
                        1.
                    };
                if ceiling < threshold
                    || (ceiling == threshold
                        && (max_ts < last.ts
                            || (max_ts == last.ts
                                && (min_session > last.session
                                    || (min_session == last.session && min_turn > last.turn)))))
                {
                    result.total_basis = "floor".into();
                    break;
                }
            }
            result.blocks += 1;
            let mut bind = vec![];
            let from = if !selective && fts.is_some() {
                bind.push(Value::Text(fts.clone().unwrap()));
                format!(
                    "{table}_fts f CROSS JOIN {table} p ON f.rowid=p.id WHERE {table}_fts MATCH ?"
                )
            } else {
                format!("{table} p WHERE 1")
            };
            let mut sql=format!("SELECT p.id,p.session,p.agent,p.project,p.position,p.who,p.model,p.model_source,p.ts,p.text,p.digest,p.extra FROM {from}{scope}");
            bind.extend(values.clone());
            if block >= 0 {
                sql.push_str(if !selective && fts.is_some() {
                    " AND f.rowid>=? AND f.rowid<?"
                } else {
                    " AND p.id>=? AND p.id<?"
                });
                bind.extend([
                    Value::Integer(block * BLOCK),
                    Value::Integer((block + 1) * BLOCK),
                ]);
            }
            let mut stmt = c.prepare(&sql)?;
            let mut rows = stmt.query(params_from_iter(bind))?;
            while let Some(r) = rows.next()? {
                let project: String = r.get(3)?;
                if o.filters
                    .project
                    .as_ref()
                    .is_some_and(|p| !project_ok(&project, p))
                    || o.filters
                        .exclude_project
                        .as_ref()
                        .is_some_and(|p| project_ok(&project, p))
                {
                    continue;
                }
                let text = store::decode(table, r.get(9)?)?;
                result.examined += 1;
                let who: String = r.get(5)?;
                let ts = r.get(8)?;
                let Some((tier, score, boundary)) = m.evaluate(
                    &text,
                    &who,
                    ts,
                    now,
                    if o.sort == "score" && o.limit > 0 && result.hits.len() >= o.limit {
                        let h = &result.hits[o.limit - 1];
                        let session: String = r.get(1)?;
                        Some((
                            if o.chats { 255 } else { h.tier },
                            h.score
                                * if o.chats {
                                    match h.tier {
                                        1 => 0.7,
                                        2 => 0.5,
                                        _ => 1.,
                                    }
                                } else {
                                    1.
                                },
                            ts < h.ts
                                || (ts == h.ts
                                    && (session > h.session
                                        || (session == h.session && r.get::<_, i64>(4)? > h.turn))),
                        ))
                    } else {
                        None
                    },
                )?
                else {
                    continue;
                };
                result.total += 1;
                sessions.insert((r.get::<_, String>(2)?, r.get::<_, String>(1)?));
                if table == "tools" {
                    result.tool_rows += 1;
                }
                if boundary == "unscored" {
                    continue;
                }
                let class = if tier == 1 {
                    "all_terms"
                } else {
                    match boundary.as_str() {
                        "aligned" => "phrase_aligned",
                        "partial" => "phrase_partial",
                        _ => "phrase_interior",
                    }
                };
                if !m.quick_count {
                    *result.tiers.entry(class.into()).or_default() += 1;
                }
                if o.count_only {
                    continue;
                }
                let session: String = r.get(1)?;
                let turn = r.get(4)?;
                let digest = content_digest(&text);
                let extra: String = r.get(11)?;
                let target = session_target(&session)?;
                let handle = if extra.is_empty() {
                    format!("{target}:{turn}.{digest}")
                } else {
                    format!(
                        "{target}:{turn}.{digest}~{}:0-{}",
                        event_id(&session, ts, &text),
                        text.chars().count()
                    )
                };
                let snippet = snippet(&text, &o.pattern, 280);
                let h = Hit {
                    session,
                    agent: r.get(2)?,
                    project,
                    family: String::new(),
                    side: false,
                    turn,
                    who,
                    model: r.get(6)?,
                    model_source: r.get(7)?,
                    ts,
                    text,
                    snippet,
                    score,
                    matched: match tier {
                        0 => "phrase",
                        1 => "all-terms",
                        _ => "content-terms",
                    }
                    .into(),
                    boundary_class: boundary,
                    handle,
                    self_marker: false,
                    rowid: r.get(0)?,
                    tier,
                };
                if o.chats {
                    if let Some(i) = result
                        .hits
                        .iter()
                        .position(|old| old.agent == h.agent && old.session == h.session)
                    {
                        if compare(&h, &result.hits[i], &o.sort, true).is_lt() {
                            result.hits[i] = h;
                        }
                    } else {
                        result.hits.push(h)
                    }
                } else {
                    result.hits.push(h)
                }
                if o.limit > 0 {
                    result.hits.sort_by(|a, b| compare(a, b, &o.sort, o.chats));
                }
                if o.limit > 0 && result.hits.len() > o.limit {
                    result.hits.truncate(o.limit);
                }
            }
        }
    }
    if o.chats {
        result.total = sessions.len();
    }
    result.session_keys = sessions;
    result.hits.sort_by(|a, b| compare(a, b, &o.sort, o.chats));
    result.tiers.insert("total".into(), result.total);
    for h in &mut result.hits {
        let meta = c.query_row(
            "SELECT family,side FROM sessions WHERE agent=?1 AND id=?2",
            [&h.agent, &h.session],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, bool>(1)?)),
        )?;
        h.family = meta.0;
        h.side = meta.1;
    }
    Ok(result)
}
/// The reference codec keeps arbitrary upstream IDs out of shell and handle delimiters.
fn session_target(session: &str) -> Result<String> {
    let bytes = session.as_bytes();
    if (3..=128).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(b))
    {
        return Ok(format!("@{session}"));
    }
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut token = String::new();
    let mut bits = 0u32;
    let mut available = 0;
    for byte in bytes {
        bits = (bits << 8) | u32::from(*byte);
        available += 8;
        while available >= 6 {
            available -= 6;
            token.push(ALPHABET[((bits >> available) & 63) as usize] as char);
        }
    }
    if available != 0 {
        token.push(ALPHABET[((bits << (6 - available)) & 63) as usize] as char);
    }
    if token.is_empty() || token.len() > 1024 {
        bail!("session id cannot be encoded safely")
    }
    Ok(format!("@~{token}"))
}

pub fn content_digest(s: &str) -> String {
    let mut h = 0x2325u16;
    for b in s.bytes() {
        h = (h ^ u16::from(b)).wrapping_mul(0x01b3);
    }
    format!("{h:04x}")
}
fn event_id(session: &str, ts: i64, text: &str) -> String {
    let mut state = blake2b_simd::Params::new()
        .hash_length(12)
        .personal(b"agrep-event-v1")
        .to_state();
    for v in [
        session.as_bytes(),
        ts.to_string().as_bytes(),
        text.as_bytes(),
    ] {
        state.update(&(v.len() as u64).to_le_bytes());
        state.update(v);
    }
    state.finalize().to_hex().to_string()
}
pub fn snippet(text: &str, query: &str, max: usize) -> String {
    let chars: Vec<_> = text.chars().collect();
    let needle = boundary_rank::query_tokens(query)
        .first()
        .copied()
        .unwrap_or(query)
        .to_lowercase();
    let low = text.to_lowercase();
    let at = low
        .find(&needle)
        .map(|p| low[..p].chars().count())
        .unwrap_or(0);
    let start = at.saturating_sub(60).min(chars.len());
    let end = (start + max).min(chars.len());
    format!(
        "{}{}{}",
        if start > 0 { "…" } else { "" },
        chars[start..end]
            .iter()
            .map(|c| if c.is_control() { ' ' } else { *c })
            .collect::<String>(),
        if end < chars.len() { "…" } else { "" }
    )
}

/// Minimal covering span ignores boundary penalties, so it is an admissible score ceiling.
fn proximity_upper(text: &str, terms: &[Vec<String>], query_chars: usize) -> f64 {
    if !text.is_ascii() || terms.len() < 2 {
        return 1.;
    }
    let mut events = Vec::new();
    for (i, variants) in terms.iter().enumerate() {
        for v in variants {
            for (start, _) in text.match_indices(v) {
                events.push((start, start + v.len(), i));
            }
        }
    }
    events.sort_unstable();
    let mut counts = vec![0; terms.len()];
    let mut present = 0;
    let mut left = 0;
    let mut width = usize::MAX;
    for right in 0..events.len() {
        let (_, end, term) = events[right];
        if counts[term] == 0 {
            present += 1;
        }
        counts[term] += 1;
        while present == terms.len() {
            width = width.min(end.saturating_sub(events[left].0).max(1));
            let t = events[left].2;
            counts[t] -= 1;
            if counts[t] == 0 {
                present -= 1;
            }
            left += 1;
        }
    }
    if width == usize::MAX {
        1.
    } else {
        0.5 + 0.5 * (query_chars as f64 / width as f64).min(1.)
    }
}

#[cfg(test)]
mod handle_tests {
    #[test]
    fn opaque_session_tokens_are_urlsafe_utf8_without_padding() {
        assert_eq!(
            super::session_target("valid-session").unwrap(),
            "@valid-session"
        );
        assert_eq!(super::session_target("a:b/é").unwrap(), "@~YTpiL8Op");
        assert!(super::session_target("").is_err());
        assert!(super::session_target(&"x".repeat(800)).is_err());
    }
}
