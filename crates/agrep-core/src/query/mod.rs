//! Substring retrieval with admissible, query-specific block bounds.
use crate::{
    boundary_rank::{self, PreparedQuery, Stats},
    pattern::Pattern,
    store::{self, BLOCK},
    window::{self, insensitive_span, Folded, Snip, View},
};
use anyhow::{bail, Result};
use rusqlite::{params_from_iter, types::Value, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::time::Instant;
#[path = "../tool_rank.rs"]
mod tool_rank;
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
#[derive(Clone)]
struct LowercaseTerm {
    lowercase: Vec<String>,
    mask: u8,
    special: bool,
    original: String,
    raw: std::sync::Arc<std::sync::OnceLock<std::result::Result<regex::Regex, regex::Error>>>,
}

#[derive(Clone)]
struct Matcher {
    quick_count: bool,
    partial: bool,
    phase: u8,
    terms: Vec<Vec<String>>,
    ordered_terms: Vec<Vec<String>>,
    lowercase_terms: Vec<LowercaseTerm>,
    dotted_candidates: Vec<String>,
    content_literals: Vec<String>,
    content_patterns: Vec<Option<regex::Regex>>,
    estimated: usize,
    phrase: Option<regex::Regex>,
    /// The phrase with one group per query token, for boundary spans.
    capture: Option<regex::Regex>,
    raw_query: String,
    raw_capture:
        std::sync::Arc<std::sync::OnceLock<std::result::Result<regex::Regex, regex::Error>>>,
    explicit: Option<Pattern>,
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
mod bounds;
use bounds::block_bounds;
mod cache;
mod candidate;
use candidate::Candidate;
use candidate::RowContext;
mod compile;
#[cfg(test)]
use compile::literal_terms;
#[cfg(test)]
use compile::matches_once;
pub use compile::regex_error;
mod content;
use content::automatic_content;
use content::content_anchors;
use content::content_idf;
use content::content_terms;
pub use content::coverage;
use content::RUN_BLOCKS;
use content::RUN_GAP;
use content::SCOPE_INDEX_ROWS;
mod counts;
use counts::admit_run;
use counts::caller_withheld;
use counts::count_scope;
use counts::count_tool_terms;
use counts::scope_candidates;
pub use counts::session_counts;
#[cfg(test)]
use counts::tool_near;
mod evaluate;
mod filters;
pub use filters::caller_allows;
use filters::id_list;
pub(crate) use filters::project_ok;
use filters::sql_filters;
mod hit;
pub use hit::content_digest;
use hit::session_target;
pub use hit::snippet;
mod pass;
use pass::search_pass;
use pass::PassContext;
mod plan;
mod quick_count;
mod ranking;
use ranking::compare;
pub use ranking::meaning_score;
pub use ranking::meta_role;
use ranking::meta_scale;
use ranking::ranked;
pub use ranking::recency;
use ranking::score;
use ranking::speaker;
pub use ranking::weight;
use ranking::BlockBound;
use ranking::CandidateGroups;
use ranking::Cutoff;
use ranking::ScopeBlocks;
use ranking::Scored;
mod row_sql;
use row_sql::BlockRead;
mod search;
pub use search::meta_notice;
pub use search::meta_rows;
pub use search::search;
pub use search::search_with;
pub use search::Plans;
mod table;
use table::TablePlan;
mod traversal;
use traversal::Traversal;
#[cfg(test)]
mod tests;
