use std::path::PathBuf;
use std::time::Instant;

use clap::{Parser, Subcommand};
use tilt_core::cache;
use tilt_core::ingest;
use tilt_core::ingest_cache::IngestCache;
use tilt_core::model::{Event, Message};

#[derive(Parser)]
#[command(name = "tilt", version, about = "ingest agent chat transcripts into the tilt index")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Ingest transcripts and write data/messages.jsonl + data/replies.jsonl (the input the
    /// Python sidecar embeds/scores/clusters from).
    Index {
        /// Which agent to ingest: claude | codex | opencode | antigravity | all.
        #[arg(long, default_value = "all")]
        agent: String,
        /// Ignore the per-file parse cache and re-parse every source file from scratch.
        #[arg(long)]
        full: bool,
    },
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Index { agent, full } => index_cmd(&agent, full),
    }
}

/// Ingest one named adapter (or all of them concatenated), then dedupe. The claude/codex
/// adapters (the 9GB+ bulk) re-parse only files changed since the last index via `cache`;
/// opencode/antigravity are small and always full-parse.
fn ingest_agent(agent: &str, cache: &mut IngestCache) -> anyhow::Result<(Vec<Message>, Vec<Event>)> {
    let (msgs, evts) = match agent {
        "claude" => ingest::claude::collect(cache),
        "codex" => ingest::codex::collect(cache),
        "opencode" => ingest::opencode::collect(),
        "antigravity" => ingest::antigravity::collect(),
        "all" => {
            let (mut m, mut e) = ingest::claude::collect(cache);
            let (m2, e2) = ingest::codex::collect(cache);
            m.extend(m2);
            e.extend(e2);
            for (m3, e3) in [ingest::opencode::collect(), ingest::antigravity::collect()] {
                m.extend(m3);
                e.extend(e3);
            }
            (m, e)
        }
        other => {
            anyhow::bail!(
                "unknown agent `{other}` (have: claude, codex, opencode, antigravity, all)"
            );
        }
    };
    // Dedupe by (agent, session, turn): Codex resumes write a new rollout file that REPLAYS the
    // earlier turns, so the same message gets ingested once per rollout file (seen up to 14x).
    // Same tuple == identical message, so keep-first is correct.
    let mut seen = std::collections::HashSet::new();
    let deduped: Vec<Message> = msgs
        .into_iter()
        .filter(|m| seen.insert((m.agent, m.session.clone(), m.turn)))
        .collect();
    // Same replay story for events; the store's own call_id is the identity.
    let mut eseen = std::collections::HashSet::new();
    let ededuped: Vec<Event> = evts
        .into_iter()
        .filter(|e| eseen.insert((e.agent, e.session.clone(), e.call_id.clone())))
        .collect();
    Ok((deduped, ededuped))
}

/// `tilt index` — ingest and (re)write data/messages.jsonl + data/replies.jsonl. The embeddings,
/// affect, topics, and arcs are produced by the Python sidecar (`python reindex.py`).
fn index_cmd(agent: &str, full: bool) -> anyhow::Result<()> {
    let t0 = Instant::now();
    let cache_path = PathBuf::from("data").join(".ingest_cache.bin");
    let mut pcache = if full {
        IngestCache::cold()
    } else {
        IngestCache::load(&cache_path)
    };
    // a complete parse (cold cache or --full) yields the full event set; a warm/incremental
    // run only carries touched sessions' events, so the pulse rollup is left as-is until the
    // next complete run (its aggregates over ~480k events don't move on a handful of new ones).
    let complete = full || !pcache.warm;
    let (msgs, evts) = ingest_agent(agent, &mut pcache)?;
    // persist the refreshed parse cache for the next run (ignored on load if schema bumps)
    if let Err(e) = pcache.save(&cache_path) {
        eprintln!("  ! parse-cache save failed (next run re-parses): {e}");
    }
    let n = msgs.len();
    let path = PathBuf::from("data").join("messages.jsonl");
    cache::write_messages(&msgs, &path)?;
    let rpath = PathBuf::from("data").join("replies.jsonl");
    cache::write_replies(&msgs, &rpath)?;
    let n_sessions =
        cache::write_session_index(&msgs, &PathBuf::from("data").join("sessions.jsonl"))?;
    // keep = every live session's event filename, so unchanged event files aren't deleted
    let keep: std::collections::HashSet<String> =
        msgs.iter().map(|m| cache::event_fname(m.agent, &m.session)).collect();
    let edir = PathBuf::from("data").join("events");
    let (n_files, n_events, n_rewritten) = cache::write_events(&evts, &edir, &keep)?;
    if complete {
        cache::write_event_stats(&evts, &PathBuf::from("data").join("event_stats.json"))?;
    }
    let with_model = msgs.iter().filter(|m| !m.model.is_empty()).count();
    let with_reply = msgs.iter().filter(|m| !m.reply.trim().is_empty()).count();
    println!(
        "  indexed {} messages across {} sessions -> {} ({:.0}ms)",
        n,
        n_sessions,
        path.display(),
        t0.elapsed().as_secs_f64() * 1000.0
    );
    println!(
        "  {} turns carry a model · {} carry an agent reply -> {}",
        with_model,
        with_reply,
        rpath.display()
    );
    println!(
        "  {} session event files -> {} ({} rewritten, {} new events this run)",
        n_files,
        edir.display(),
        n_rewritten,
        n_events
    );
    println!("  next: `python tilt.py reindex` to (re)build embeddings / affect / topics / arcs");
    Ok(())
}
