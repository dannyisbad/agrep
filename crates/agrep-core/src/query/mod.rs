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
/// History a `--chat` fork child inherited: `session`'s rows up to `until` (ms, inclusive).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Inherited {
    pub session: String,
    pub until: i64,
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
    /// With `chat`, the parent rows that chat inherited (see `crate::forks`).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inherited: Vec<Inherited>,
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
    /// A row's copies in other chats (forks sharing its history), newest first; the row
    /// stands for them on a page.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub forks: Vec<Fork>,
    #[serde(skip)]
    pub rowid: i64,
    #[serde(skip)]
    pub tier: u8,
    /// The characters of `text` 0.4.9 placed the hit's match at (`_match_span`).
    #[serde(skip)]
    pub span: Option<(usize, usize)>,
    /// A tool row's event selector: [`crate::handles::event_id`], or once its page is final
    /// the [`crate::handles::repeat_event_id`] of a row repeating a smaller call id's row.
    #[serde(skip)]
    pub event: Option<String>,
}
impl Hit {
    /// The matching rows a page row stands for: itself and its copies in forks.
    pub fn rows(&self) -> usize {
        1 + self.forks.len()
    }
}
/// Another chat's copy of a row: the same speaker, text and time.
#[derive(Default, Serialize, Deserialize, Clone, Debug)]
pub struct Fork {
    pub session: String,
    pub project: String,
    pub turn: i64,
    pub handle: String,
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
    /// On a page merged with meaning rows, how many trailing hits are keyword rows of a
    /// conversation family a meaning row leads; they follow every other row.
    #[serde(skip)]
    pub hybrid_folded: usize,
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
    pub(crate) fn add(&mut self, other: &Self) {
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
    /// Expansions of the non-ASCII runs anchors take (`PlanCache::run_words`).
    runs: HashMap<String, std::sync::Arc<Expansion>>,
    /// `-w` expansions (`PlanCache::bounded_words`).
    bounded_words: HashMap<String, Option<std::sync::Arc<Expansion>>>,
    /// Tool rows `tools_sym` admits per expression (`PlanCache::symbol_rows`): how many
    /// were read, and all of them unless the read stopped at its cap.
    symbols: HashMap<String, (usize, Option<CandidateGroups>)>,
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
    /// Whether each tool row a page held repeats the time and text of a smaller call id's
    /// row in its turn: its call id if so. A page ranked again reads only its new rows.
    repeated_calls: HashMap<i64, Option<String>>,
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
    /// Per `-E` end, or the `-w` term: folded spellings one of which every match holds.
    required: Vec<Vec<String>>,
    /// The required groups the extractor found as several literals. Like anchors, they
    /// narrow only plans that may skip the rows they rule out: a page reads, and its
    /// bounds and estimate count, only the groups of single literals.
    literal_sets: Vec<Vec<String>>,
    /// For `-w`, the indexed words in which some occurrence of the term has no ASCII
    /// word character beside it: every match lies in one. Plans taking anchors read them.
    word_terms: HashMap<String, std::sync::Arc<Expansion>>,
    idf: Vec<f64>,
    tool_terms: HashMap<String, std::sync::Arc<Expansion>>,
    /// Variants the tool dictionary can expand; plans expand only those they read.
    tool_supported: HashSet<String>,
    /// Per query term: blocks holding any word of every variant, when all expand.
    tool_presence: Vec<Option<Vec<u64>>>,
    /// Each variant of a group the dictionary cannot expand, mapped to an expandable
    /// term every row it matches holds (`Matcher::anchor_group`).
    tool_anchors: HashMap<String, String>,
    /// Whether tool plans may read only the rows anchors admit: no page ceiling can
    /// then mark a total as a floor on a row that cannot match.
    plan_anchors: bool,
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
mod copies;
pub use copies::Copies;
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
use filters::normalize;
use filters::sql_filters;
mod hit;
pub use hit::content_digest;
use hit::name_repeated_calls;
use hit::row_handle;
#[cfg(test)]
use hit::session_target;
pub use hit::snippet;
mod pass;
use pass::search_pass;
use pass::PassContext;
mod plan;
mod quick_count;
mod ranking;
use ranking::compare;
pub use ranking::copy_may_rank_below;
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
