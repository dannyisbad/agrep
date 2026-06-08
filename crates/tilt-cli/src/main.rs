use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Instant;

use clap::{Parser, Subcommand};
use tilt_core::cache;
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
    Ok(msgs)
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
