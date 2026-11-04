//! Substring retrieval with admissible, query-specific block bounds.
use crate::{
    boundary_rank::{self, PreparedQuery, Stats},
    store::{self, BLOCK},
    window::{self, insensitive_span, Folded, Snip, View},
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
/// Rows of exactly one session from one position on, read through the session indexes.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SessionRows {
    pub session: String,
    pub from: Option<i64>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_rows: Option<SessionRows>,
    /// Rows of exactly these sessions, under any agent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sessions: Option<Vec<String>>,
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
    /// 0.4.9's `_semantic_row_weak` for a meaning row: a cosine below the strong band, less
    /// than half the query's terms, or a query no word of which the scope holds.
    #[serde(default, skip_serializing_if = "is_false")]
    pub sem_weak: bool,
    #[serde(skip)]
    pub rowid: i64,
    #[serde(skip)]
    pub tier: u8,
    /// The characters of `text` 0.4.9 placed the hit's match at (`_match_span`).
    #[serde(skip)]
    pub span: Option<(usize, usize)>,
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
    /// Matching rows, which `total` counts too unless it counts chats.
    #[serde(skip)]
    rows: usize,
    /// Proven ~meta rows `--no-meta` dropped from the fetched page, and kept as the only evidence.
    #[serde(default)]
    pub meta_dropped: usize,
    #[serde(default)]
    pub meta_retained: usize,
    /// Counted rows per session id, kept when the scope names its sessions.
    #[serde(skip)]
    session_counts: HashMap<String, usize>,
    /// On a page merged with meaning rows, whether either lane held rows it did not return
    /// (0.4.9's `hybrid_more`); `None` for one lane's result.
    #[serde(skip)]
    pub hybrid_more: Option<bool>,
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
/// The speaker/source prior: 0.4.9's speaker weight times its source scale.
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
/// 0.4.9's `_META_SCALE` (py/search.py:1222): every ranked tier scales a row whose
/// provenance is tooling about history rather than history by this factor.
const META_SCALE: f64 = 0.45;
/// 0.4.9's `_meta_row` from stored provenance (py/search.py:1227-1238): machinery roles
/// and a subagent's first turn, which is its spawned task prompt.
pub fn meta_role(who: &str, turn: i64) -> bool {
    matches!(who, "harness" | "synthetic") || (who == "subagent" && turn == 0)
}
fn meta_scale(meta: bool) -> f64 {
    if meta {
        META_SCALE
    } else {
        1.
    }
}
/// 0.4.9's speaker weight and source scale, which its score multiplies separately.
fn speaker(who: &str) -> (f64, f64) {
    match who {
        "user" => (1., 1.),
        "agent" => (0.8, 1.),
        "subagent" => (0.85, 0.85),
        "tool" => (0.4, 0.55),
        "recap" | "control" => (0.5, 0.65),
        "synthetic" | "harness" => (0.5, 0.6),
        _ => (0.5, 1.),
    }
}
/// A row's score from its match strength, recency, boundary factor and meta scale,
/// multiplied and rounded to four places in 0.4.9's order (py/search.py:1874-1876).
fn score(strength: f64, rec: f64, who: &str, factor: f64, meta: bool) -> f64 {
    let (w, s) = speaker(who);
    crate::window::round4(strength * rec * w * factor * s * meta_scale(meta))
}
pub fn recency(ts: i64, now: i64) -> f64 {
    let days = (now - ts).max(0) as f64 / 86_400_000.;
    0.5f64.powf(days / 14.)
}
/// The score 0.4.9 reported for a meaning row: its keyword formula with a full match
/// (py/search.py:1817-1876, 1938-1942), while the cosine ranks the row.
pub fn meaning_score(h: &Hit, now: i64) -> f64 {
    let rec = recency(h.ts, now).max(if h.who == "user" { 0.35 } else { 0. });
    let (w, s) = speaker(&h.who);
    window::round4(rec * w * s * meta_scale(meta_role(&h.who, h.turn)))
}
/// The ranked (rounded) score a row can reach when its raw score is at most `ceiling`.
/// The margin covers products formed in another order than the score's.
fn ranked(ceiling: f64) -> f64 {
    window::round4(ceiling * (1. + 1e-12))
}
/// Session-head views fold lane confidence into the score.
fn lane(tier: u8) -> f64 {
    match tier {
        1 => 0.7,
        2 => 0.5,
        _ => 1.,
    }
}
/// A row as `Matcher::evaluate` found it.
struct Scored {
    tier: u8,
    score: f64,
    /// The boundary class; `explicit`, `counted`, or `unscored` when the row cannot
    /// enter the page.
    class: &'static str,
    snippet: String,
    /// 0.4.9's match span (`_match_span`), in bytes of the row.
    span: Option<std::ops::Range<usize>>,
}
impl Scored {
    fn bare(tier: u8, class: &'static str) -> Self {
        Self {
            tier,
            score: 0.,
            class,
            snippet: String::new(),
            span: None,
        }
    }
}
/// The page's last row, which another row must outrank to enter a full page.
struct Cutoff {
    chats: bool,
    tier: u8,
    /// Its score, lane-folded for session heads.
    score: f64,
    /// Whether the candidate loses equal scores on time, session and turn.
    later: bool,
}
impl Cutoff {
    /// `last` as the cutoff for a candidate (or a block's best tie key) at `ts`,
    /// `session` and `turn`.
    fn of(last: &Hit, chats: bool, ts: i64, session: &str, turn: i64) -> Self {
        Self {
            chats,
            tier: last.tier,
            score: last.score * if chats { lane(last.tier) } else { 1. },
            later: ts < last.ts
                || (ts == last.ts
                    && (session > last.session.as_str()
                        || (session == last.session && turn > last.turn))),
        }
    }
    /// Whether a row of `tier` whose raw score is at most `ceiling` cannot enter. Rows
    /// rank by rounded score, then (session heads) lane, then time and position.
    fn excludes(&self, tier: u8, ceiling: f64) -> bool {
        if !self.chats && tier != self.tier {
            return tier > self.tier;
        }
        let best = ranked(ceiling) * if self.chats { lane(tier) } else { 1. };
        best < self.score
            || (best == self.score && (tier > self.tier || (tier == self.tier && self.later)))
    }
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
    /// Per lane, a selective metadata scope's blocks; None when the scope is
    /// too broad to scan.
    scopes: HashMap<String, Option<ScopeBlocks>>,
    /// The pattern's matcher before any pass adjusts it, keyed by pattern and mode:
    /// compiling its expressions costs more than a pass over a small corpus.
    matcher: Option<((String, bool, bool), Matcher)>,
}
impl PlanCache {
    fn matcher(&mut self, o: &Options, c: &Connection) -> Result<Matcher> {
        let key = (o.pattern.clone(), o.regex, o.word);
        if self
            .matcher
            .as_ref()
            .is_none_or(|(cached, _)| *cached != key)
        {
            self.matcher = Some((key, Matcher::new(o, c)?));
        }
        let mut m = self.matcher.as_ref().expect("cached above").1.clone();
        m.quick_count = o.count_only && !o.count_by_tier;
        Ok(m)
    }
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
#[derive(Clone)]
struct Matcher {
    quick_count: bool,
    partial: bool,
    phase: u8,
    terms: Vec<Vec<String>>,
    ordered_terms: Vec<Vec<String>>,
    estimated: usize,
    phrase: Option<regex::Regex>,
    /// The phrase with one group per query token, for boundary spans.
    capture: Option<regex::Regex>,
    explicit: Option<fancy_regex::Regex>,
    /// 0.4.9's query length for explicit scores: zero for a regular expression.
    explicit_chars: usize,
    /// Whether the regular expression matches at most once in any snippet (`matches_once`).
    explicit_once: bool,
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
}
/// Literals every match of a regular expression begins or ends with, normalized: the
/// single prefix literal and the single suffix literal, when the expression has them.
fn literal_terms(s: &str) -> Vec<String> {
    use regex_syntax::hir::literal::{ExtractKind, Extractor};
    let Ok(hir) = regex_syntax::ParserBuilder::new().build().parse(s) else {
        return vec![];
    };
    let mut out = Vec::new();
    for kind in [ExtractKind::Prefix, ExtractKind::Suffix] {
        let seq = Extractor::new().kind(kind).extract(&hir);
        let Some([literal]) = seq.literals() else {
            continue;
        };
        let literal = String::from_utf8_lossy(literal.as_bytes());
        if !literal.is_empty() {
            let literal = store::normalize(&literal);
            if !out.contains(&literal) {
                out.push(literal);
            }
        }
    }
    out
}
/// Whether a regular expression matches at most once in a snippet, whose collapsed
/// whitespace leaves no line break: it is `A.*B` with `A` never empty and `.*` greedy and
/// unbounded. The first match's `.*` runs to the last place `B` matches after `A`, and any
/// later match would offer a later one.
fn matches_once(pattern: &str) -> bool {
    use regex_syntax::hir::{Class, HirKind};
    let Ok(mut hir) = regex_syntax::ParserBuilder::new().build().parse(pattern) else {
        return false;
    };
    while let HirKind::Capture(capture) = hir.kind() {
        hir = (*capture.sub).clone();
    }
    let HirKind::Concat(parts) = hir.kind() else {
        return false;
    };
    // Any character but a line break, which the snippet's whitespace runs replace.
    let dot = |class: &Class| match class {
        Class::Unicode(class) => {
            let mut missing = class.clone();
            missing.negate();
            missing
                .ranges()
                .iter()
                .all(|range| (range.start()..=range.end()).all(|c| c == '\n' || c == '\r'))
        }
        Class::Bytes(_) => false,
    };
    parts.iter().enumerate().any(|(at, part)| {
        matches!(part.kind(), HirKind::Repetition(r)
            if r.greedy && r.max.is_none() && matches!(r.sub.kind(), HirKind::Class(c) if dot(c)))
            && parts[..at]
                .iter()
                .any(|before| before.properties().minimum_len().is_some_and(|n| n > 0))
    })
}
/// Why `pattern` is no regular expression, read as written, as 0.4.9 compiled the query
/// before searching (py/search.py:7532-7538). The search compiles it inside a group, which
/// would accept a pattern such as `a)(b`.
pub fn regex_error(pattern: &str) -> Option<String> {
    fancy_regex::Regex::new(pattern)
        .err()
        .map(|e| e.to_string())
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
        let (explicit, required, phrase, capture) = if o.regex || o.word {
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
                None,
            )
        } else {
            let tokens: Vec<String> = boundary_rank::query_tokens(&o.pattern)
                .iter()
                .map(|t| regex::escape(&store::normalize(t)))
                .collect();
            let groups: Vec<String> = tokens.iter().map(|t| format!("({t})")).collect();
            (
                None,
                vec![],
                Some(regex::Regex::new(&tokens.join(r"[\W_]*"))?),
                Some(regex::Regex::new(&groups.join(r"[\W_]*"))?),
            )
        };
        let query_chars = boundary_rank::query_tokens(&o.pattern)
            .iter()
            .map(|t| t.chars().count())
            .sum();
        let mut stats = Stats::new();
        for token in boundary_rank::query_tokens(&o.pattern) {
            let normalized = store::normalize(token);
            let v = c
                .query_row(
                    "SELECT n,s FROM term_stats WHERE token=?",
                    [&normalized],
                    |r| Ok([r.get(0)?, r.get(1)?]),
                )
                .optional()?;
            if let Some(v) = v {
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
            capture,
            explicit,
            explicit_chars: if o.word { query_chars } else { 0 },
            explicit_once: o.regex && matches_once(&o.pattern),
            boundary: boundary_rank::prepare_query(&o.pattern, &stats),
            query_chars,
            required,
            idf: vec![],
            tool_terms: HashMap::new(),
            tool_supported: HashSet::new(),
            tool_presence: Vec::new(),
            coverage_term: None,
            coverage_excluded: HashSet::new(),
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
        turn: i64,
        ts: i64,
        now: i64,
        cutoff: Option<&Cutoff>,
    ) -> Result<Option<Scored>> {
        let rec = recency(ts, now).max(if who == "user" { 0.35 } else { 0. });
        let meta = meta_role(who, turn);
        if let Some(re) = &self.explicit {
            let Some(found) = re.find(text)? else {
                return Ok(None);
            };
            let span = found.start()..found.end();
            let snip = Snip::new(text, std::slice::from_ref(&span));
            let (mut n, mut best) = (0, 0);
            if snip.chars >= self.explicit_chars {
                for m in re.find_iter(&snip.text) {
                    let m = m?;
                    n += 1;
                    let length = snip.text[m.start()..m.end()].chars().count();
                    if best == 0 || length < best {
                        best = length;
                    }
                }
            }
            let strength = window::strength(n, best, self.explicit_chars);
            return Ok(Some(Scored {
                tier: 0,
                score: score(strength, rec.max(0.5), who, 1., meta),
                class: "explicit",
                snippet: snip.text,
                span: Some(span),
            }));
        }
        let row = Folded::new(text);
        let low = &row.low;
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
            return Ok(found.then(|| Scored::bare(0, "counted")));
        }
        let mut found = Vec::new();
        let mut found_weight = 0.;
        for (i, variants) in self.terms.iter().enumerate() {
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
            found.push(i);
        }
        if found.is_empty() {
            return Ok(None);
        }
        let first = if self.partial {
            None
        } else {
            self.phrase.as_ref().unwrap().find(low)
        };
        if (self.phase == 1 && first.is_none()) || (self.phase == 2 && first.is_some()) {
            return Ok(None);
        }
        if let Some(first) = first {
            // 0.4.9 anchors a lone word at its first aligned occurrence and a phrase at
            // its first match.
            let anchor = if self.terms.len() == 1 {
                insensitive_span(&row, &self.terms[0][..1])
            } else {
                None
            }
            .unwrap_or_else(|| row.start(first.start())..row.end(first.end()));
            let snip = Snip::new(text, std::slice::from_ref(&anchor));
            let view = View::new(&row, &snip);
            let strength = self.phrase_strength(&row, &snip, &view);
            let (w, s) = speaker(who);
            // Boundary factors never exceed one, so this is the row's best score.
            if cutoff.is_some_and(|c| c.excludes(0, strength * rec * w * s * meta_scale(meta))) {
                return Ok(Some(Scored::bare(0, "unscored")));
            }
            let (factor, class) = self.phrase_boundary(&row, &snip, &view)?;
            return Ok(Some(Scored {
                tier: 0,
                score: score(strength, rec, who, factor, meta),
                class,
                snippet: snip.text,
                span: Some(anchor),
            }));
        }
        if !self.partial {
            let Some(spans) = self
                .terms
                .iter()
                .map(|variants| insensitive_span(&row, variants))
                .collect::<Option<Vec<_>>>()
            else {
                return Ok(None);
            };
            let snip = Snip::new(text, &spans);
            let view = View::new(&row, &snip);
            let phrase = self.phrase_strength(&row, &snip, &view);
            let graded = boundary_rank::decut_text(&snip.text, true);
            let b = self.boundary.evaluate(&graded, None, true)?;
            let shift = i64::from(snip.text.starts_with(window::CUT));
            let selected: Vec<_> = b
                .spans
                .iter()
                .map(|span| span.map(|[start, end]| [start - shift, end - shift]))
                .collect();
            let near = window::proximity(&snip.text, &selected, &b.qualities, self.query_chars);
            return Ok(Some(Scored {
                tier: 1,
                score: score(phrase.max(near), rec, who, b.factor, meta),
                class: b.match_class,
                span: spans
                    .iter()
                    .min_by_key(|span| (span.start, span.end))
                    .cloned(),
                snippet: snip.text,
            }));
        }
        // The earliest occurrence of a found term anchors the snippet, as 0.4.9's content
        // lanes place theirs; they find the literal term, so a spelling variant counts
        // only for a term found through it alone.
        let anchor = || {
            found
                .iter()
                .filter_map(|&i| {
                    let spellings = &self.terms[i];
                    let at = |v: &String| low.find(v.as_str()).map(|at| (at, at + v.len()));
                    at(&spellings[0]).or_else(|| spellings[1..].iter().filter_map(at).min())
                })
                .min_by_key(|&(at, _)| at)
                .map(|(at, end)| row.start(at)..row.end(end))
        };
        // 0.4.9's content lane (py/corpusdb.py:4897-4908, py/search.py:1852-1876): the
        // row's share of the query's weight, to four places, is its whole match strength,
        // without a boundary factor.
        let total = self.idf.iter().sum::<f64>();
        let coverage = window::round4(found_weight / if total == 0. { 1. } else { total });
        if cutoff.is_some_and(|c| c.excludes(2, coverage * rec * weight(who) * meta_scale(meta))) {
            return Ok(Some(Scored::bare(2, "unscored")));
        }
        let anchor = anchor();
        Ok(Some(Scored {
            tier: 2,
            score: score(coverage, rec, who, 1., meta),
            class: "",
            snippet: Snip::new(text, anchor.as_slice()).text,
            span: anchor,
        }))
    }
    /// The largest coverage factor a partial row reaches when the terms it holds weigh at
    /// most `weight`: rounding is monotonic, so the rounded share bounds every rounded
    /// coverage.
    fn partial_share(&self, weight: f64) -> f64 {
        let total = self.idf.iter().sum::<f64>();
        window::round4(weight / if total == 0. { 1. } else { total })
    }
    /// 0.4.9's phrase strength in a snippet: its matches counted, the tightest measured in
    /// snippet characters.
    fn phrase_strength(&self, row: &Folded, snip: &Snip, view: &View) -> f64 {
        let (mut n, mut best) = (0, 0);
        if snip.chars >= self.query_chars {
            let tokens = self.capture.as_ref().map_or(1, |c| c.captures_len() - 1);
            for m in self.phrase.as_ref().unwrap().find_iter(&view.text) {
                n += 1;
                let mut length = view.span(row, snip, m.range()).len();
                if tokens > 1 && length < self.query_chars + tokens - 1 {
                    // Folding can make a match span fewer original characters than its
                    // folded tokens and separators. Block ceilings count each separator
                    // the folded text holds, so a match is never measured shorter.
                    if let Some(groups) = self
                        .capture
                        .as_ref()
                        .and_then(|c| c.captures_at(&view.text, m.start()))
                    {
                        let gaps = (2..groups.len())
                            .filter(|&i| {
                                groups
                                    .get(i)
                                    .zip(groups.get(i - 1))
                                    .is_some_and(|(next, prior)| next.start() > prior.end())
                            })
                            .count();
                        length = length.max(self.query_chars + gaps);
                    }
                }
                if best == 0 || length < best {
                    best = length;
                }
            }
        }
        window::strength(n, best, self.query_chars)
    }
    /// 0.4.9's phrase boundary factor: graded on the de-cut snippet at each match's token
    /// spans, the best factor, then the shorter match.
    fn phrase_boundary(
        &self,
        row: &Folded,
        snip: &Snip,
        view: &View,
    ) -> Result<(f64, &'static str)> {
        let Some(capture) = self
            .capture
            .as_ref()
            .filter(|c| c.captures_len() - 1 == self.boundary.len())
        else {
            return Ok((1., "aligned"));
        };
        let graded = boundary_rank::decut_text(&snip.text, false);
        let mut best: Option<(f64, usize, &'static str)> = None;
        for groups in capture.captures_iter(&view.text) {
            let spans: Vec<_> = (1..groups.len())
                .map(|i| {
                    groups.get(i).map(|g| {
                        let span = view.span(row, snip, g.range());
                        [span.start as i64, span.end as i64]
                    })
                })
                .collect();
            let length = groups
                .get(0)
                .map_or(0, |m| view.span(row, snip, m.range()).len());
            let value = self
                .boundary
                .evaluate_compact(&graded, Some(&spans), false)?;
            if best.is_none_or(|(factor, shortest, _)| {
                value.factor > factor || (value.factor == factor && length < shortest)
            }) {
                best = Some((value.factor, length, value.match_class));
            }
        }
        Ok(best.map_or((1., "aligned"), |(factor, _, class)| (factor, class)))
    }
    /// On ASCII rows, the all-terms strength ceiling, with the bridges whose adjacency
    /// would break it: each pair of spellings of two terms, one after the other.
    ///
    /// A phrase match in an all-terms snippet crosses a window joint (three separator
    /// characters). The proximity spread covers one selected occurrence of each term;
    /// below one quality, its adjacency bonus is zero. Aligned occurrences of distinct
    /// terms that neither contain nor overlap each other and are not written adjacently
    /// sit at least one character apart. Only a cut marker in the spread, which adds 80,
    /// can bring a de-cut copy of a neighbour into an occurrence; the start pad, which
    /// Python's negative slice does not count, would put the same character twice at a
    /// spelling's start.
    fn all_terms_ceiling(&self) -> Option<(f64, Vec<Vec<String>>)> {
        let count = self.terms.len();
        let spellings = || self.terms.iter().flatten();
        if count < 2
            || spellings().any(|s| {
                s.len() < 3
                    || !s.bytes().all(|b| b.is_ascii_alphanumeric())
                    || s.as_bytes()[0] == s.as_bytes()[1]
            })
        {
            return None;
        }
        let width: usize = self
            .terms
            .iter()
            .map(|v| v.iter().map(String::len).min().unwrap_or(0))
            .sum();
        if width + count - 1 > window::PAD {
            return None;
        }
        let mut bridges = Vec::new();
        for (i, left) in self.terms.iter().enumerate() {
            for (j, right) in self.terms.iter().enumerate() {
                if i == j {
                    continue;
                }
                for (a, b) in left.iter().flat_map(|a| right.iter().map(move |b| (a, b))) {
                    let overlaps = (1..a.len().min(b.len()))
                        .any(|k| a.as_bytes()[a.len() - k..] == b.as_bytes()[..k]);
                    if a.contains(b.as_str()) || overlaps {
                        return None;
                    }
                    let joined = format!("{}{}", &a[a.len() - 2..], &b[..2]);
                    bridges.push(vec![joined[..3].to_owned(), joined[1..].to_owned()]);
                }
            }
        }
        let qlen = self.query_chars as f64;
        let near = 0.5 + 0.5 * (qlen / (width + count - 1) as f64).min(1.);
        Some((near.max(qlen / (qlen + 3.)), bridges))
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
/// Row ids as a JSON array for `json_each`.
fn id_list(ids: &[i64]) -> String {
    use std::fmt::Write;
    let mut out = String::with_capacity(ids.len() * 8 + 2);
    out.push('[');
    for (index, id) in ids.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        let _ = write!(out, "{id}");
    }
    out.push(']');
    out
}
fn sql_filters(f: &Filters, alias: &str, c: &Connection) -> Result<(String, Vec<Value>)> {
    let mut clauses = vec![];
    let mut values = vec![];
    // A session scope holds a few sessions' rows. Without table statistics the planner
    // ranks the agent, time and project indexes above the session indexes and reads
    // every row of the agent; a unary plus keeps those columns off the access path.
    let off = if f.chat.is_some() || f.session_rows.is_some() || f.sessions.is_some() {
        "+"
    } else {
        ""
    };
    if let Some(v) = &f.agent {
        let agents: Vec<_> = v.split(',').collect();
        clauses.push(format!(
            "{off}{alias}.agent IN ({})",
            vec!["?"; agents.len()].join(",")
        ));
        values.extend(agents.into_iter().map(|v| Value::Text(v.into())));
    }
    // add's borrow is not used after the separate agent clause.
    let mut add = |col: &str, op: &str, value: Value| {
        clauses.push(format!("{off}{alias}.{col} {op} ?"));
        values.push(value);
    };
    if let Some(v) = f.since {
        add("ts", ">=", v.into());
    }
    if let Some(v) = f.until {
        add("ts", "<", v.into());
    }
    if let Some(v) = &f.chat {
        // The prefix is matched on the small `sessions` table, which names every row's
        // session, so the rows are then reached through the session indexes; a LIKE
        // on the rows themselves cannot use them and scans the whole lane.
        let mut ids: Vec<String> = c
            .prepare_cached("SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\'")?
            .query_map([format!("{}%", escape_like(v))], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        ids.sort_unstable();
        ids.dedup();
        if ids.is_empty() {
            clauses.push("0".into());
        } else {
            clauses.push(format!(
                "{alias}.session IN (SELECT value FROM json_each(?))"
            ));
            values.push(Value::Text(serde_json::to_string(&ids)?));
        }
    }
    if let Some(rows) = &f.session_rows {
        clauses.push(format!("{alias}.session=?"));
        values.push(Value::Text(rows.session.clone()));
        if let Some(from) = rows.from {
            clauses.push(format!("{alias}.position>=?"));
            values.push(Value::Integer(from));
        }
    }
    if let Some(sessions) = &f.sessions {
        clauses.push(format!(
            "{alias}.session IN (SELECT value FROM json_each(?))"
        ));
        values.push(Value::Text(serde_json::to_string(sessions)?));
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
                    "{off}{alias}.project {}IN ({})",
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
/// 0.4.9's `_rank_key` (rows) and `_session_view_key` (session heads) over rounded
/// scores, then the row id.
fn compare(a: &Hit, b: &Hit, sort: &str, chats: bool) -> std::cmp::Ordering {
    let score = |h: &Hit| h.score * lane(h.tier);
    let order = match sort {
        "time" => b.ts.cmp(&a.ts),
        "position" => a.session.cmp(&b.session).then(a.turn.cmp(&b.turn)),
        _ => {
            if chats {
                score(b).total_cmp(&score(a)).then(a.tier.cmp(&b.tier))
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
fn block_bounds(
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
pub fn search(c: &Connection, o: &Options) -> Result<Results> {
    search_with(c, o, &mut Plans::default())
}
/// Word expansions, posting estimates and term weights of one pattern and lane set,
/// kept across searches of that pattern over different scopes.
#[derive(Default)]
pub struct Plans(PlanCache);
pub fn search_with(c: &Connection, o: &Options, plans: &mut Plans) -> Result<Results> {
    if o.filters.no_meta && (o.count_only || o.count_by_tier) {
        bail!("--no-meta cannot narrow -c/--count-by-tier; machine counts keep grep parity");
    }
    let started = Instant::now();
    plans.0.scopes.clear();
    let mut result = if o.filters.no_meta {
        // 0.4.9 filtered only the page it fetched, at least 40 rows (or chats) deep.
        let mut fetched = o.clone();
        fetched.filters.no_meta = false;
        if o.limit > 0 {
            fetched.limit = o.limit.max(crate::history_meta::ROWS_PER_QUERY);
        }
        let result = search_unfiltered(c, &fetched, &mut plans.0)?;
        drop_meta(c, result, o)?
    } else {
        search_unfiltered(c, o, &mut plans.0)?
    };
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
/// 0.4.9's `_filter_meta_rows`: a proven ~meta row leaves the page unless it is the
/// best of a page holding nothing else; the totals then count only the kept rows.
fn drop_meta(c: &Connection, mut result: Results, o: &Options) -> Result<Results> {
    let fetched = result.hits.len();
    let meta = meta_rows(c, &result.hits, &o.pattern)?;
    let lived = meta.contains(&false);
    let mut kept = Vec::with_capacity(fetched);
    for (mut h, meta) in std::mem::take(&mut result.hits).into_iter().zip(meta) {
        if !meta {
            kept.push(h);
        } else if !lived && result.meta_retained == 0 {
            h.meta_marker = true;
            kept.push(h);
            result.meta_retained = 1;
        } else {
            result.meta_dropped += 1;
        }
    }
    let total = kept.len();
    if o.limit > 0 {
        kept.truncate(o.limit);
    }
    if result.meta_dropped + result.meta_retained > 0 {
        // 0.4.9 compared its matching-row total with the page, also for chat heads.
        let complete = result.total_basis == "exact" && result.rows <= fetched;
        result.total = total;
        result.total_basis = if complete { "exact" } else { "floor" }.into();
        result.tool_rows = kept.iter().filter(|h| h.who == "tool").count();
    }
    result.hits = kept;
    Ok(result)
}
/// Which rows 0.4.9's `--no-meta` proved to be tooling about history: stored harness,
/// synthetic and spawned-task rows, history-reading tool calls, and agent prose within
/// four turns after a history read of `query`.
pub fn meta_rows(c: &Connection, hits: &[Hit], query: &str) -> Result<Vec<bool>> {
    hits.iter()
        .map(|h| crate::history_meta::is_meta(c, h, query))
        .collect()
}
/// 0.4.9's one `--no-meta` disclosure line.
pub fn meta_notice(dropped: usize, retained: usize) -> Option<String> {
    let rows = |n: usize, noun: &str| {
        format!(
            "{} {noun}{}",
            crate::surface::number(n),
            if n == 1 { "" } else { "s" }
        )
    };
    match (dropped, retained) {
        (0, 0) => None,
        (dropped, 0) => Some(format!(
            "excluded {} (--no-meta)",
            rows(dropped, "~meta row")
        )),
        (0, retained) => Some(format!(
            "only ~meta evidence; retained {} (--no-meta)",
            rows(retained, "best row")
        )),
        (dropped, retained) => Some(format!(
            "excluded {}; retained {} (--no-meta)",
            rows(dropped, "~meta row"),
            rows(retained, "sole ~meta row")
        )),
    }
}
fn search_unfiltered(c: &Connection, o: &Options, cache: &mut PlanCache) -> Result<Results> {
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
    let optimized = !o.regex && !o.word && o.limit > 0 && !o.exact && o.sort == "score";
    let mut result = if optimized {
        let mut phrase = search_pass(c, o, false, 1, None, cache, 0)?;
        if !o.chats && phrase.hits.len() >= o.limit {
            // Lower tiers have not been counted, even if the phrase stream ended.
            phrase.total_basis = "floor".into();
            phrase
        } else {
            search_pass(c, o, false, 2, Some(phrase), cache, 0)?
        }
    } else {
        search_pass(c, o, false, 0, None, cache, 0)?
    };
    if result.total == 0 && o.content && !o.regex && !o.word && automatic_content(&o.pattern) {
        let anchors = content_anchors(&content_terms(&o.pattern));
        if anchors > 0 {
            let mut fallback = None;
            // Disjoint MaxScore partitions: the rarest anchor first, then rows lacking
            // every earlier one.
            for stage in 0..anchors {
                fallback = Some(search_pass(c, o, true, 0, fallback, cache, stage)?);
            }
            let mut fallback = fallback.unwrap_or_default();
            fallback.timings.add(&result.timings);
            result = fallback;
        }
    }
    result.timings.total_ms = started.elapsed().as_secs_f64() * 1000.;
    Ok(result)
}
/// 0.4.9's `_content_terms`: the query's tokens without edge punctuation, at least two
/// characters long and not connectives.
fn content_words(pattern: &str) -> Vec<String> {
    const STOP:&str="a an and are as at be been but by can could did do does for from had has have how i if in is it its me my not of on or our should so that the their them they this these those to was we were what when where which who why will with would you your whats what's im i'm dont don't isnt isn't didnt didn't";
    boundary_rank::query_tokens(pattern)
        .into_iter()
        .map(|t| t.trim_matches(|c: char| c.is_ascii_punctuation()))
        .filter(|t| {
            t.chars().count() >= 2 && !STOP.split_whitespace().any(|s| s == store::normalize(t))
        })
        .map(boundary_rank::normalize_token)
        .filter(|t| !t.is_empty())
        .collect()
}
/// The content fallback's terms as 0.4.9's content lane read them (py/corpusdb.py:4871):
/// folded, deduplicated, each matching literally without spelling variants.
fn content_terms(pattern: &str) -> Vec<Vec<String>> {
    let mut seen = HashSet::new();
    content_words(pattern)
        .into_iter()
        .filter(|t| seen.insert(t.clone()))
        .map(|t| vec![t])
        .collect()
}
/// 0.4.9 drew content candidates from the three rarest terms of three or more characters
/// (py/corpusdb.py:4885-4886).
fn content_anchors(terms: &[Vec<String>]) -> usize {
    terms
        .iter()
        .filter(|v| v[0].chars().count() >= 3)
        .count()
        .min(3)
}
fn automatic_content(pattern: &str) -> bool {
    if !pattern.trim().contains(' ') {
        return false;
    }
    let words = content_words(pattern);
    !words.is_empty()
        && words
            != boundary_rank::query_tokens(pattern)
                .iter()
                .map(|s| store::normalize(s))
                .collect::<Vec<_>>()
}
/// 0.4.9's content-lane term weights (py/corpusdb.py:4863-4884): `ln(1 + N / (df + 1))`,
/// where `N` counts the lane's rows and `df` the rows holding the term; a term under three
/// characters counts as in every row. Prose rows are counted exactly through the trigram
/// index; tool rows, when in scope, by their word dictionary's frequency total.
fn content_idf(
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
    for word in raw.iter().map(|w| store::normalize(w)) {
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
    let quote = |w: &str| format!("\"{}\"", w.replace('"', "\"\""));
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
        for w in &long {
            for word in crate::tool_index::expand(c, w)?.unwrap_or_default() {
                expanded.push(quote(&word.term));
            }
        }
        if !expanded.is_empty() {
            lanes.push(("tools", expanded.join(" OR ")));
        }
    }
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
            .map(|w| regex::escape(&store::normalize(w)))
            .collect::<Vec<_>>()
            .join(r"[\W_]*"),
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
            let row = Folded::new(&h.text);
            // Trigram candidates are a superset; the lead is the earliest word the row holds.
            let Some((at, end)) = words
                .iter()
                .filter_map(|w| row.low.find(w.as_str()).map(|at| (at, at + w.len())))
                .min()
            else {
                continue;
            };
            (row.start(at)..row.end(end), echo.is_match(&row.low))
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
const SCOPE_INDEX_ROWS: usize = 32;
/// The most blocks a bounded scan reads its admitted rows for at once. One rowid-range
/// query opens each word's postings once for the run instead of once per block, and
/// larger runs widen the word union faster than they save.
const RUN_BLOCKS: usize = 64;
/// Consecutive blocks a run may step over: ones without a ceiling, already read, or
/// below the page's cutoff.
const RUN_GAP: usize = 8;
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
        admit_run(c, m, table, expression, span, &blocks, None, &mut admitted)?;
    }
    Ok(admitted)
}
/// Adds to `admitted` the rows of `blocks` (sorted) that the text index admits, read with
/// one rowid-range query over their span; other blocks inside it are left out. Tool rows
/// are bounded by `max_gap`, which a consecutive run may leave to its blocks' metadata.
#[allow(clippy::too_many_arguments)]
fn admit_run(
    c: &Connection,
    m: &Matcher,
    table: &str,
    expression: &str,
    span: Option<&[u8]>,
    blocks: &[i64],
    max_gap: Option<i64>,
    admitted: &mut HashMap<i64, Vec<i64>>,
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
    let sql = match (table, span) {
        ("tools", Some(_)) => "SELECT f.rowid FROM tools_fts f LEFT JOIN event_rows e INDEXED BY event_tool_id ON e.tool_id=f.rowid AND e.active=1 LEFT JOIN tool_blobs t ON t.id=e.blob WHERE tools_fts MATCH ?1 AND f.rowid>=?2 AND f.rowid<?3 AND agrep_word_span(tools_fts,?4,CASE WHEN t.ascii THEN 0 ELSE ?5 END)".to_owned(),
        (_, Some(_)) => format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?1 AND rowid>=?2 AND rowid<?3 AND agrep_word_span({table}_fts,?4,?5)"),
        (_, None) => format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?1 AND rowid>=?2 AND rowid<?3"),
    };
    let mut bind = vec![
        Value::Text(expression.to_owned()),
        Value::Integer(first * BLOCK),
        Value::Integer((last + 1) * BLOCK),
    ];
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
    for id in query.query_map(params_from_iter(bind), |r| r.get::<_, i64>(0))? {
        let id = id?;
        if let Some(ids) = rows.get_mut(&(id / BLOCK)) {
            ids.push(id);
        }
    }
    for ids in rows.values_mut() {
        ids.sort_unstable();
    }
    admitted.extend(rows);
    Ok(())
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
/// The matches among a session-shaped scope's rows, which `rows` yields as (id, session)
/// through the session indexes. The text index answers which of them match, one rowid
/// range per run of consecutive blocks holding them, so the work follows the scope's
/// size rather than the pattern's matches across the corpus. Prose matches
/// `expression`; a tool run matches the words its own blocks hold. Each match is
/// tallied under its session in `tally` when one is given.
fn count_scope(
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
fn caller_withheld(
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
    let mut m = cache.matcher(o, c)?;
    m.partial = partial;
    m.phase = phase;
    // The tier this pass's rows take; a pass over both tiers is bounded as phrase rows.
    let pass_tier = if partial { 2 } else { u8::from(phase == 2) };
    if partial {
        m.terms = content_terms(&o.pattern);
        if m.terms.is_empty() {
            return Ok(Results {
                total_basis: "exact".into(),
                ..Default::default()
            });
        }
    }
    let now = chrono::Utc::now().timestamp_millis();
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
    if partial {
        m.idf = match &cache.coverage_idf {
            Some(idf) => idf.clone(),
            None => content_idf(c, &m.terms, !only_tools && !include_tools, cache)?,
        };
        cache.coverage_idf = Some(m.idf.clone());
        let mut order: Vec<_> = (0..m.terms.len())
            .filter(|&i| m.terms[i][0].chars().count() >= 3)
            .collect();
        order.sort_by(|&a, &b| m.idf[b].total_cmp(&m.idf[a]).then(a.cmp(&b)));
        order.truncate(content_anchors(&m.terms));
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
                let narrowed = f.agent.is_some()
                    || f.project.is_some()
                    || f.exclude_project.is_some()
                    || f.model.is_some()
                    || f.who.is_some()
                    || f.no_who.is_some()
                    || f.since.is_some()
                    || f.until.is_some()
                    || f.no_side;
                let sessions = f.chat.is_some() || f.session_rows.is_some() || f.sessions.is_some();
                let prose = (table == "prose").then_some(expression.as_str());
                result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
                let started = Instant::now();
                let count = if sessions {
                    let rows = format!("SELECT p.id,p.session FROM {table} p WHERE 1{scope}");
                    let tally = f.sessions.is_some().then_some(&mut result.session_counts);
                    count_scope(c, &m, table, prose, &rows, &values, tally)?
                } else if narrowed {
                    if table == "tools" {
                        count_tool_terms(c, &m, &scope, &values, false)?
                    } else {
                        let mut bind = vec![Value::Text(expression.clone())];
                        bind.extend(values.clone());
                        c.query_row(&format!("SELECT count(*) FROM {table}_fts f CROSS JOIN {table} p ON p.id=f.rowid WHERE {table}_fts MATCH ?{scope}"), params_from_iter(bind), |r| r.get(0))?
                    }
                } else {
                    // Every match minus those inside the rows the caller withholds.
                    let all = if table == "tools" {
                        count_tool_terms(c, &m, "", &[], true)?
                    } else {
                        c.query_row(
                            &format!("SELECT count(*) FROM {table}_fts WHERE {table}_fts MATCH ?"),
                            [&expression],
                            |r| r.get::<_, usize>(0),
                        )?
                    };
                    match &f.caller {
                        Some(caller) => all - caller_withheld(c, &m, table, prose, caller)?,
                        None => all,
                    }
                };
                result.timings.sqlite_ms += started.elapsed().as_secs_f64() * 1000.;
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
        // A prose term in most rows is cheaper to verify than to admit through the index.
        // Tool rows decode far more slowly, so tool blocks always read admitted rows.
        let common_scan = table == "prose"
            && !partial
            && !o.regex
            && !o.word
            && !o.exact
            && o.limit > 0
            && o.sort == "score"
            && m.terms.len() == 1
            && m.estimated != usize::MAX
            && m.estimated.saturating_mul(4) >= lane_total;
        let span_plan = if table == "tools" {
            m.word_positions(None)
        } else {
            m.prose_positions()
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
            || o.filters.until.is_some()
            || o.filters.session_rows.is_some()
            || o.filters.sessions.is_some();
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
            block_bounds(c, &m, now, table, cache, coverage.as_ref())?
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
                .is_some_and(|b| ranked(b.1) < result.hits[o.limit - 1].score)
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
        let mut order = blocks;
        let mut position: HashMap<i64, usize> = order
            .iter()
            .enumerate()
            .map(|(at, entry)| (entry.0, at))
            .collect();
        let mut next = 0;
        // Rows the text index admits in blocks still to visit, read for whole runs of
        // consecutive blocks at once.
        let mut runs: HashMap<i64, Vec<i64>> = HashMap::new();
        let mut visited = HashSet::new();
        let mut coverage_tried = false;
        while let Some((block, bound, max_ts, min_session, min_turn, max_gap)) =
            order.get(next).cloned()
        {
            next += 1;
            let block_index = visited.len();
            if bounded && partial && table == "prose" && !coverage_tried && block_index >= 4 {
                coverage_tried = true;
                let tick = Instant::now();
                coverage = cache.coverage_bounds(c, &m, lane_total)?;
                if let Some(coverage) = &coverage {
                    let mut replacement = block_bounds(c, &m, now, table, cache, Some(coverage))?;
                    replacement.retain(|entry| !visited.contains(&entry.0));
                    position = replacement
                        .iter()
                        .enumerate()
                        .map(|(at, entry)| (entry.0, at))
                        .collect();
                    (order, next) = (replacement, 0);
                    ids = Some(coverage.candidates.clone());
                    result.timings.plan_ms += tick.elapsed().as_secs_f64() * 1000.;
                    continue;
                }
                result.timings.plan_ms += tick.elapsed().as_secs_f64() * 1000.;
            }
            visited.insert(block);
            if bounded && result.hits.len() >= o.limit {
                let last = &result.hits[o.limit - 1];
                if o.sort == "time" {
                    if max_ts < last.ts {
                        result.total_basis = "floor".into();
                        break;
                    }
                } else if Cutoff::of(last, o.chats, max_ts, &min_session, min_turn)
                    .excludes(pass_tier, bound)
                {
                    result.total_basis = "floor".into();
                    break;
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
            let spanned = bounded && block >= 0 && !selective && ids.is_none() && fts.is_some();
            if spanned && !runs.contains_key(&block) {
                let page = (o.limit > 0 && result.hits.len() >= o.limit)
                    .then(|| &result.hits[o.limit - 1]);
                // Blocks the page still admits will be visited unless its cutoff rises.
                let admits = |entry: &BlockBound| {
                    page.is_none_or(|last| {
                        if o.sort == "time" {
                            entry.2 >= last.ts
                        } else {
                            !Cutoff::of(last, o.chats, entry.2, &entry.3, entry.4)
                                .excludes(pass_tier, entry.1)
                        }
                    })
                };
                // The adaptive prose plan reads one candidate stream after this block.
                let limit = if adaptive_prose { 1 } else { RUN_BLOCKS };
                let mut run = vec![block];
                for step in [1, -1] {
                    let (mut id, mut skipped) = (block + step, 0);
                    while id >= 0 && run.len() < limit && skipped <= RUN_GAP {
                        let member = position.get(&id).is_some_and(|&at| {
                            !visited.contains(&id) && !runs.contains_key(&id) && admits(&order[at])
                        });
                        if member {
                            run.push(id);
                            skipped = 0;
                        } else {
                            skipped += 1;
                        }
                        id += step;
                    }
                }
                run.sort_unstable();
                let max_gap = run
                    .iter()
                    .map(|id| order[position[id]].5)
                    .max()
                    .unwrap_or(max_gap);
                let tick = Instant::now();
                admit_run(
                    c,
                    &m,
                    table,
                    fts.as_deref().unwrap_or_default(),
                    span_plan.as_ref().map(|(_, v)| v.as_slice()),
                    &run,
                    Some(max_gap),
                    &mut runs,
                )?;
                result.timings.sqlite_ms += tick.elapsed().as_secs_f64() * 1000.;
            }
            let block_ids = match &ids {
                Some(map) => map.get(&block),
                None => runs.get(&block),
            };
            if (ids.is_some() || spanned) && block_ids.is_none_or(Vec::is_empty) {
                continue;
            }
            result.blocks += 1;
            let mut bind = vec![];
            let uses_fts = !selective && fts.is_some() && ids.is_none() && !spanned;
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
            // A tool candidate row names its stored text, read without the view's joins.
            let blob = if candidate_table == crate::tool_store::CANDIDATES_BY_ID {
                "p.blob"
            } else {
                "NULL"
            };
            let mut sql=format!("SELECT p.id,p.session,p.agent,{blob},p.position,p.who,NULL,NULL,p.ts,NULL,NULL,NULL FROM {from}{scope}");
            bind.extend(values.clone());
            if uses_fts {
                if let Some((_, ordinals)) = &span_plan {
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
                // One statement text for every list keeps the statement prepared.
                sql.push_str(" AND p.id IN (SELECT value FROM json_each(?))");
                bind.push(Value::Text(id_list(ids)));
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
                if last.tier == pass_tier && ranked(bound) <= last.score {
                    // At the block's best possible score, an older row cannot
                    // win the deterministic tie. Keep equal timestamps for
                    // the session/position tie breakers below.
                    sql.push_str(" AND p.ts>=?");
                    bind.push(Value::Integer(last.ts));
                    result.total_basis = "floor".into();
                }
            }
            if block_ids.is_some() {
                sql.push_str(" ORDER BY p.id");
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
                let turn: i64 = r.get(4)?;
                if !o.exact && o.limit > 0 && result.hits.len() >= o.limit && o.sort == "score" {
                    let mut ceiling = weight(&who)
                        * if m.explicit.is_some() {
                            recency(ts, now).max(0.5)
                        } else {
                            recency(ts, now).max(if who == "user" { 0.35 } else { 0. })
                        }
                        * meta_scale(meta_role(&who, turn));
                    if partial {
                        let id: i64 = r.get(0)?;
                        let total = m.idf.iter().sum::<f64>();
                        let possible = coverage.as_ref().map_or(total, |coverage| {
                            coverage.rows.get(&id).copied().unwrap_or(total)
                        });
                        ceiling *= m.partial_share(possible);
                    }
                    if bounded {
                        ceiling = ceiling.min(bound);
                    }
                    let session: String = r.get(1)?;
                    if Cutoff::of(&result.hits[o.limit - 1], o.chats, ts, &session, turn)
                        .excludes(pass_tier, ceiling)
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
                let bytes: Vec<u8> = match r.get::<_, Option<i64>>(3)? {
                    Some(blob) => c
                        .prepare_cached("SELECT body FROM tool_blobs WHERE id=?")?
                        .query_row([blob], |r| r.get(0))?,
                    None => c
                        .prepare_cached(&format!("SELECT text FROM {table} WHERE id=?"))?
                        .query_row([r.get::<_, i64>(0)?], |r| r.get(0))?,
                };
                let text = store::decode(table, bytes)?;
                result.timings.hydrate_ms += hydrate_started.elapsed().as_secs_f64() * 1000.;
                result.examined += 1;
                let score_started = Instant::now();
                let cutoff = if o.sort == "score" && o.limit > 0 && result.hits.len() >= o.limit {
                    let session: String = r.get(1)?;
                    Some(Cutoff::of(
                        &result.hits[o.limit - 1],
                        o.chats,
                        ts,
                        &session,
                        turn,
                    ))
                } else {
                    None
                };
                let evaluated = m.evaluate(&text, &who, turn, ts, now, cutoff.as_ref())?;
                result.timings.score_ms += score_started.elapsed().as_secs_f64() * 1000.;
                let Some(scored) = evaluated else {
                    continue;
                };
                let (tier, score) = (scored.tier, scored.score);
                result.total += 1;
                result.rows += 1;
                let session: String = r.get(1)?;
                if o.filters.sessions.is_some() {
                    *result.session_counts.entry(session.clone()).or_default() += 1;
                }
                sessions.insert((r.get::<_, String>(2)?, session));
                if table == "tools" {
                    result.tool_rows += 1;
                }
                if scored.class == "unscored" {
                    continue;
                }
                let class = if tier == 2 {
                    "content_terms"
                } else if tier == 1 {
                    "all_terms"
                } else {
                    match scored.class {
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
                if !o.chats && o.limit > 0 && result.hits.len() >= o.limit {
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
                let span = scored.span.map(|span| {
                    (
                        text[..span.start].chars().count(),
                        text[..span.end].chars().count(),
                    )
                });
                let handle = if extra.is_empty() {
                    format!("{target}:{turn}.{digest}")
                } else {
                    let (start, end) = span
                        .filter(|(start, end)| end > start)
                        .unwrap_or((0, text.chars().count()));
                    format!(
                        "{target}:{turn}.{digest}~{}:{start}-{end}",
                        crate::handles::event_id(&session, ts, &text),
                    )
                };
                let self_marker = o.filters.caller.as_ref().is_some_and(|caller| {
                    caller.agent == r.get::<_, String>(2).unwrap_or_default()
                        && caller.session == session
                });
                let meta_marker = meta_role(&who, turn);
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
                    snippet: scored.snippet,
                    score,
                    matched: match tier {
                        0 => "phrase",
                        1 => "all-terms",
                        _ => "content-terms",
                    }
                    .into(),
                    boundary_class: scored.class.into(),
                    handle,
                    self_marker,
                    meta_marker,
                    sem_weak: false,
                    rowid: r.get(0)?,
                    tier,
                    span,
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
/// A meaning match's snippet. 0.4.9 served the matched row's text as it is stored
/// (py/segment_query.py:650, py/search.py:1012), leaving control characters for the
/// sanitizer to quote.
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
        chars[start..end].iter().collect::<String>(),
        if end < chars.len() { "…" } else { "" }
    )
}

#[cfg(test)]
mod snippet_tests {
    #[test]
    fn meaning_snippets_keep_control_characters_for_the_sanitizer() {
        let snippet = super::snippet("alpha \x1b[31mred\x07 beta", "red", 280);
        assert_eq!(snippet, "alpha \x1b[31mred\x07 beta");
        assert_eq!(
            crate::surface::safe(&snippet, false),
            r"alpha \u001b[31mred\u0007 beta"
        );
    }
}
#[cfg(test)]
mod meaning_score_tests {
    use super::{meaning_score, Hit};
    // 0.4.9's `_score` with a full match: recency halving every 14 days, a user floor of
    // 0.35, speaker weight times source scale, and 0.45 for stored meta rows
    // (py/search.py:1218-1222, 1397, 1817-1876). 0.4.9's `recall --json` scored an agent
    // meaning row 18.001 days old 0.3281.
    #[test]
    fn meaning_rows_score_by_the_keyword_formula_as_0_4_9() {
        let day = 86_400_000;
        let now = 1_800_000_000_000;
        let row = |who: &str, turn: i64, age: f64| Hit {
            who: who.into(),
            turn,
            ts: now - (age * day as f64) as i64,
            score: 0.9,
            matched: "semantic".into(),
            ..Hit::default()
        };
        assert_eq!(meaning_score(&row("agent", 1, 14.), now), 0.4);
        assert_eq!(meaning_score(&row("agent", 1, 18.001), now), 0.3281);
        assert_eq!(meaning_score(&row("user", 1, 70.), now), 0.35);
        assert_eq!(meaning_score(&row("tool", 1, 0.), now), 0.22);
        assert_eq!(meaning_score(&row("subagent", 0, 0.), now), 0.3251);
        assert_eq!(meaning_score(&row("harness", 3, 0.), now), 0.135);
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
#[cfg(test)]
mod window_tests {
    use super::*;
    fn evaluate(pattern: &str, text: &str, phase: u8, partial: bool) -> Scored {
        let options = Options {
            pattern: pattern.into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 10,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: false,
            filters: Filters::default(),
        };
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let mut m = Matcher::new(&options, &c).unwrap();
        (m.phase, m.partial) = (phase, partial);
        if partial {
            m.terms = content_terms(pattern);
        }
        m.evaluate(text, "tool", 1, 0, 0, None).unwrap().unwrap()
    }
    // Expected spans were produced by agrep 0.4.9's own matchers: the `[\W_]*` phrase
    // search, `console.insensitive_span` with and without `_keyword_term_specs` variants,
    // and `corpusdb.content`'s earliest lowered occurrence.
    #[test]
    fn hit_spans_follow_0_4_9_match_spans() {
        let text = "bash [failed]: ./run-migration --step 3\nERROR copper falcon migration timeout; copper-falcon_migration";
        let span = |pattern: &str, phase: u8, partial: bool| {
            let span = evaluate(pattern, text, phase, partial).span.unwrap();
            (
                text[..span.start].chars().count(),
                text[..span.end].chars().count(),
            )
        };
        assert_eq!(span("copper falcon migration", 1, false), (46, 69));
        assert_eq!(span("falcon migrations copper", 2, false), (21, 30));
        assert_eq!(span("migration", 1, false), (21, 30));
        assert_eq!(span("timeout copper", 0, true), (46, 52));
    }
}
#[cfg(test)]
mod regex_bound_tests {
    use super::*;
    #[test]
    fn required_literals_come_from_both_ends() {
        assert_eq!(literal_terms("Release.*workflow"), ["release", "workflow"]);
        assert_eq!(literal_terms("release"), ["release"]);
        assert!(literal_terms("release|workflow").is_empty());
        assert!(literal_terms("(?<=a)b").is_empty());
    }
    #[test]
    fn only_a_greedy_dot_run_after_a_nonempty_part_matches_once() {
        for (pattern, once) in [
            ("release.*workflow", true),
            ("(release.*workflow)", true),
            ("release.+", true),
            ("release.*?workflow", false),
            (".*workflow", false),
            ("release.{0,9}workflow", false),
            ("release[^x]*workflow", false),
            ("release.*workflow|cache", false),
            ("(?s)release.*workflow", true),
        ] {
            assert_eq!(matches_once(pattern), once, "{pattern}");
        }
        // Any later match would have offered the first match's `.*` a later end.
        let re = fancy_regex::Regex::new("(?i:release.*workflow)").unwrap();
        let snippet = "release a workflow … release b workflow release c";
        assert_eq!(re.find_iter(snippet).count(), 1);
    }
}
#[cfg(test)]
mod chat_scope_tests {
    use super::*;
    fn plan(c: &Connection, table: &str, filters: &Filters) -> Vec<String> {
        let (scope, values) = sql_filters(filters, "p", c).unwrap();
        c.prepare(&format!(
            "EXPLAIN QUERY PLAN SELECT p.id FROM {table} p WHERE 1{scope}"
        ))
        .unwrap()
        .query_map(params_from_iter(values), |r| r.get(3))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
    }
    // The selective-scope probe, which a LIKE on the rows turned into a full scan, and
    // which an agent or time index outranks without statistics.
    #[test]
    fn session_scopes_read_rows_through_the_session_indexes() {
        let temp = tempfile::tempdir().unwrap();
        let root = crate::data::DataRoot::open(temp.path().join("data"), true).unwrap();
        let c = store::writer(&root).unwrap();
        c.execute_batch(
            "INSERT INTO sessions VALUES('pi','chat-one','p','','','chat-one',0,'',0,0);
             INSERT INTO sessions VALUES('codex','chat-two','p','','','chat-two',0,'',0,0);",
        )
        .unwrap();
        let chat = Filters {
            chat: Some("CHAT-O".into()),
            ..Filters::default()
        };
        assert_eq!(
            sql_filters(&chat, "p", &c).unwrap().1,
            [Value::Text(r#"["chat-one"]"#.into())]
        );
        let narrowed = Filters {
            agent: Some("pi".into()),
            project: Some("p".into()),
            since: Some(1),
            until: Some(2),
            ..chat.clone()
        };
        let rows = Filters {
            chat: None,
            session_rows: Some(SessionRows {
                session: "chat-one".into(),
                from: Some(3),
            }),
            ..narrowed.clone()
        };
        for (filters, tool_rows) in [
            (&chat, "(session_key=?)"),
            (&narrowed, "(session_key=?)"),
            (&rows, "(session_key=? AND position>?)"),
        ] {
            let prose = plan(&c, "prose", filters);
            assert!(
                prose
                    .iter()
                    .any(|step| step.contains("INDEX prose_session (session=?)")),
                "{prose:?}"
            );
            assert!(!prose.iter().any(|step| step.starts_with("SCAN p")));
            let tools = plan(&c, "tools", filters);
            for step in [
                "INDEX event_sessions_session (session=?)".to_owned(),
                format!("INDEX event_position {tool_rows}"),
            ] {
                assert!(tools.iter().any(|s| s.contains(&step)), "{tools:?}");
            }
        }
        let none = Filters {
            chat: Some("chat-one%".into()),
            ..Filters::default()
        };
        assert_eq!(sql_filters(&none, "p", &c).unwrap().0, " AND 0");
    }
}
#[cfg(test)]
mod matcher_cache_tests {
    use super::*;
    // Every pass of a search clones one compiled matcher: a pass adjusts only its copy,
    // a count takes its own counting mode, and another pattern compiles anew.
    #[test]
    fn passes_share_one_compiled_matcher() {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let mut o = Options {
            pattern: "cobalt lantern".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 40,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: true,
            chats: false,
            tools: true,
            filters: Filters::default(),
        };
        let mut cache = PlanCache::default();
        let mut first = cache.matcher(&o, &c).unwrap();
        assert!(!first.quick_count);
        first.terms.clear();
        o.count_only = true;
        let second = cache.matcher(&o, &c).unwrap();
        assert!(second.quick_count);
        assert_eq!(second.terms.len(), 2);
        o.pattern = "deployment".into();
        assert_eq!(cache.matcher(&o, &c).unwrap().terms.len(), 1);
    }
}
#[cfg(test)]
mod term_stats_tests {
    use super::*;
    // A term without statistics ranks with the cold prior; a statistics row that cannot
    // be read fails the search instead of silently ranking as if it were absent.
    #[test]
    fn unreadable_term_statistics_fail_the_matcher() {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let options = Options {
            pattern: "cobalt".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 10,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: false,
            filters: Filters::default(),
        };
        Matcher::new(&options, &c).unwrap();
        c.execute("INSERT INTO term_stats VALUES('cobalt','many',1)", [])
            .unwrap();
        let error = Matcher::new(&options, &c).err().unwrap();
        assert!(
            matches!(
                error.downcast_ref::<rusqlite::Error>(),
                Some(rusqlite::Error::InvalidColumnType(0, ..))
            ),
            "{error:#}"
        );
    }
}
