//! Substring retrieval with admissible, query-specific block bounds.
use crate::{
    boundary_rank::{self, PreparedQuery, Stats},
    store::{self, BLOCK},
};
use anyhow::{bail, Result};
use rusqlite::{params_from_iter, types::Value, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::time::Instant;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CallerFilter {
    pub agent: String,
    pub session: String,
    pub family: String,
    pub exclude_from_turn: Option<i64>,
    pub exclude_family: bool,
    #[serde(default)]
    pub excluded_sessions: Vec<String>,
}
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
    #[serde(default)]
    pub caller: Option<CallerFilter>,
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
fn is_false(value: &bool) -> bool {
    !*value
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
    #[serde(default, rename = "meta", skip_serializing_if = "is_false")]
    pub meta_marker: bool,
    #[serde(skip)]
    pub rowid: i64,
    #[serde(skip)]
    pub tier: u8,
}
#[derive(Default, Serialize, Deserialize, Debug)]
pub struct Results {
    pub pending_sources: usize,
    pub hits: Vec<Hit>,
    pub total: usize,
    pub total_basis: String,
    pub examined: usize,
    pub blocks: usize,
    pub tiers: HashMap<String, usize>,
    pub tool_rows: usize,
    pub tools_excluded: bool,
    #[serde(default)]
    pub timings: Timings,
    #[serde(skip)]
    session_keys: HashSet<(String, String)>,
    #[serde(skip)]
    meta_fallback: Option<Hit>,
}
#[derive(Default, Clone, Serialize, Deserialize, Debug)]
pub struct Timings {
    pub plan_ms: f64,
    pub sqlite_ms: f64,
    pub hydrate_ms: f64,
    pub score_ms: f64,
    pub total_ms: f64,
}
impl Timings {
    fn add(&mut self, other: &Self) {
        self.plan_ms += other.plan_ms;
        self.sqlite_ms += other.sqlite_ms;
        self.hydrate_ms += other.hydrate_ms;
        self.score_ms += other.score_ms;
    }
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
type CandidateGroups = std::sync::Arc<HashMap<i64, Vec<i64>>>;
/// A selective scope's blocks with their row counts, in block order.
type ScopeBlocks = std::sync::Arc<Vec<(i64, usize)>>;
struct CoverageBounds {
    candidates: CandidateGroups,
    rows: HashMap<i64, f64>,
    blocks: HashMap<i64, f64>,
}
/// Every indexed tool word containing one term, with its exact block presence.
struct Expansion {
    words: Vec<crate::tool_index::Word>,
    doc: usize,
    blocks: Vec<u64>,
    by_block: std::sync::OnceLock<HashMap<u32, Vec<u32>>>,
}
impl Expansion {
    fn new(words: Vec<crate::tool_index::Word>) -> Self {
        let mut blocks = Vec::new();
        for word in &words {
            for &block in &word.blocks {
                let (slot, bit) = (block as usize / 64, block % 64);
                if slot >= blocks.len() {
                    blocks.resize(slot + 1, 0u64);
                }
                blocks[slot] |= 1 << bit;
            }
        }
        Self {
            doc: words.iter().map(|w| w.doc).sum(),
            words,
            blocks,
            by_block: std::sync::OnceLock::new(),
        }
    }
    /// Words present in one block, as indexes into `words`.
    fn in_block(&self, block: u32) -> &[u32] {
        self.by_block
            .get_or_init(|| {
                let mut map: HashMap<u32, Vec<u32>> = HashMap::new();
                for (index, word) in self.words.iter().enumerate() {
                    for &block in &word.blocks {
                        map.entry(block).or_default().push(index as u32);
                    }
                }
                map
            })
            .get(&block)
            .map_or(&[], Vec::as_slice)
    }
}
fn has_block(blocks: &[u64], block: i64) -> bool {
    usize::try_from(block)
        .ok()
        .and_then(|block| {
            blocks
                .get(block / 64)
                .map(|bits| bits & (1 << (block % 64)) != 0)
        })
        .unwrap_or(false)
}
#[derive(Default)]
struct PlanCache {
    words: HashMap<String, Option<std::sync::Arc<Expansion>>>,
    trigram_docs: HashMap<String, Option<usize>>,
    postings: HashMap<(i64, String), Vec<store::Posting>>,
    candidates: HashMap<String, CandidateGroups>,
    rejected_candidates: HashSet<String>,
    coverage_idf: Option<Vec<f64>>,
    coverage_all: bool,
    /// Per lane, a selective metadata scope's blocks; None when the scope is
    /// too broad to scan.
    scopes: HashMap<String, Option<ScopeBlocks>>,
}
impl PlanCache {
    fn coverage_bounds(
        &mut self,
        c: &Connection,
        m: &Matcher,
        lane_total: usize,
    ) -> Result<Option<CoverageBounds>> {
        let mut groups = Vec::new();
        for variants in &m.terms {
            let mut estimate = 0usize;
            let mut terms = Vec::new();
            for term in variants {
                if variants
                    .iter()
                    .any(|other| other.len() < term.len() && term.contains(other.as_str()))
                {
                    continue;
                }
                let chars: Vec<_> = term.chars().collect();
                if chars.len() < 3 {
                    estimate = usize::MAX;
                    break;
                }
                let mut df = usize::MAX;
                for gram in chars.windows(3) {
                    let gram: String = gram.iter().collect();
                    let n = c
                        .query_row("SELECT doc FROM prose_terms WHERE term=?", [&gram], |r| {
                            r.get::<_, usize>(0)
                        })
                        .optional()?
                        .unwrap_or(0);
                    df = df.min(n);
                }
                estimate = estimate.saturating_add(df);
                terms.push(format!("\"{}\"", term.replace('"', "\"\"")));
            }
            // Common and short terms remain unknown (possibly present) in the
            // upper bound; never enumerate their corpus-wide postings.
            groups.push(
                if estimate > 65_536 || estimate.saturating_mul(2) > lane_total {
                    None
                } else {
                    self.candidate_rows(
                        c,
                        "prose",
                        &format!("text : ({})", terms.join(" OR ")),
                        None,
                    )?
                },
            );
        }
        let Some(selected) = m.coverage_term.and_then(|i| groups[i].as_ref()) else {
            return Ok(None);
        };
        let contains = |group: &CandidateGroups, block: i64, id: i64| {
            group
                .get(&block)
                .is_some_and(|ids| ids.binary_search(&id).is_ok())
        };
        let total = m.idf.iter().sum::<f64>().max(1.);
        let mut candidates = HashMap::new();
        let mut rows = HashMap::new();
        let mut blocks = HashMap::new();
        for (&block, ids) in selected.iter() {
            for &id in ids {
                if m.coverage_excluded
                    .iter()
                    .any(|&i| groups[i].as_ref().is_some_and(|g| contains(g, block, id)))
                {
                    continue;
                }
                let weight = groups
                    .iter()
                    .enumerate()
                    .filter(|(_, g)| g.as_ref().is_none_or(|g| contains(g, block, id)))
                    .map(|(i, _)| m.idf[i])
                    .sum::<f64>();
                if weight < m.minimum_coverage * total {
                    continue;
                }
                candidates.entry(block).or_insert_with(Vec::new).push(id);
                rows.insert(id, weight);
                let upper = blocks.entry(block).or_insert(0.0_f64);
                *upper = upper.max(weight);
            }
        }
        Ok(Some(CoverageBounds {
            candidates: std::sync::Arc::new(candidates),
            rows,
            blocks,
        }))
    }
    fn candidate_rows(
        &mut self,
        c: &Connection,
        table: &str,
        expression: &str,
        ordinals: Option<&[u8]>,
    ) -> Result<Option<CandidateGroups>> {
        let key = format!("{table}:{ordinals:?}:{expression}");
        if self.rejected_candidates.contains(&key) {
            return Ok(None);
        }
        if let Some(rows) = self.candidates.get(&key) {
            return Ok(Some(rows.clone()));
        }
        let (sql, bind) = if let Some(ordinals) = ordinals.filter(|_| table == "prose") {
            ("SELECT rowid FROM prose_fts WHERE prose_fts MATCH ? AND agrep_word_span(prose_fts,?,2147483647)".into(),vec![Value::Text(expression.into()),Value::Blob(ordinals.to_vec())])
        } else if let Some(ordinals) = ordinals {
            (format!("SELECT f.rowid FROM tools_fts f LEFT JOIN tool_blocks b ON b.id=f.rowid/{BLOCK} LEFT JOIN event_rows e INDEXED BY event_tool_id ON e.tool_id=f.rowid AND e.active=1 LEFT JOIN tool_blobs t ON t.id=e.blob WHERE tools_fts MATCH ? AND agrep_word_span(tools_fts,?,CASE WHEN t.ascii THEN 0 ELSE coalesce(b.max_gap,2147483647) END)"),vec![Value::Text(expression.into()),Value::Blob(ordinals.to_vec())])
        } else {
            (
                format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?"),
                vec![Value::Text(expression.into())],
            )
        };
        let mut by_block: HashMap<i64, Vec<i64>> = HashMap::new();
        let mut query = c.prepare(&sql)?;
        for (retained, row) in query
            .query_map(params_from_iter(bind), |r| r.get::<_, i64>(0))?
            .enumerate()
        {
            let id = row?;
            if retained == 65_536 {
                // This caps a planner allocation, never the matching stream:
                // callers fall back to the complete bounded block cursor.
                self.rejected_candidates.insert(key);
                return Ok(None);
            }
            by_block.entry(id / BLOCK).or_default().push(id);
        }
        for ids in by_block.values_mut() {
            ids.sort_unstable();
        }
        let rows = std::sync::Arc::new(by_block);
        self.candidates.insert(key, rows.clone());
        Ok(Some(rows))
    }
    fn words(&mut self, c: &Connection, term: &str) -> Result<Option<std::sync::Arc<Expansion>>> {
        if !self.words.contains_key(term) {
            let expansion = crate::tool_index::expand(c, term)?
                .map(|words| std::sync::Arc::new(Expansion::new(words)));
            self.words.insert(term.to_owned(), expansion);
        }
        Ok(self.words[term].clone())
    }
    /// A three-character term's word frequency total without expanding it.
    fn trigram_doc(&mut self, c: &Connection, term: &str) -> Result<Option<usize>> {
        if let Some(&doc) = self.trigram_docs.get(term) {
            return Ok(doc);
        }
        let doc = crate::tool_index::trigram_doc(c, term)?;
        self.trigram_docs.insert(term.to_owned(), doc);
        Ok(doc)
    }
    /// The summed tool word frequency of a term; None if it cannot use the dictionary.
    fn tool_doc(&mut self, c: &Connection, term: &str) -> Result<Option<usize>> {
        if let Some(doc) = self.trigram_doc(c, term)? {
            return Ok(Some(doc));
        }
        Ok(self.words(c, term)?.map(|words| words.doc))
    }
}
struct Matcher {
    quick_count: bool,
    partial: bool,
    phase: u8,
    terms: Vec<Vec<String>>,
    ordered_terms: Vec<Vec<String>>,
    estimated: usize,
    phrase: Option<regex::Regex>,
    explicit: Option<fancy_regex::Regex>,
    boundary: PreparedQuery,
    query_chars: usize,
    required: Vec<String>,
    idf: Vec<f64>,
    tool_terms: HashMap<String, std::sync::Arc<Expansion>>,
    /// Variants the tool dictionary can expand; plans expand only those they read.
    tool_supported: HashSet<String>,
    /// Per query term: blocks holding any word of every variant, when all expand.
    tool_presence: Vec<Option<Vec<u64>>>,
    coverage_term: Option<usize>,
    coverage_excluded: HashSet<usize>,
    minimum_coverage: f64,
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
                format!(r"(?<!\w)(?:{})(?!\w)", regex::escape(&o.pattern))
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
            ordered_terms: terms.clone(),
            estimated: usize::MAX,
            terms,
            phrase,
            explicit,
            boundary: boundary_rank::prepare_query(&o.pattern, &stats),
            query_chars: boundary_rank::query_tokens(&o.pattern)
                .iter()
                .map(|t| t.chars().count())
                .sum(),
            required,
            idf: vec![],
            tool_terms: HashMap::new(),
            tool_supported: HashSet::new(),
            tool_presence: Vec::new(),
            coverage_term: None,
            coverage_excluded: HashSet::new(),
            minimum_coverage: 0.,
        })
    }
    fn order_for(&mut self, c: &Connection, table: &str, cache: &mut PlanCache) -> Result<()> {
        let groups = if let Some(i) = self.coverage_term {
            vec![self.terms[i].clone()]
        } else if self.explicit.is_some() {
            self.required.iter().map(|v| vec![v.clone()]).collect()
        } else {
            self.terms.clone()
        };
        self.tool_terms.clear();
        self.tool_supported.clear();
        self.tool_presence.clear();
        if table == "tools" {
            if self.explicit.is_none() {
                self.tool_presence = vec![None; self.terms.len()];
            }
            let mut ordered = vec![];
            let mut estimate = if self.partial { 0 } else { usize::MAX };
            let mut indexed = false;
            for (i, variants) in groups.into_iter().enumerate() {
                let mut df = 0usize;
                let mut expanded = vec![];
                let mut supported = true;
                for v in &variants {
                    // A trigram's block postings already bound where it occurs;
                    // its words are read only if a plan uses them.
                    if let Some(doc) = cache.trigram_doc(c, v)? {
                        df = df.saturating_add(doc);
                    } else if let Some(words) = cache.words(c, v)? {
                        df = df.saturating_add(words.doc);
                        expanded.push((v.clone(), words));
                    } else {
                        supported = false;
                        break;
                    }
                }
                if supported {
                    if self.explicit.is_none() && expanded.len() == variants.len() {
                        let mut blocks: Vec<u64> = Vec::new();
                        for (_, words) in &expanded {
                            if words.blocks.len() > blocks.len() {
                                blocks.resize(words.blocks.len(), 0);
                            }
                            for (slot, bits) in blocks.iter_mut().zip(&words.blocks) {
                                *slot |= bits;
                            }
                        }
                        self.tool_presence[self.coverage_term.unwrap_or(i)] = Some(blocks);
                    }
                    self.tool_supported.extend(variants.iter().cloned());
                    self.tool_terms.extend(expanded);
                    indexed = true;
                    if self.partial {
                        estimate = estimate.saturating_add(df);
                    } else {
                        estimate = estimate.min(df);
                    }
                } else {
                    df = usize::MAX;
                }
                ordered.push((df, i, variants));
            }
            ordered.sort_by_key(|v| (v.0, v.1));
            self.ordered_terms = ordered.into_iter().map(|v| v.2).collect();
            // Disjunctions and counts read every group; other plans read the two rarest.
            let read = if self.partial || self.quick_count {
                usize::MAX
            } else {
                2
            };
            let needed: Vec<String> = self
                .ordered_terms
                .iter()
                .filter(|group| group.iter().all(|v| self.tool_supported.contains(v)))
                .take(read)
                .flatten()
                .filter(|v| !self.tool_terms.contains_key(*v))
                .cloned()
                .collect();
            for v in needed {
                if let Some(words) = cache.words(c, &v)? {
                    self.tool_terms.insert(v, words);
                }
            }
            self.estimated = if indexed { estimate } else { usize::MAX };
            return Ok(());
        }
        let mut cache: HashMap<String, usize> = HashMap::new();
        let mut ordered = vec![];
        let mut estimate = if self.partial { 0 } else { usize::MAX };
        let mut indexed = false;
        for (i, variants) in groups.into_iter().enumerate() {
            let mut df = 0usize;
            if variants.iter().any(|v| v.chars().count() < 3) {
                df = usize::MAX;
            } else {
                for v in &variants {
                    let chars: Vec<_> = v.chars().collect();
                    let mut least = usize::MAX;
                    for gram in chars.windows(3) {
                        let gram: String = gram.iter().collect();
                        let n = if let Some(n) = cache.get(&gram) {
                            *n
                        } else {
                            let n = c
                                .query_row(
                                    "SELECT doc FROM prose_terms WHERE term=?",
                                    [&gram],
                                    |r| r.get::<_, usize>(0),
                                )
                                .optional()?
                                .unwrap_or(0);
                            cache.insert(gram, n);
                            n
                        };
                        least = least.min(n);
                    }
                    df = df.saturating_add(least);
                }
                indexed = true;
                if self.partial {
                    estimate = estimate.saturating_add(df);
                } else {
                    estimate = estimate.min(df);
                }
            }
            ordered.push((df, i, variants));
        }
        ordered.sort_by_key(|v| (v.0, v.1));
        self.ordered_terms = ordered.into_iter().map(|v| v.2).collect();
        self.estimated = if indexed { estimate } else { usize::MAX };
        Ok(())
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
        let mut found = Vec::new();
        let mut found_weight = 0.;
        for (i, variants) in self.terms.iter().enumerate() {
            // Only presence matters here; the phrase tier counts its own matches.
            let present = variants.iter().any(|v| low.contains(v.as_str()));
            if (present && self.coverage_excluded.contains(&i))
                || (!present && self.coverage_term == Some(i))
            {
                return Ok(None);
            }
            if !present {
                if self.partial {
                    continue;
                } else {
                    return Ok(None);
                }
            }
            found_weight += self.idf.get(i).copied().unwrap_or(1.);
            found.push(variants[0].clone());
        }
        if found.is_empty()
            || (self.partial && found_weight < self.minimum_coverage * self.idf.iter().sum::<f64>())
        {
            return Ok(None);
        }
        let phrase = self.phrase.as_ref().unwrap();
        let matches: Vec<_> = phrase.find_iter(&low).collect();
        let is_phrase = !self.partial && !matches.is_empty();
        if (self.phase == 1 && !is_phrase) || (self.phase == 2 && is_phrase) {
            return Ok(None);
        }
        let occurrences = matches.len();
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
            0.5 * found_weight / self.idf.iter().sum::<f64>().max(1.)
        } else if text.is_ascii() {
            proximity_upper(&low, &self.terms, self.query_chars)
        } else {
            // Compatibility characters can expand under normalization. Their
            // original spans may be tighter than the normalized ASCII span.
            1.
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
        if is_phrase {
            let boundary = self.boundary.evaluate_compact(text, None, true)?;
            let span = matches
                .iter()
                .map(|m| low[m.start()..m.end()].chars().count())
                .min()
                .unwrap_or(1);
            let tight = (self.query_chars as f64 / span as f64).min(1.);
            let repetition = 1. - 2f64.powi(-(occurrences.min(60) as i32));
            return Ok(Some((
                0,
                tight * repetition * rec * weight(who) * boundary.factor,
                boundary.match_class.into(),
            )));
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
                0.5 * found_weight / self.idf.iter().sum::<f64>().max(1.)
            } else {
                1.
            };
        Ok(Some((tier, score, b.match_class.into())))
    }
    // On ASCII rows, fully aligned distinct terms occupy disjoint spans with at
    // least one separator. Any partial boundary has quality <= 0.5, making the
    // all-terms tightness 0.5. This bounds both cases without scanning text.
    fn all_terms_tightness(&self) -> f64 {
        if self.terms.len() < 2
            || self
                .terms
                .iter()
                .flatten()
                .any(|s| s.is_empty() || !s.bytes().all(|b| b.is_ascii_alphanumeric()))
            || self.terms.iter().enumerate().any(|(i, variants)| {
                self.terms[..i]
                    .iter()
                    .any(|prior| variants.iter().any(|v| prior.contains(v)))
            })
        {
            return 1.;
        }
        let width: usize = self
            .terms
            .iter()
            .map(|v| v.iter().map(String::len).min().unwrap_or(0))
            .sum();
        0.5 + 0.5 * (self.query_chars as f64 / (width + self.terms.len() - 1) as f64).min(1.)
    }
    // Each alternative keeps its own upper count. Min across terms is a sound occurrence bound.
    fn phrase_bridges(&self) -> Vec<Vec<String>> {
        if self.phase != 1 || self.partial || self.explicit.is_some() {
            return vec![];
        }
        self.terms
            .windows(2)
            .map(|pair| {
                let (left, right) = (&pair[0][0], &pair[1][0]);
                if !left
                    .bytes()
                    .chain(right.bytes())
                    .all(|b| b.is_ascii_alphanumeric())
                {
                    return vec![];
                }
                let a = &left[left.len().saturating_sub(2)..];
                let b = &right[..right.len().min(2)];
                let bridge = format!("{a}{b}");
                let width = bridge.len().min(3);
                bridge
                    .as_bytes()
                    .windows(width)
                    .enumerate()
                    .filter(|(at, _)| *at < a.len() && *at + width > a.len())
                    .map(|(_, bytes)| String::from_utf8(bytes.to_vec()).expect("ASCII bridge"))
                    .collect()
            })
            .collect()
    }
    fn bound_grams(&self) -> Vec<Vec<Vec<String>>> {
        let ts = if self.explicit.is_some() {
            self.required.iter().map(|s| vec![s.clone()]).collect()
        } else if self.phase == 1 && !self.partial {
            // Phrase matching uses the literal normalized tokens. Summing a
            // plural/stem alternative can double-count the same occurrence
            // and recreate an unattainable repetition ceiling.
            self.terms.iter().map(|v| vec![v[0].clone()]).collect()
        } else {
            self.terms.clone()
        };
        ts.into_iter()
            .enumerate()
            .map(|(i, vs)| {
                if self.coverage_excluded.contains(&i) {
                    return vec![];
                }
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
    fn prose_fts(&self, _pattern: &str) -> Option<String> {
        let groups = self.coverage_term.map_or_else(
            || self.ordered_terms.clone(),
            |i| vec![self.terms[i].clone()],
        );
        if groups.is_empty() {
            return None;
        }
        let mut out = vec![];
        for group in groups {
            // A literal phrase cannot use a plural-only variant. In the other
            // tiers a shorter substring already admits every containing variant.
            let terms: Vec<_> = if self.phase == 1 && !self.partial && self.explicit.is_none() {
                group.iter().take(1).collect()
            } else {
                group
                    .iter()
                    .filter(|term| {
                        !group
                            .iter()
                            .any(|other| other.len() < term.len() && term.contains(other.as_str()))
                    })
                    .collect()
            };
            if terms.iter().any(|v| v.chars().count() < 3) {
                // AND may omit a short clause; OR must retain its possible hits.
                if self.partial {
                    return None;
                }
                continue;
            }
            out.push(format!(
                "({})",
                terms
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
    /// Indexed words for one query group. A block keeps only the words its rows contain.
    fn group_words(&self, group: &[String], blocks: Option<&[i64]>) -> Vec<&str> {
        let mut words: Vec<&str> = Vec::new();
        for v in group {
            let expansion = &self.tool_terms[v];
            match blocks {
                None => words.extend(expansion.words.iter().map(|w| w.term.as_str())),
                Some(blocks) => {
                    for &block in blocks {
                        if let Ok(block) = u32::try_from(block) {
                            words.extend(
                                expansion
                                    .in_block(block)
                                    .iter()
                                    .map(|&i| expansion.words[i as usize].term.as_str()),
                            );
                        }
                    }
                }
            }
        }
        words.sort_unstable();
        words.dedup();
        words
    }
    fn fts(&self, blocks: Option<&[i64]>) -> Option<String> {
        let mut clauses = vec![];
        for group in &self.ordered_terms {
            if group.iter().any(|v| !self.tool_supported.contains(v)) {
                if self.partial {
                    return None;
                }
                continue;
            }
            let words = self.group_words(group, blocks);
            if words.is_empty() {
                if self.partial {
                    continue;
                }
                return None;
            }
            clauses.push(format!(
                "({})",
                words
                    .into_iter()
                    .map(|s| format!("\"{}\"", s.replace('"', "\"\"")))
                    .collect::<Vec<_>>()
                    .join(" OR ")
            ));
            if !self.partial && !self.quick_count && clauses.len() == 2 {
                break;
            }
        }
        // Intersect the two rarest required groups once; verify remaining terms
        // on their candidates. This avoids reopening a broad single-term stream
        // for each block in a long all-terms query.
        (!clauses.is_empty()).then(|| clauses.join(if self.partial { " OR " } else { " AND " }))
    }
    fn word_positions(&self, blocks: Option<&[i64]>) -> Option<(String, Vec<u8>)> {
        if self.phase != 1
            || self.partial
            // A selective term in a longer sentence is cheaper to drive once
            // and verify than intersecting every common word-position stream.
            || (self.terms.len() > 2 && self.estimated <= 32_768)
            || self.terms.iter().map(|v| v[0].len()).sum::<usize>() > 4096
            || self.terms.iter().flatten().any(|v| !v.is_ascii())
        {
            return None;
        }
        let mut clauses = vec![];
        let mut ordinals = vec![];
        let mut groups: Vec<_> = self
            .terms
            .iter()
            .enumerate()
            .filter(|(_, group)| group.iter().all(|v| self.tool_supported.contains(v)))
            .collect();
        // A phrase prefilter needs only a necessary positional condition.
        // Intersecting every common word stream makes long questions expensive
        // even when two selective terms already rule out almost all rows.
        // Keep original ordinals so skipped words still contribute distance;
        // canonical text verifies the complete phrase afterwards.
        if groups.len() > 2 {
            groups.sort_by_key(|(i, group)| {
                (
                    self.ordered_terms
                        .iter()
                        .position(|ordered| ordered == *group)
                        .unwrap_or(usize::MAX),
                    *i,
                )
            });
            groups.truncate(2);
            groups.sort_by_key(|(i, _)| *i);
        }
        for (i, group) in groups {
            let words = self.group_words(group, blocks);
            if words.is_empty() {
                return None;
            }
            clauses.push(format!(
                "({})",
                words
                    .iter()
                    .map(|w| format!("\"{}\"", w.replace('"', "\"\"")))
                    .collect::<Vec<_>>()
                    .join(" OR ")
            ));
            for _ in words {
                ordinals.extend((i as u32).to_le_bytes());
            }
        }
        (clauses.len() >= 2).then(|| (clauses.join(" AND "), ordinals))
    }
    fn prose_positions(&self) -> Option<(String, Vec<u8>)> {
        if self.phase != 1 || self.partial || self.explicit.is_some() {
            return None;
        }
        let mut clauses = Vec::new();
        let mut ordinals = Vec::new();
        for (i, terms) in self.terms.iter().enumerate() {
            let term = &terms[0];
            if term.chars().count() >= 3 {
                clauses.push(format!("\"{}\"", term.replace('"', "\"\"")));
                ordinals.extend((i as u32).to_le_bytes());
            }
        }
        (clauses.len() >= 2).then(|| (clauses.join(" AND "), ordinals))
    }
}
fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}
fn sql_filters(f: &Filters, alias: &str, c: &Connection) -> Result<(String, Vec<Value>)> {
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
    if let Some(v) = f.since {
        add("ts", ">=", v.into());
    }
    if let Some(v) = f.until {
        add("ts", "<", v.into());
    }
    if let Some(v) = &f.chat {
        clauses.push(format!("{alias}.session LIKE ? ESCAPE '\\'"));
        values.push(Value::Text(format!("{}%", escape_like(v))));
    }
    if let Some(v) = &f.model {
        clauses.push(if f.model_soft {
            format!("{alias}.model LIKE ? ESCAPE '\\'")
        } else {
            format!("{alias}.model = ? COLLATE NOCASE")
        });
        values.push(Value::Text(if f.model_soft {
            format!("%{}%", escape_like(v))
        } else {
            v.clone()
        }));
    }
    for (pattern, exclude) in [(&f.project, false), (&f.exclude_project, true)] {
        if let Some(pattern) = pattern {
            let projects: Vec<String> = c
                .prepare("SELECT DISTINCT project FROM sessions")?
                .query_map([], |r| r.get(0))?
                .collect::<rusqlite::Result<Vec<String>>>()?
                .into_iter()
                .filter(|p| project_ok(p, pattern))
                .collect();
            if projects.is_empty() {
                if !exclude {
                    clauses.push("0".into())
                }
            } else {
                clauses.push(format!(
                    "{alias}.project {}IN ({})",
                    if exclude { "NOT " } else { "" },
                    vec!["?"; projects.len()].join(",")
                ));
                values.extend(projects.into_iter().map(Value::Text));
            }
        }
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
    if let Some(caller) = &f.caller {
        if !caller.excluded_sessions.is_empty() {
            clauses.push(format!(
                "NOT ({alias}.agent=? AND {alias}.session IN ({}))",
                vec!["?"; caller.excluded_sessions.len()].join(",")
            ));
            values.push(Value::Text(caller.agent.clone()));
            values.extend(caller.excluded_sessions.iter().cloned().map(Value::Text));
        }
        if caller.exclude_family {
            clauses.push(format!("NOT ({alias}.agent=? AND EXISTS(SELECT 1 FROM sessions s WHERE s.agent={alias}.agent AND s.id={alias}.session AND s.family=?))"));
            values.push(Value::Text(caller.agent.clone()));
            values.push(Value::Text(caller.family.clone()));
        } else if let Some(from) = caller.exclude_from_turn {
            clauses.push(format!(
                "NOT ({alias}.agent=? AND {alias}.session=? AND {alias}.position>=?)"
            ));
            values.push(Value::Text(caller.agent.clone()));
            values.push(Value::Text(caller.session.clone()));
            values.push(Value::Integer(from));
        }
    }
    if f.no_side {
        clauses.push(format!("NOT EXISTS(SELECT 1 FROM sessions s WHERE s.agent={alias}.agent AND s.id={alias}.session AND s.side=1)"));
    }
    Ok((
        if clauses.is_empty() {
            String::new()
        } else {
            format!(" AND {}", clauses.join(" AND "))
        },
        values,
    ))
}
/// The same caller predicate used by SQL retrieval, for the semantic lane.
pub fn caller_allows(f: &Filters, h: &Hit) -> bool {
    f.caller.as_ref().is_none_or(|c| {
        h.agent != c.agent
            || !(c.excluded_sessions.contains(&h.session)
                || (c.exclude_family && h.family == c.family)
                || (h.session == c.session && c.exclude_from_turn.is_some_and(|p| h.turn >= p)))
    })
}
pub(crate) fn project_ok(value: &str, pattern: &str) -> bool {
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
type BlockBound = (i64, f64, i64, String, i64, i64);
fn block_bounds(
    c: &Connection,
    m: &Matcher,
    now: i64,
    folded: bool,
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
    let all_terms_tightness = m.all_terms_tightness();
    let mut map: HashMap<String, HashMap<i64, store::Posting>> = HashMap::new();
    for g in grams
        .iter()
        .flatten()
        .flatten()
        .chain(bridges.iter().flatten())
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
                        let upper =
                            if m.explicit.is_some() {
                                weight * recency(posting.max_ts, now).max(0.5)
                            } else {
                                (weight * recency(posting.max_ts, now))
                                    .max(if posting.weight == 6 { 0.35 } else { 0. })
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
        let repetition = if m.explicit.is_some() || m.phase == 2 {
            1.
        } else {
            1. - 2f64.powi(-(n.min(60) as i32))
        };
        // If a cross-token gram is absent, this block cannot contain that
        // pair with zero separator characters. Account for the mandatory gap
        // in the same normalized-character span used by phrase scoring.
        let gaps = bridges
            .iter()
            .filter(|bridge| {
                !bridge.is_empty()
                    && bridge.iter().any(|g| {
                        !map.get(g)
                            .is_some_and(|postings| postings.contains_key(&id))
                    })
            })
            .count();
        let phrase_tightness = if m.phase == 2 && all_ascii {
            all_terms_tightness
        } else if bridges.is_empty() {
            1.
        } else {
            let width: usize = m
                .terms
                .iter()
                .map(|variants| variants[0].chars().count())
                .sum();
            (m.query_chars as f64 / (width + gaps).max(1) as f64).min(1.)
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
                rw * (0.5 * possible_weight / m.idf.iter().sum::<f64>().max(1.))
            } else if folded && m.phase == 2 {
                0.7 * phrase_tightness * rw
            } else if folded {
                (phrase_tightness * repetition * rw).max(0.7 * rw)
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
pub fn search(c: &Connection, o: &Options) -> Result<Results> {
    if o.filters.no_meta && (o.count_only || o.count_by_tier) {
        bail!("--no-meta cannot be combined with counts");
    }
    let started = Instant::now();
    let result = search_unfiltered(c, o)?;
    let mut result = finish_meta(c, result)?;
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
fn save_meta(result: &mut Results, hit: Hit, o: &Options) {
    if result
        .meta_fallback
        .as_ref()
        .is_none_or(|old| compare(&hit, old, &o.sort, o.chats).is_lt())
    {
        result.meta_fallback = Some(hit);
    }
}
fn finish_meta(c: &Connection, mut result: Results) -> Result<Results> {
    if result.hits.is_empty() {
        if let Some(mut hit) = result.meta_fallback.take() {
            (hit.family, hit.side) = c.query_row(
                "SELECT family,side FROM sessions WHERE agent=?1 AND id=?2",
                [&hit.agent, &hit.session],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            let class = match hit.tier {
                1 => "all_terms",
                2 => "content_terms",
                _ => match hit.boundary_class.as_str() {
                    "aligned" => "phrase_aligned",
                    "partial" => "phrase_partial",
                    _ => "phrase_interior",
                },
            };
            result.tiers.insert(class.into(), 1);
            result.tiers.insert("total".into(), 1);
            result.tool_rows = usize::from(hit.who == "tool");
            result.total = 1;
            result.hits.push(hit);
        }
    }
    result.meta_fallback = None;
    Ok(result)
}
fn search_unfiltered(c: &Connection, o: &Options) -> Result<Results> {
    let started = Instant::now();
    if !o.regex && !o.word && boundary_rank::query_tokens(&o.pattern).len() > 1 {
        crate::fts_span::install(c)?;
    }
    let mut normalized = o.clone();
    for text in [
        &mut normalized.filters.agent,
        &mut normalized.filters.who,
        &mut normalized.filters.no_who,
    ]
    .into_iter()
    .flatten()
    {
        *text = text
            .split(',')
            .map(|part| part.trim().to_ascii_lowercase())
            .collect::<Vec<_>>()
            .join(",");
    }
    let o = &normalized;
    let mut cache = PlanCache::default();
    let optimized = !o.regex && !o.word && o.limit > 0 && !o.exact && o.sort == "score";
    let mut result = if optimized {
        let mut phrase = search_pass(c, o, false, 1, None, &mut cache, 0)?;
        if !o.chats && phrase.hits.len() >= o.limit {
            // Lower tiers have not been counted, even if the phrase stream ended.
            phrase.total_basis = "floor".into();
            phrase
        } else {
            search_pass(c, o, false, 2, Some(phrase), &mut cache, 0)?
        }
    } else {
        search_pass(c, o, false, 0, None, &mut cache, 0)?
    };
    if result.total == 0 && o.content && !o.regex && !o.word && automatic_content(&o.pattern) {
        let count = content_terms(&o.pattern).len();
        let mut fallback = None;
        // Disjoint MaxScore partitions: rare terms first, then rows lacking every
        // earlier term. The complement's IDF ceiling can rule out common terms.
        for stage in 0..count {
            fallback = Some(search_pass(c, o, true, 0, fallback, &mut cache, stage)?);
        }
        let mut fallback = fallback.unwrap_or_default();
        fallback.timings.add(&result.timings);
        if let Some(hit) = result.meta_fallback.take() {
            save_meta(&mut fallback, hit, o);
        }
        result = fallback;
    }
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
fn content_terms(pattern: &str) -> Vec<Vec<String>> {
    const STOP:&str="a an and are as at be been but by can could did do does for from had has have how i if in is it its me my not of on or our should so that the their them they this these those to was we were what when where which who why will with would you your whats what's im i'm dont don't isnt isn't didnt didn't";
    boundary_rank::query_tokens(pattern)
        .into_iter()
        .map(|t| {
            t.trim_matches(|c: char| c.is_ascii_punctuation())
                .to_owned()
        })
        .filter(|t| {
            t.chars().count() >= 2 && !STOP.split_whitespace().any(|s| s == store::normalize(t))
        })
        .map(|t| boundary_rank::term_variants(&t))
        .filter(|v| !v.is_empty())
        .collect()
}
fn coverage_terms(pattern: &str) -> Vec<Vec<String>> {
    let mut seen = HashSet::new();
    boundary_rank::query_tokens(pattern)
        .into_iter()
        .map(|t| boundary_rank::term_variants(t.trim_matches(|c: char| c.is_ascii_punctuation())))
        .filter(|v| !v.is_empty() && seen.insert(v[0].clone()))
        .collect()
}
fn automatic_content(pattern: &str) -> bool {
    if !pattern.trim().contains(' ') {
        return false;
    }
    let terms = content_terms(pattern);
    !terms.is_empty()
        && terms.iter().map(|v| v[0].clone()).collect::<Vec<_>>()
            != boundary_rank::query_tokens(pattern)
                .iter()
                .map(|s| store::normalize(s))
                .collect::<Vec<_>>()
}
/// Explicit IDF-weighted recovery. Presentation renders this separate evidence
/// lane and keeps the primary search's exit status and machine framing.
pub fn coverage(c: &Connection, o: &Options) -> Result<Results> {
    if o.regex || o.word || o.count_only || o.count_by_tier || o.sort != "score" {
        bail!("coverage requires a keyword score query without counts");
    }
    let started = Instant::now();
    let count = coverage_terms(&o.pattern).len();
    if count < 2 {
        return Ok(Results {
            total_basis: "exact".into(),
            ..Default::default()
        });
    }
    let mut cache = PlanCache {
        coverage_all: true,
        ..Default::default()
    };
    let mut result = None;
    for stage in 0..count {
        result = Some(search_pass(c, o, true, 0, result, &mut cache, stage)?);
    }
    let mut result = finish_meta(c, result.unwrap_or_default())?;
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
/// Below this many scope rows, decoding a run's rows costs less than reading
/// its postings.
const SCOPE_INDEX_ROWS: usize = 32;
/// The rows of a selective scope that its lane's text index admits, using the
/// expressions unselective plans rely on as necessary conditions. Each run of
/// consecutive scope blocks is one rowid-range query, so a word's postings are
/// opened once per run rather than once per block. Blocks absent from the map
/// are not filtered.
fn scope_candidates(
    c: &Connection,
    m: &Matcher,
    table: &str,
    expression: &str,
    span: Option<&[u8]>,
    scope: &[(i64, usize)],
) -> Result<HashMap<i64, Vec<i64>>> {
    let sql = match (table, span) {
        ("tools", Some(_)) => "SELECT f.rowid FROM tools_fts f LEFT JOIN event_rows e INDEXED BY event_tool_id ON e.tool_id=f.rowid AND e.active=1 LEFT JOIN tool_blobs t ON t.id=e.blob WHERE tools_fts MATCH ?1 AND f.rowid>=?2 AND f.rowid<?3 AND agrep_word_span(tools_fts,?4,CASE WHEN t.ascii THEN 0 ELSE ?5 END)".to_owned(),
        (_, Some(_)) => format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?1 AND rowid>=?2 AND rowid<?3 AND agrep_word_span({table}_fts,?4,?5)"),
        (_, None) => format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?1 AND rowid>=?2 AND rowid<?3"),
    };
    let mut query = c.prepare_cached(&sql)?;
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
        let last = blocks[blocks.len() - 1];
        // Tool blocks hold their own words; a required group without any word
        // in the run admits no row of it.
        let own = if table != "tools" {
            None
        } else {
            let plan = if span.is_some() {
                m.word_positions(Some(&blocks))
            } else {
                m.fts(Some(&blocks))
                    .map(|expression| (expression, Vec::new()))
            };
            let Some(plan) = plan else {
                admitted.extend(blocks.iter().map(|&block| (block, Vec::new())));
                continue;
            };
            Some(plan)
        };
        let (expression, ordinals) = own.as_ref().map_or((expression, span), |(e, o)| {
            (e.as_str(), Some(o.as_slice()))
        });
        let mut bind = vec![
            Value::Text(expression.to_owned()),
            Value::Integer(first * BLOCK),
            Value::Integer((last + 1) * BLOCK),
        ];
        if let Some(ordinals) = ordinals.filter(|_| span.is_some()) {
            // The widest gap of the run's blocks bounds every row in it.
            let max_gap = if table == "tools" {
                c.query_row(
                    "SELECT CASE WHEN count(*)=?3 THEN max(max_gap) ELSE 2147483647 END FROM tool_blocks WHERE id BETWEEN ?1 AND ?2",
                    rusqlite::params![first, last, blocks.len() as i64],
                    |r| r.get(0),
                )?
            } else {
                i64::from(i32::MAX)
            };
            bind.push(Value::Blob(ordinals.to_vec()));
            bind.push(Value::Integer(max_gap));
        }
        for &block in &blocks {
            admitted.insert(block, Vec::new());
        }
        for id in query.query_map(params_from_iter(bind), |r| r.get::<_, i64>(0))? {
            let id = id?;
            if let Some(ids) = admitted.get_mut(&(id / BLOCK)) {
                ids.push(id);
            }
        }
        for block in &blocks {
            if let Some(ids) = admitted.get_mut(block) {
                ids.sort_unstable();
            }
        }
    }
    Ok(admitted)
}
fn count_tool_terms(
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
fn search_pass(
    c: &Connection,
    o: &Options,
    partial: bool,
    phase: u8,
    seed: Option<Results>,
    cache: &mut PlanCache,
    coverage_stage: usize,
) -> Result<Results> {
    let started = Instant::now();
    let mut m = Matcher::new(o, c)?;
    m.partial = partial;
    m.phase = phase;
    if partial {
        m.terms = if cache.coverage_all {
            coverage_terms(&o.pattern)
        } else {
            content_terms(&o.pattern)
        };
        if m.terms.is_empty() {
            return Ok(Results {
                total_basis: "exact".into(),
                ..Default::default()
            });
        }
    }
    let now = chrono::Utc::now().timestamp_millis();
    if partial {
        if let Some(idf) = &cache.coverage_idf {
            m.idf = idf.clone();
        } else {
            let count = store::counts(c)?;
            let n = count["prose"].as_f64().unwrap_or(0.) + count["tools"].as_f64().unwrap_or(0.);
            m.idf = m
                .terms
                .iter()
                .map(|variants| {
                    let df: usize = variants
                        .iter()
                        .map(|v| {
                            let cs: Vec<_> = v.chars().collect();
                            let prose = cs
                                .windows(3)
                                .map(|g| {
                                    let gram: String = g.iter().collect();
                                    c.query_row(
                                        "SELECT doc FROM prose_terms WHERE term=?",
                                        [&gram],
                                        |r| r.get::<_, usize>(0),
                                    )
                                    .unwrap_or(0)
                                })
                                .min()
                                .unwrap_or(n as usize);
                            let tools = if o.tools || o.filters.who.as_deref() == Some("tool") {
                                cache.tool_doc(c, v).ok().flatten().unwrap_or(n as usize)
                            } else {
                                0
                            };
                            prose.saturating_add(tools)
                        })
                        .sum();
                    1. + ((n + 1.) / (df as f64 + 1.)).max(1.).ln()
                })
                .collect();
            cache.coverage_idf = Some(m.idf.clone());
        }
        m.minimum_coverage = if cache.coverage_all { 0.55 } else { 0. };
        let mut order: Vec<_> = (0..m.terms.len()).collect();
        order.sort_by(|&a, &b| m.idf[b].total_cmp(&m.idf[a]).then(a.cmp(&b)));
        m.coverage_term = order.get(coverage_stage).copied();
        m.coverage_excluded
            .extend(order.into_iter().take(coverage_stage));
    }
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

    let (filter_scope, values) = sql_filters(&o.filters, "p", c)?;
    let scope = format!(" AND p.visible=1{filter_scope}");
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
    result.timings.plan_ms += started.elapsed().as_secs_f64() * 1000.;
    for table in tables {
        let plan_started = Instant::now();
        if table == "tools"
            && !o.chats
            && !o.exact
            && o.sort == "score"
            && o.limit > 0
            && result.hits.len() >= o.limit
            && result.hits[o.limit - 1].tier == 0
            && result.hits[o.limit - 1].score > weight("tool")
        {
            result.total_basis = "floor".into();
            result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
            continue;
        }
        m.order_for(c, table, cache)?;
        if m.estimated == 0 {
            result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
            continue;
        }
        if m.quick_count
            && !o.chats
            && m.explicit.is_none()
            && !partial
            && m.terms.iter().flatten().all(|t| t.chars().count() >= 3)
            && (table != "tools"
                || m.terms
                    .iter()
                    .flatten()
                    .all(|t| m.tool_supported.contains(t)))
        {
            let expression = if table == "prose" {
                m.prose_fts(&o.pattern)
            } else {
                m.fts(None)
            };
            if let Some(expression) = expression {
                let f = &o.filters;
                let unfiltered = f.agent.is_none()
                    && f.project.is_none()
                    && f.exclude_project.is_none()
                    && f.model.is_none()
                    && f.who.is_none()
                    && f.no_who.is_none()
                    && f.chat.is_none()
                    && f.since.is_none()
                    && f.until.is_none()
                    && !f.no_side
                    && !f.no_meta
                    && f.caller.is_none();
                if table == "tools" {
                    result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
                    let started = Instant::now();
                    let count = count_tool_terms(c, &m, &scope, &values, unfiltered)?;
                    result.timings.sqlite_ms += started.elapsed().as_secs_f64() * 1000.;
                    result.total += count;
                    result.tool_rows += count;
                    continue;
                }
                let mut bind = vec![Value::Text(expression)];
                let sql = if unfiltered {
                    format!("SELECT count(*) FROM {table}_fts WHERE {table}_fts MATCH ?")
                } else {
                    bind.extend(values.clone());
                    format!("SELECT count(*) FROM {table}_fts f CROSS JOIN {table} p ON p.id=f.rowid WHERE {table}_fts MATCH ?{scope}")
                };
                result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
                let sql_started = Instant::now();
                let count: usize = c.query_row(&sql, params_from_iter(bind), |r| r.get(0))?;
                result.timings.sqlite_ms += sql_started.elapsed().as_secs_f64() * 1000.;
                result.total += count;
                if table == "tools" {
                    result.tool_rows += count;
                }
                continue;
            }
        }
        let lane_total: usize = c
            .query_row(
                "SELECT CAST(value AS INTEGER) FROM state WHERE key=?",
                [format!("{table}_count")],
                |r| r.get(0),
            )
            .optional()?
            .unwrap_or(0);
        let common_scan = !partial
            && !o.regex
            && !o.word
            && !o.exact
            && o.limit > 0
            && o.sort == "score"
            && m.terms.len() == 1
            && m.estimated != usize::MAX
            && m.estimated
                .saturating_mul(if table == "tools" { 8 } else { 4 })
                >= lane_total;
        let span_plan = if table == "tools" && !common_scan {
            m.word_positions(None)
        } else if table == "prose" {
            m.prose_positions()
        } else {
            None
        };
        let fts = if common_scan {
            None
        } else if let Some((expression, _)) = &span_plan {
            Some(expression.clone())
        } else if table == "prose" {
            m.prose_fts(&o.pattern)
        } else {
            m.fts(None)
        };
        let has_scope = o.filters.agent.is_some()
            || o.filters.chat.is_some()
            || o.filters.since.is_some()
            || o.filters.model.is_some()
            || o.filters.project.is_some()
            || o.filters.until.is_some();
        let scope_blocks = if let Some(blocks) = cache.scopes.get(table) {
            blocks.clone()
        } else if has_scope {
            let limit = 4096usize.max((lane_total / 16).min(65_536));
            // Invisible rows may conservatively make a scope look larger, but
            // cannot exclude a result. Leaving visibility out lets ordinary
            // agent/time probes use their covering indexes instead of fetching
            // thousands of prose records past their text payload.
            let sql = format!(
                "SELECT id/{BLOCK},count(*) FROM (SELECT p.id FROM {table} p WHERE 1{filter_scope} LIMIT {}) GROUP BY 1 ORDER BY 1",
                limit + 1
            );
            let blocks = c
                .prepare(&sql)?
                .query_map(params_from_iter(values.clone()), |r| {
                    Ok((r.get::<_, i64>(0)?, r.get::<_, usize>(1)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            let blocks = (blocks.iter().map(|(_, rows)| rows).sum::<usize>() <= limit)
                .then(|| std::sync::Arc::new(blocks));
            cache.scopes.insert(table.into(), blocks.clone());
            blocks
        } else {
            None
        };
        let selective = scope_blocks.is_some();
        let bounded = !selective
            && m.estimated > 128
            && !o.exact
            && o.limit > 0
            && matches!(o.sort.as_str(), "score" | "time");
        let mut coverage = None;
        let mut blocks = if bounded {
            block_bounds(c, &m, now, o.chats, table, cache, coverage.as_ref())?
        } else {
            vec![(
                -1,
                f64::INFINITY,
                i64::MAX,
                String::new(),
                0,
                c.query_row(
                    &format!(
                        "SELECT coalesce(max(max_gap),0) FROM {}",
                        if table == "tools" {
                            "tool_blocks"
                        } else {
                            "blocks"
                        }
                    ),
                    [],
                    |r| r.get(0),
                )?,
            )]
        };
        if bounded && o.sort == "time" {
            blocks.sort_by(|a, b| b.2.cmp(&a.2).then(a.3.cmp(&b.3)).then(a.4.cmp(&b.4)));
        }
        if bounded
            && o.sort == "score"
            && m.phase == 1
            && result.hits.len() >= o.limit
            && result.hits[o.limit - 1].tier == 0
            && blocks
                .first()
                .is_some_and(|b| b.1 < result.hits[o.limit - 1].score)
        {
            result.total_basis = "floor".into();
            result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
            continue;
        }
        result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
        // A small selective posting stream is cheaper to read once than to rebuild
        // the same expanded-word cursor in every block. This changes the access
        // method only: all posting IDs are retained and the same block bounds stop
        // scoring. If a phrase stream exceeds the small ID-buffer allocation,
        // discard that plan and use the complete block cursor instead. This
        // bounds planner memory, not matches or result accuracy.
        let mut adaptive_prose = table == "prose" && bounded && !partial && m.terms.len() > 1;
        let mut ids = None;
        // FTS5 advances every OR child on each step, so one corpus-wide stream
        // of a broad substring expansion costs more than the bounded blocks
        // reading only the words each block holds.
        if table == "tools"
            && bounded
            && m.estimated.saturating_mul(8) <= lane_total
            && fts.as_ref().is_some_and(|f| f.matches('"').count() <= 64)
        {
            if let Some(expression) = &fts {
                let tick = Instant::now();
                ids = cache.candidate_rows(
                    c,
                    table,
                    expression,
                    span_plan.as_ref().map(|(_, v)| v.as_slice()),
                )?;
                result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
            }
        }
        // A selective scope is still scanned in its own order, so the same rows
        // fill the page and meet the same ceilings; the text index only spares
        // decoding rows it proves cannot match.
        let admitted = match (&scope_blocks, &fts) {
            (Some(scope_blocks), Some(expression)) => {
                let tick = Instant::now();
                let admitted = scope_candidates(
                    c,
                    &m,
                    table,
                    expression,
                    span_plan.as_ref().map(|(_, v)| v.as_slice()),
                    scope_blocks,
                )?;
                result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
                Some(admitted)
            }
            _ => None,
        };
        let mut remaining = blocks.into_iter();
        let mut visited = HashSet::new();
        let mut coverage_tried = false;
        while let Some((block, bound, max_ts, min_session, min_turn, max_gap)) = remaining.next() {
            let block_index = visited.len();
            if bounded && partial && table == "prose" && !coverage_tried && block_index >= 4 {
                coverage_tried = true;
                let tick = Instant::now();
                coverage = cache.coverage_bounds(c, &m, lane_total)?;
                if let Some(coverage) = &coverage {
                    let mut replacement =
                        block_bounds(c, &m, now, o.chats, table, cache, Some(coverage))?;
                    replacement.retain(|entry| !visited.contains(&entry.0));
                    remaining = replacement.into_iter();
                    ids = Some(coverage.candidates.clone());
                    result.timings.plan_ms += tick.elapsed().as_secs_f64() * 1000.;
                    continue;
                }
                result.timings.plan_ms += tick.elapsed().as_secs_f64() * 1000.;
            }
            visited.insert(block);
            let (fts, block_span) = if table == "tools"
                && bounded
                && block >= 0
                && !selective
                && ids.is_none()
                && fts.is_some()
            {
                let found = if span_plan.is_some() {
                    m.word_positions(Some(&[block]))
                        .map(|(expression, ordinals)| (Some(expression), Some(ordinals)))
                } else {
                    m.fts(Some(&[block]))
                        .map(|expression| (Some(expression), None))
                };
                // A required group without any word in this block cannot match here.
                let Some(found) = found else { continue };
                found
            } else {
                (fts.clone(), span_plan.as_ref().map(|(_, v)| v.clone()))
            };
            if bounded && result.hits.len() >= o.limit {
                let last = &result.hits[o.limit - 1];
                if o.sort == "time" {
                    if max_ts < last.ts {
                        result.total_basis = "floor".into();
                        break;
                    }
                } else {
                    let ceiling = if m.partial || m.phase == 2 || o.chats || last.tier == 0 {
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
            }
            if adaptive_prose && block_index > 0 {
                adaptive_prose = false;
                if let Some(expression) = &fts {
                    let tick = Instant::now();
                    ids = cache.candidate_rows(
                        c,
                        table,
                        expression,
                        span_plan.as_ref().map(|(_, v)| v.as_slice()),
                    )?;
                    result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
                }
            }
            let block_ids = ids.as_ref().and_then(|map| map.get(&block));
            if ids.is_some() && block_ids.is_none() {
                continue;
            }
            result.blocks += 1;
            let mut bind = vec![];
            let uses_fts = !selective && fts.is_some() && ids.is_none();
            let candidate_table = if table == "tools" && (uses_fts || ids.is_some() || bounded) {
                crate::tool_store::CANDIDATES_BY_ID
            } else {
                table
            };
            let from = if uses_fts {
                bind.push(Value::Text(fts.clone().unwrap()));
                format!(
                    "{table}_fts f CROSS JOIN {candidate_table} p ON f.rowid=p.id WHERE {table}_fts MATCH ?"
                )
            } else if table == "prose" && (ids.is_some() || bounded) {
                // The candidate IDs already establish the selective access path.
                // An agent/time index here can rescan a large scope per block.
                format!("{table} p NOT INDEXED WHERE 1")
            } else {
                format!("{candidate_table} p WHERE 1")
            };
            let mut sql=format!("SELECT p.id,p.session,p.agent,NULL,p.position,p.who,NULL,NULL,p.ts,NULL,NULL,NULL FROM {from}{scope}");
            bind.extend(values.clone());
            if uses_fts {
                if let Some(ordinals) = &block_span {
                    sql.push_str(&if table == "tools" {
                        " AND agrep_word_span(tools_fts,?,CASE WHEN (SELECT ascii FROM tool_blobs WHERE id=p.blob) THEN 0 ELSE ? END)".into()
                    } else {
                        format!(" AND agrep_word_span({table}_fts,?,?)")
                    });
                    bind.push(Value::Blob(ordinals.clone()));
                    bind.push(Value::Integer(if table == "prose" {
                        i64::from(i32::MAX)
                    } else {
                        max_gap
                    }));
                }
            }
            if let Some(ids) = block_ids {
                sql.push_str(&format!(
                    " AND p.id IN ({})",
                    vec!["?"; ids.len()].join(",")
                ));
                bind.extend(ids.iter().copied().map(Value::Integer));
            } else if block >= 0 {
                sql.push_str(if uses_fts {
                    " AND f.rowid>=? AND f.rowid<?"
                } else {
                    " AND p.id>=? AND p.id<?"
                });
                bind.extend([
                    Value::Integer(block * BLOCK),
                    Value::Integer((block + 1) * BLOCK),
                ]);
            }
            if bounded && !o.chats && o.sort == "score" && result.hits.len() >= o.limit {
                let last = &result.hits[o.limit - 1];
                if bound <= last.score {
                    // At the block's best possible score, an older row cannot
                    // win the deterministic tie. Keep equal timestamps for
                    // the session/position tie breakers below.
                    sql.push_str(" AND p.ts>=?");
                    bind.push(Value::Integer(last.ts));
                    result.total_basis = "floor".into();
                }
            }
            let sql_started = Instant::now();
            let mut stmt = c.prepare_cached(&sql)?;
            let mut rows = stmt.query(params_from_iter(bind))?;
            result.timings.sqlite_ms += sql_started.elapsed().as_secs_f64() * 1000.;
            loop {
                let sql_started = Instant::now();
                let row = rows.next()?;
                result.timings.sqlite_ms += sql_started.elapsed().as_secs_f64() * 1000.;
                let Some(r) = row else { break };
                let who: String = r.get(5)?;
                let ts: i64 = r.get(8)?;
                if !o.exact && o.limit > 0 && result.hits.len() >= o.limit && o.sort == "score" {
                    let last = &result.hits[o.limit - 1];
                    let tier = if partial {
                        2
                    } else if phase == 2 {
                        1
                    } else {
                        0
                    };
                    let mut ceiling = weight(&who)
                        * if m.explicit.is_some() {
                            recency(ts, now).max(0.5)
                        } else {
                            recency(ts, now).max(if who == "user" { 0.35 } else { 0. })
                        };
                    if partial {
                        if let Some(coverage) = &coverage {
                            ceiling *= 0.5
                                * coverage
                                    .rows
                                    .get(&r.get::<_, i64>(0)?)
                                    .copied()
                                    .unwrap_or_else(|| m.idf.iter().sum::<f64>())
                                / m.idf.iter().sum::<f64>().max(1.);
                        } else {
                            ceiling *= 0.5;
                        }
                    }
                    if bounded && !o.chats {
                        ceiling = ceiling.min(bound);
                    }
                    if o.chats && tier == 1 {
                        ceiling *= 0.7;
                    }
                    let threshold = last.score * if o.chats && last.tier == 1 { 0.7 } else { 1. };
                    let session: String = r.get(1)?;
                    let turn: i64 = r.get(4)?;
                    let later = ts < last.ts
                        || (ts == last.ts
                            && (session > last.session
                                || (session == last.session && turn > last.turn)));
                    if (!o.chats && tier > last.tier)
                        || ((o.chats || tier == last.tier)
                            && (ceiling < threshold || (ceiling == threshold && later)))
                    {
                        result.total_basis = "floor".into();
                        continue;
                    }
                }
                if let Some(admitted) = &admitted {
                    let id: i64 = r.get(0)?;
                    if admitted
                        .get(&(id / BLOCK))
                        .is_some_and(|ids| ids.binary_search(&id).is_err())
                    {
                        continue;
                    }
                }
                let hydrate_started = Instant::now();
                let bytes: Vec<u8> = c
                    .prepare_cached(&format!("SELECT text FROM {table} WHERE id=?"))?
                    .query_row([r.get::<_, i64>(0)?], |r| r.get(0))?;
                let text = store::decode(table, bytes)?;
                result.timings.hydrate_ms += hydrate_started.elapsed().as_secs_f64() * 1000.;
                result.examined += 1;
                let score_started = Instant::now();
                let evaluated = m.evaluate(
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
                )?;
                result.timings.score_ms += score_started.elapsed().as_secs_f64() * 1000.;
                let Some((tier, score, boundary)) = evaluated else {
                    continue;
                };
                let meta_marker = o.filters.no_meta
                    && crate::history_meta::is_meta(
                        c,
                        &Hit {
                            agent: r.get(2)?,
                            session: r.get(1)?,
                            turn: r.get(4)?,
                            who: who.clone(),
                            rowid: r.get(0)?,
                            ..Default::default()
                        },
                        &o.pattern,
                    )?;
                if !meta_marker {
                    result.total += 1;
                    sessions.insert((r.get::<_, String>(2)?, r.get::<_, String>(1)?));
                    if table == "tools" {
                        result.tool_rows += 1;
                    }
                }
                if boundary == "unscored" {
                    continue;
                }
                let class = if tier == 2 {
                    "content_terms"
                } else if tier == 1 {
                    "all_terms"
                } else {
                    match boundary.as_str() {
                        "aligned" => "phrase_aligned",
                        "partial" => "phrase_partial",
                        _ => "phrase_interior",
                    }
                };
                if !m.quick_count && !meta_marker {
                    *result.tiers.entry(class.into()).or_default() += 1;
                }
                if o.count_only {
                    continue;
                }
                let session: String = r.get(1)?;
                let turn = r.get(4)?;
                if !meta_marker && !o.chats && o.limit > 0 && result.hits.len() >= o.limit {
                    let ranked = Hit {
                        session: session.clone(),
                        turn,
                        who: who.clone(),
                        ts,
                        score,
                        tier,
                        rowid: r.get(0)?,
                        ..Default::default()
                    };
                    if compare(&ranked, &result.hits[o.limit - 1], &o.sort, false).is_ge() {
                        // Matching/counting is already complete. Display joins,
                        // digests and snippets are only needed for page winners.
                        continue;
                    }
                }
                let digest = content_digest(&text);
                // Ranking needs only identity, role and time. Fetch display
                // metadata after matching so losing candidates do not join
                // symbol/blob tables or walk past a large prose BLOB.
                let (project, model, model_source, extra): (String, String, String, String) = c
                    .prepare_cached(&format!(
                        "SELECT project,model,model_source,extra FROM {table} WHERE id=?"
                    ))?
                    .query_row([r.get::<_, i64>(0)?], |r| {
                        Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
                    })?;
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
                let self_marker = o.filters.caller.as_ref().is_some_and(|caller| {
                    caller.agent == r.get::<_, String>(2).unwrap_or_default()
                        && caller.session == session
                });
                let h = Hit {
                    session,
                    agent: r.get(2)?,
                    project,
                    family: String::new(),
                    side: false,
                    turn,
                    who,
                    model,
                    model_source,
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
                    self_marker,
                    meta_marker,
                    rowid: r.get(0)?,
                    tier,
                };
                if meta_marker {
                    save_meta(&mut result, h, o);
                    continue;
                }
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
