//! Aggregate-only retrieval phase profiling for an isolated index.
use agrep_core::{
    data::DataRoot,
    query::{self, Filters, Options},
    store,
};
use anyhow::Result;
use clap::Parser;
use std::path::PathBuf;
#[derive(Parser)]
struct Args {
    #[arg(long)]
    data: PathBuf,
    pattern: String,
    #[arg(long)]
    agent: Option<String>,
    #[arg(long)]
    who: Option<String>,
    #[arg(long)]
    since_days: Option<i64>,
    #[arg(long)]
    regex: bool,
    #[arg(long)]
    count: bool,
    #[arg(long)]
    chats: bool,
    #[arg(long)]
    content: bool,
    #[arg(long)]
    optimize: bool,
    #[arg(long)]
    refresh_gaps: bool,
    #[arg(long)]
    rebuild_bounds: bool,
    #[arg(long, default_value_t = 40)]
    limit: usize,
    #[arg(long)]
    cache_mib: Option<u32>,
    #[arg(long)]
    mmap_mib: Option<u32>,
    /// Run the same search repeatedly in this process, for sampling profilers.
    #[arg(long, default_value_t = 1)]
    repeat: usize,
    /// Print each page hit's handle, tier and exact score.
    #[arg(long)]
    handles: bool,
}
fn main() -> Result<()> {
    let a = Args::parse();
    let root = DataRoot::open(&a.data, false)?;
    let optimize_ms = if a.optimize {
        let started = std::time::Instant::now();
        let _lock = root.lock(true)?;
        let mut writer = store::writer(&root)?;
        let tx = writer.transaction()?;
        for table in ["prose_fts", "tools_fts", "tool_lexicon"] {
            tx.execute(
                &format!("INSERT INTO {table}({table}) VALUES('optimize')"),
                [],
            )?;
        }
        tx.commit()?;
        Some(started.elapsed().as_secs_f64() * 1000.)
    } else {
        None
    };
    let gap_ms = if a.refresh_gaps {
        let started = std::time::Instant::now();
        store::refresh_phrase_gaps(&root)?;
        Some(started.elapsed().as_secs_f64() * 1000.)
    } else {
        None
    };
    let bounds_ms = if a.rebuild_bounds {
        let started = std::time::Instant::now();
        store::rebuild_bounds(&root)?;
        Some(started.elapsed().as_secs_f64() * 1000.)
    } else {
        None
    };
    let c = store::reader(&root)?;
    if let Some(mib) = a.cache_mib {
        c.pragma_update(None, "cache_size", -i64::from(mib) * 1024)?;
    }
    if let Some(mib) = a.mmap_mib {
        c.pragma_update(None, "mmap_size", i64::from(mib) * 1024 * 1024)?;
    }
    let options = Options {
        pattern: a.pattern,
        regex: a.regex,
        word: false,
        sort: "score".into(),
        limit: if a.count { 0 } else { a.limit },
        exact: a.count,
        count_only: a.count,
        count_by_tier: false,
        content: a.content,
        chats: a.chats,
        tools: true,
        filters: Filters {
            agent: a.agent,
            who: a.who,
            since: a
                .since_days
                .map(|days| chrono::Utc::now().timestamp_millis() - days * 86400000),
            ..Default::default()
        },
    };
    let mut r = query::search(&c, &options)?;
    for _ in 1..a.repeat {
        r = query::search(&c, &options)?;
    }
    if a.handles {
        for h in &r.hits {
            println!("{} {} {:.17}", h.handle, h.tier, h.score);
        }
    }
    println!(
        "{}",
        serde_json::json!({"total":r.total,"shown":r.hits.len(),"total_basis":r.total_basis,"examined":r.examined,"blocks":r.blocks,"timings":r.timings,"optimize_ms":optimize_ms,"gap_ms":gap_ms,"bounds_ms":bounds_ms,"cache_kib":c.pragma_query_value(None,"cache_size",|r|r.get::<_,i64>(0))?,"mmap_bytes":c.pragma_query_value(None,"mmap_size",|r|r.get::<_,i64>(0))?})
    );
    Ok(())
}
