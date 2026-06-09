use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::time::Instant;

use clap::{Parser, Subcommand};
use tilt_core::cache;
use tilt_core::index::{self, Index};
use tilt_core::ingest;
use tilt_core::model::Message;
use tilt_core::scan::Scanner;
use tilt_core::score::{score, Tag};

#[derive(Parser)]
#[command(name = "tilt", version, about = "forensic instrument for chat vibes")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Scan transcripts and print the lexical rage/vulgarity readout.
    Scan {
        /// Which agent to ingest: claude | codex | opencode | antigravity | all.
        #[arg(long, default_value = "all")]
        agent: String,
        /// Minimum messages for a project to appear in the rage leaderboard.
        #[arg(long, default_value_t = 25)]
        min_msgs: u64,
        /// Write the full ingested set to data/messages.jsonl for the semantic sidecar.
        #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
        cache: bool,
    },
    /// Ingest transcripts and write data/messages.jsonl (the input the embed step consumes).
    Index {
        /// Which agent to ingest: claude | codex | opencode | antigravity | all.
        #[arg(long, default_value = "all")]
        agent: String,
    },
    /// Semantic search over the embedding index (requires the embed step to have run).
    Search {
        /// The query text. Informational only here — the actual vector is data/query.f32,
        /// which the embed step produces from this same text.
        query: String,
        /// How many hits to return.
        #[arg(long, default_value_t = 10)]
        k: usize,
    },
}

#[derive(Default)]
struct Agg {
    msgs: u64,
    vulgarity: u64,
    rage: f64,
    fuming: u64,
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Scan {
            agent,
            min_msgs,
            cache,
        } => scan(&agent, min_msgs, cache),
        Cmd::Index { agent } => index_cmd(&agent),
        Cmd::Search { query, k } => search_cmd(&query, k),
    }
}

/// Ingest one named adapter (or all of them concatenated).
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
    // Dedupe by (agent, session, turn): Codex resumes write a new rollout file that REPLAYS
    // the earlier turns, so the same message gets ingested once per rollout file (seen up to
    // 14x). Same tuple == identical message, so keep-first is correct.
    let mut seen = std::collections::HashSet::new();
    let deduped: Vec<Message> = msgs
        .into_iter()
        .filter(|m| seen.insert((m.agent, m.session.clone(), m.turn)))
        .collect();
    Ok(deduped)
}

fn scan(agent: &str, min_msgs: u64, cache_on: bool) -> anyhow::Result<()> {
    let t0 = Instant::now();
    let msgs = ingest_agent(agent)?;
    let t_ingest = t0.elapsed();

    let sc = Scanner::new();
    let mut projects: HashMap<String, Agg> = HashMap::new();
    let mut by_agent: HashMap<&'static str, Agg> = HashMap::new();
    let mut words: HashMap<String, u64> = HashMap::new();
    let mut tags = [0u64; 6]; // fuming, annoyed, irked, hype, casual, neutral
    let mut tot_vulg: u64 = 0;
    let mut tot_rage: f64 = 0.0;

    for m in &msgs {
        let h = sc.scan(&m.text);
        let s = score(&m.text, &h);
        let a = projects.entry(m.project.clone()).or_default();
        a.msgs += 1;
        a.vulgarity += s.vulgarity as u64;
        a.rage += s.rage as f64;
        if s.tag == Tag::Fuming {
            a.fuming += 1;
        }
        let ag = by_agent.entry(m.agent).or_default();
        ag.msgs += 1;
        ag.vulgarity += s.vulgarity as u64;
        ag.rage += s.rage as f64;
        if s.tag == Tag::Fuming {
            ag.fuming += 1;
        }
        tot_vulg += s.vulgarity as u64;
        tot_rage += s.rage as f64;
        tags[tag_idx(s.tag)] += 1;
        for (w, _) in h.swears {
            *words.entry(w).or_default() += 1;
        }
    }
    let t_total = t0.elapsed();

    let n = msgs.len() as u64;
    println!("\n  tilt · {agent}");
    println!(
        "  {} messages · {} vulgarity pts · {:.0} cumulative rage",
        n, tot_vulg, tot_rage
    );
    println!(
        "  ingest {:.0}ms · scan+score {:.0}ms · {:.0} msg/s",
        t_ingest.as_secs_f64() * 1000.0,
        (t_total - t_ingest).as_secs_f64() * 1000.0,
        n as f64 / t_total.as_secs_f64().max(1e-9),
    );

    // per-agent summary (msgs + vulgarity + mean rough-rage)
    let agent_order = ["claude", "codex", "opencode", "antigravity"];
    println!("\n  by agent                    msgs   vulg  mean_rage");
    for ag_name in agent_order {
        if let Some(a) = by_agent.get(ag_name) {
            let mean = if a.msgs > 0 { a.rage / a.msgs as f64 } else { 0.0 };
            println!(
                "  {:<26} {:>6} {:>6} {:>9.2}",
                ag_name, a.msgs, a.vulgarity, mean
            );
        }
    }

    // vocabulary
    let mut wv: Vec<_> = words.iter().collect();
    wv.sort_by(|a, b| b.1.cmp(a.1));
    print!("\n  vocabulary  ");
    for (w, c) in wv.iter().take(12) {
        print!("{w}:{c}  ");
    }
    println!();

    // tag distribution
    let labels = ["fuming", "annoyed", "irked", "hype", "casual", "neutral"];
    print!("\n  register    ");
    for (i, l) in labels.iter().enumerate() {
        let pct = if n > 0 {
            tags[i] as f64 / n as f64 * 100.0
        } else {
            0.0
        };
        print!("{l} {:.1}%  ", pct);
    }
    println!();

    // potty-mouth leaderboard (raw vulgarity)
    let mut pv: Vec<_> = projects.iter().collect();
    pv.sort_by(|a, b| b.1.vulgarity.cmp(&a.1.vulgarity));
    println!("\n  vulgarity by project        msgs   vulg   /msg");
    for (p, a) in pv.iter().take(12) {
        println!(
            "  {:<26} {:>6} {:>6} {:>6.2}",
            trunc(p, 26),
            a.msgs,
            a.vulgarity,
            a.vulgarity as f64 / a.msgs as f64
        );
    }

    // rage leaderboard (mean rage, gated by min_msgs) — who actually pissed you off
    let mut pr: Vec<_> = projects.iter().filter(|(_, a)| a.msgs >= min_msgs).collect();
    pr.sort_by(|a, b| {
        (b.1.rage / b.1.msgs as f64)
            .partial_cmp(&(a.1.rage / a.1.msgs as f64))
            .unwrap()
    });
    println!("\n  rage by project (>= {min_msgs} msgs)  msgs  mean_rage  fuming%");
    for (p, a) in pr.iter().take(12) {
        let mean = a.rage / a.msgs as f64;
        let fpct = a.fuming as f64 / a.msgs as f64 * 100.0;
        println!(
            "  {:<26} {:>6} {:>9.2} {:>7.1}",
            trunc(p, 26),
            a.msgs,
            mean,
            fpct
        );
    }
    println!();

    // cache the full ingested set for the semantic sidecar / search index.
    if cache_on {
        let path = PathBuf::from("data").join("messages.jsonl");
        cache::write_messages(&msgs, &path)?;
        println!("  cached {} messages -> {}", n, path.display());
    }

    Ok(())
}

/// `tilt index` — ingest and (re)write data/messages.jsonl, the row source the embed step reads.
/// The embeddings themselves are produced by the Python sidecar, so we just refresh the cache and
/// print the next step.
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
    println!("  next: run `tilt embed` (Python sidecar) to write data/embeddings.f32 + data/embeddings.ids");
    Ok(())
}

/// `tilt search <query>` — brute-force cosine over data/embeddings.f32, joined back to the message
/// text in data/messages.jsonl. The query *vector* comes from data/query.f32, which the embed step
/// writes from the query text; this command does not embed anything itself.
fn search_cmd(query: &str, k: usize) -> anyhow::Result<()> {
    let data = PathBuf::from("data");
    let mat_path = data.join("embeddings.f32");
    let query_path = data.join("query.f32");

    // Both artifacts are produced by the embed step. Missing -> a clear hint, not a panic.
    if !mat_path.exists() || !query_path.exists() {
        let missing = if !mat_path.exists() {
            "data/embeddings.f32"
        } else {
            "data/query.f32"
        };
        println!("  no semantic index: {missing} is missing.");
        println!("  run the embed step first:");
        println!("    1. tilt index                 # writes data/messages.jsonl");
        println!("    2. tilt embed                 # Python sidecar -> embeddings.f32 + .ids");
        println!("    3. tilt embed --query \"{}\"   # writes data/query.f32", trunc(query, 40));
        println!("  then re-run: tilt search \"{}\"", trunc(query, 40));
        return Ok(());
    }

    let dim = index::read_dim(&data);
    let q = index::load_query(&query_path, dim)?;
    let idx = Index::open(&data)?;
    let hits = idx.search(&q, k);

    // Join hits back onto message metadata/text via the id -> (project, agent, text) map.
    let meta = load_message_meta(&data.join("messages.jsonl"))?;

    println!("\n  tilt search · \"{}\"  ({} rows indexed)", query, idx.rows());
    if hits.is_empty() {
        println!("  no hits.");
        return Ok(());
    }
    println!("  rank  score  agent/project                 snippet");
    for (rank, (id, score)) in hits.iter().enumerate() {
        match meta.get(id) {
            Some((project, agent, text)) => {
                let ap = format!("{}/{}", agent, project);
                println!(
                    "  {:>4}  {:>5.3}  {:<28}  {}",
                    rank + 1,
                    score,
                    trunc(&ap, 28),
                    snippet(text, 80)
                );
            }
            None => {
                // id present in the index but not in messages.jsonl (stale cache).
                println!(
                    "  {:>4}  {:>5.3}  {:<28}  [id {} not in messages.jsonl]",
                    rank + 1,
                    score,
                    "?",
                    id
                );
            }
        }
    }
    println!();
    Ok(())
}

/// Load data/messages.jsonl into an id -> (project, agent, text) map for search result join.
fn load_message_meta(
    path: &Path,
) -> anyhow::Result<HashMap<String, (String, String, String)>> {
    let file = std::fs::File::open(path)
        .map_err(|e| anyhow::anyhow!("opening {} ({e}); run `tilt index` first", path.display()))?;
    let reader = BufReader::new(file);
    let mut map = HashMap::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let v: serde_json::Value = serde_json::from_str(&line)?;
        let id = v["id"].as_str().unwrap_or_default().to_string();
        if id.is_empty() {
            continue;
        }
        let project = v["project"].as_str().unwrap_or_default().to_string();
        let agent = v["agent"].as_str().unwrap_or_default().to_string();
        let text = v["text"].as_str().unwrap_or_default().to_string();
        map.insert(id, (project, agent, text));
    }
    Ok(map)
}

/// One-line snippet: collapse all whitespace runs to single spaces, then truncate to `n` chars.
fn snippet(s: &str, n: usize) -> String {
    let collapsed: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    trunc(&collapsed, n)
}

fn tag_idx(t: Tag) -> usize {
    match t {
        Tag::Fuming => 0,
        Tag::Annoyed => 1,
        Tag::Irked => 2,
        Tag::Hype => 3,
        Tag::Casual => 4,
        Tag::Neutral => 5,
    }
}

fn trunc(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let mut o: String = s.chars().take(n - 1).collect();
        o.push('…');
        o
    }
}
