use std::path::PathBuf;
use std::time::Instant;

use clap::{Parser, Subcommand};
use tilt_core::cache;
use tilt_core::ingest;
use tilt_core::model::Message;

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
    },
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Index { agent } => index_cmd(&agent),
    }
}

/// Ingest one named adapter (or all of them concatenated), then dedupe.
fn ingest_agent(agent: &str) -> anyhow::Result<Vec<Message>> {
    let msgs = match agent {
        "claude" => ingest::claude::collect(),
        "codex" => ingest::codex::collect(),
        "opencode" => ingest::opencode::collect(),
        "antigravity" => ingest::antigravity::collect(),
        "all" => {
            let mut v = ingest::claude::collect();
            v.extend(ingest::codex::collect());
            v.extend(ingest::opencode::collect());
            v.extend(ingest::antigravity::collect());
            v
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
    Ok(deduped)
}

/// `tilt index` — ingest and (re)write data/messages.jsonl + data/replies.jsonl. The embeddings,
/// affect, topics, and arcs are produced by the Python sidecar (`python reindex.py`).
fn index_cmd(agent: &str) -> anyhow::Result<()> {
    let t0 = Instant::now();
    let msgs = ingest_agent(agent)?;
    let n = msgs.len();
    let path = PathBuf::from("data").join("messages.jsonl");
    cache::write_messages(&msgs, &path)?;
    let rpath = PathBuf::from("data").join("replies.jsonl");
    cache::write_replies(&msgs, &rpath)?;
    let with_model = msgs.iter().filter(|m| !m.model.is_empty()).count();
    let with_reply = msgs.iter().filter(|m| !m.reply.trim().is_empty()).count();
    println!(
        "  indexed {} messages -> {} ({:.0}ms)",
        n,
        path.display(),
        t0.elapsed().as_secs_f64() * 1000.0
    );
    println!(
        "  {} turns carry a model · {} carry an agent reply -> {}",
        with_model,
        with_reply,
        rpath.display()
    );
    println!("  next: run `python reindex.py` to (re)build embeddings / affect / topics / arcs");
    Ok(())
}
