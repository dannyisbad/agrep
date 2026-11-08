//! One WAL transaction is the visibility boundary for rows, indexes and fingerprints.
pub use crate::source_token::SourceToken;
use anyhow::Result;
use fts::FtsWrites;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    time::Instant,
};
pub const BLOCK: i64 = 512;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Stamp {
    pub path: String,
    pub agent: String,
    pub size: u64,
    pub mtime_ns: i64,
    pub token: SourceToken,
    pub error: Option<String>,
}
#[derive(Default, Debug, Serialize)]
pub struct Refresh {
    pub sources: usize,
    pub parsed: usize,
    pub changed_rows: usize,
    pub errors: usize,
    pub skipped_lock: bool,
    pub census_ms: f64,
    pub total_ms: f64,
    pub parse_ms: f64,
    pub write_ms: f64,
    pub bounds_ms: f64,
    pub stats_ms: f64,
    pub resumed_sources: usize,
    pub bytes_read: usize,
    pub bytes_parsed: usize,
    pub adapter_parse_ms: BTreeMap<String, f64>,
    pub adapter_source_parse_ms: BTreeMap<String, f64>,
    pub adapter_sources: BTreeMap<String, usize>,
    pub adapter_write_ms: BTreeMap<String, f64>,
    pub row_diff_ms: f64,
    pub fts_ms: f64,
    pub secondary_indexes_ms: f64,
    pub canonical_ms: f64,
    pub dictionary_ms: f64,
    pub node_chunks_ms: f64,
    pub commit_ms: f64,
    /// Opening the writer connection, WAL recovery included.
    pub open_ms: f64,
}
thread_local! {
    static WRITE_TIMES: std::cell::Cell<(u64,u64)> = const { std::cell::Cell::new((0,0)) };
}
struct WriteTimer {
    start: Instant,
    fts: bool,
    before_fts: u64,
}
impl WriteTimer {
    fn new(fts: bool) -> Self {
        Self {
            start: Instant::now(),
            fts,
            before_fts: WRITE_TIMES.get().1,
        }
    }
}
impl Drop for WriteTimer {
    fn drop(&mut self) {
        let elapsed = self.start.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64;
        let (rows, fts) = WRITE_TIMES.get();
        WRITE_TIMES.set(if self.fts {
            (rows, fts.saturating_add(elapsed))
        } else {
            (
                rows.saturating_add(elapsed.saturating_sub(fts.saturating_sub(self.before_fts))),
                fts,
            )
        });
    }
}
#[derive(Default)]
struct Changes {
    /// Prose block ids, tool blocks as `-1 - id`.
    blocks: BTreeSet<i64>,
    bounds: [BTreeSet<i64>; 2],
    // A first build summarizes whole blocks and needs no per-row delta set.
    bulk: bool,
    fts: FtsWrites,
}
impl Changes {
    fn touch(&mut self, table: &str, id: i64) {
        let lane = usize::from(table == "tools");
        if !self.bulk {
            self.bounds[lane].insert(id);
        }
        self.blocks.insert(if lane == 1 {
            -1 - id / BLOCK
        } else {
            id / BLOCK
        });
    }
}
pub fn counts(c: &Connection) -> Result<serde_json::Value> {
    Ok(
        serde_json::json!({"sessions":c.query_row("SELECT count(*) FROM sessions",[],|r|r.get::<_,i64>(0))?,"prose":c.query_row("SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_count'),0)",[],|r|r.get::<_,i64>(0))?,"tools":c.query_row("SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='tools_count'),0)",[],|r|r.get::<_,i64>(0))?,"source_errors":c.query_row("SELECT count(*) FROM sources WHERE last_error IS NOT NULL",[],|r|r.get::<_,i64>(0))?}),
    )
}

mod bounds;
mod connection;
mod families;
mod fts;
mod refresh;
mod replay;
mod rows;
mod sources;
mod text;

pub use bounds::{grams, rebuild_bounds, refresh_phrase_gaps};
pub(crate) use bounds::{
    indexed_gram, posting_list, posting_weight, put_varint, take_varint, tighten_bounds_step,
    Posting,
};
#[cfg(test)]
pub(crate) use connection::schema;
pub use connection::{reader, writer};
pub use refresh::{refresh, refresh_checked, refresh_session, reindex};
pub use sources::census;
pub(crate) use sources::{gone, stamp_owned};
pub use text::{decode, digest, encode_tool, joined, normalize};
pub(crate) use text::{hex_digest, search_fold};
