use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

/// Where the index lands: $AGREP_DATA_DIR (or the pre-rename $TILT_DATA_DIR), else
/// ./data - the python side always exports the env when it spawns us, so installed
/// wheels never depend on this process's cwd.
fn data_dir() -> PathBuf {
    std::env::var_os("AGREP_DATA_DIR")
        .or_else(|| std::env::var_os("TILT_DATA_DIR"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("data"))
}

struct IndexLock {
    path: PathBuf,
    _file: File,
}

impl IndexLock {
    fn acquire() -> anyhow::Result<Self> {
        let path = data_dir().join(".index.lock");
        let started = Instant::now();
        let mut delay = Duration::from_millis(50);
        loop {
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(mut file) => {
                    writeln!(
                        file,
                        "pid={} label=agrep-rs time={:?}",
                        std::process::id(),
                        SystemTime::now()
                    )?;
                    return Ok(IndexLock { path, _file: file });
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                    if let Ok(meta) = fs::metadata(&path) {
                        if meta
                            .modified()
                            .ok()
                            .and_then(|t| t.elapsed().ok())
                            .map(|age| age > Duration::from_secs(6 * 3600))
                            .unwrap_or(false)
                        {
                            let _ = fs::remove_file(&path);
                            continue;
                        }
                    }
                    if started.elapsed() > Duration::from_secs(1800) {
                        anyhow::bail!("timed out waiting for {}", path.display());
                    }
                    std::thread::sleep(delay);
                    delay = (delay + delay / 2).min(Duration::from_secs(1));
                }
                Err(e) => return Err(e.into()),
            }
        }
    }
}

impl Drop for IndexLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

use agrep_core::cache;
use agrep_core::ingest;
use agrep_core::ingest_cache::IngestCache;
use agrep_core::model::{Event, Message};
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "agrep-rs",
    version,
    about = "ingest agent chat transcripts into the agrep index"
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Ingest transcripts and write data/messages.jsonl + data/replies.jsonl (the input the
    /// Python sidecar embeds/scores/clusters from).
    Index {
        /// Which agent to ingest: claude | codex | opencode | antigravity | kimi | cline | all.
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

/// Ingest one named adapter (or all of them concatenated), then dedupe. claude/codex/
/// opencode re-parse only sources changed since the last index via `cache`; antigravity
/// (small, many tiny per-session files) always full-parses, so it runs concurrently
/// with the cache-driven chain instead of after it.
fn ingest_agent(
    agent: &str,
    cache: &mut IngestCache,
) -> anyhow::Result<(Vec<Message>, Vec<Event>)> {
    let (msgs, evts) = match agent {
        "claude" => ingest::claude::collect(cache),
        "codex" => ingest::codex::collect(cache),
        "opencode" => ingest::opencode::collect(cache),
        "antigravity" => ingest::antigravity::collect(),
        "kimi" => ingest::kimi::collect(),
        "cline" => ingest::cline::collect(),
        "all" => {
            // the cacheless adapters (small stores, full-parse) overlap the cache-driven chain
            let ((mut m4, mut e4), (mut m, mut e)) = rayon::join(
                || {
                    let (mut m, mut e) = ingest::antigravity::collect();
                    let (m5, e5) = ingest::kimi::collect();
                    m.extend(m5);
                    e.extend(e5);
                    let (m6, e6) = ingest::cline::collect();
                    m.extend(m6);
                    e.extend(e6);
                    (m, e)
                },
                || {
                    let (mut m, mut e) = ingest::claude::collect(cache);
                    let (m2, e2) = ingest::codex::collect(cache);
                    m.extend(m2);
                    e.extend(e2);
                    let (m3, e3) = ingest::opencode::collect(cache);
                    m.extend(m3);
                    e.extend(e3);
                    (m, e)
                },
            );
            m.append(&mut m4);
            e.append(&mut e4);
            (m, e)
        }
        other => {
            anyhow::bail!(
                "unknown agent `{other}` (have: claude, codex, opencode, antigravity, kimi, cline, all)"
            );
        }
    };
    // Dedupe by (agent, session, turn): Codex resumes write a new rollout file that REPLAYS the
    // earlier turns, so the same message gets ingested once per rollout file (seen up to 14x).
    // Same tuple == identical message, so keep-first is correct.
    let mut seen = HashSet::new();
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
    Ok((normalize_messages(deduped), ededuped))
}

fn is_synthetic_turn(model: &str) -> bool {
    model == "<synthetic>"
}

fn is_control_turn(text: &str) -> bool {
    let t = text.trim();
    t.eq_ignore_ascii_case("continue")
        || t == "Request interrupted by user"
        || t.starts_with("[Request interrupted by user")
}

fn is_recap_turn(text: &str) -> bool {
    text.trim_start()
        .starts_with("This session is being continued from a previous conversation")
}

fn is_harness_project(project: &str) -> bool {
    let p = project.to_ascii_lowercase();
    p == "vo-exp"
        || p == "_probe"
        || p.contains("_probe")
        || p.contains("control_")
        || p.contains("run_control")
        || p.contains("haiku-control")
        || p.contains("haiku-treatment")
}

fn is_harness_turn(project: &str, text: &str) -> bool {
    let t = text.trim_start();
    is_harness_project(project)
        || t.starts_with("Return only valid JSON that matches the schema below.")
        || t.starts_with("Use the context below to decide what Candence should say")
        || t.starts_with("You are a workflow planner for OpenCode.")
        || t.starts_with("You are an independent verifier.")
        || t.starts_with("\"Create a file named ok.txt")
        || t.starts_with("\"You are working in the current directory.")
        || t.starts_with("\"Stay in this directory. STEP 1:")
        || t.starts_with("\"STEP 1: run ")
        || t.starts_with("Follow the README in this directory to run the analysis")
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RowKind {
    User,
    Synthetic,
    Control,
    Recap,
    Harness,
}

fn row_kind(m: &Message) -> RowKind {
    let raw_model = m.model.trim();
    if is_synthetic_turn(raw_model) {
        RowKind::Synthetic
    } else if is_control_turn(&m.text) {
        RowKind::Control
    } else if is_recap_turn(&m.text) {
        RowKind::Recap
    } else if is_harness_turn(&m.project, &m.text) {
        RowKind::Harness
    } else {
        RowKind::User
    }
}

fn normalize_messages(msgs: Vec<Message>) -> Vec<Message> {
    let mut session_models: HashMap<(&'static str, Arc<str>), HashSet<Arc<str>>> = HashMap::new();
    let mut session_timeline: HashMap<(&'static str, Arc<str>), Vec<(u32, Arc<str>)>> =
        HashMap::new();
    for m in &msgs {
        let model = m.model.trim();
        if model.is_empty() || row_kind(m) != RowKind::User {
            continue;
        }
        session_models
            .entry((m.agent, m.session.clone()))
            .or_default()
            .insert(m.model.clone());
        session_timeline
            .entry((m.agent, m.session.clone()))
            .or_default()
            .push((m.turn, m.model.clone()));
    }
    for models in session_timeline.values_mut() {
        models.sort_by_key(|(turn, _)| *turn);
    }

    msgs.into_iter()
        .map(|m| {
            let raw_model = m.model.trim();
            let (who, model, model_source): (&str, Arc<str>, &str) = match row_kind(&m) {
                RowKind::Synthetic => ("synthetic", m.model.clone(), "synthetic"),
                RowKind::Control => ("control", Arc::from("<control>"), "control"),
                RowKind::Recap => ("recap", Arc::from("<recap>"), "recap"),
                RowKind::Harness => {
                    if raw_model.is_empty() {
                        ("harness", Arc::from("<harness>"), "harness")
                    } else {
                        ("harness", m.model.clone(), "explicit_harness")
                    }
                }
                RowKind::User => {
                    if !raw_model.is_empty() {
                        ("user", m.model.clone(), "explicit")
                    } else {
                        let models = session_models.get(&(m.agent, m.session.clone()));
                        match models.map(|s| s.len()).unwrap_or(0) {
                            1 => {
                                let model = models
                                    .and_then(|s| s.iter().next())
                                    .cloned()
                                    .unwrap_or_else(|| Arc::from(""));
                                ("user", model, "session")
                            }
                            0 => ("user", Arc::from(""), "unknown"),
                            _ => {
                                let timeline = session_timeline.get(&(m.agent, m.session.clone()));
                                if let Some(model) =
                                    timeline.and_then(|rows| temporal_backfill(rows, m.turn))
                                {
                                    ("user", model, "temporal_session")
                                } else {
                                    ("user", Arc::from(""), "ambiguous_session")
                                }
                            }
                        }
                    }
                }
            };
            Message {
                agent: m.agent,
                project: m.project,
                session: m.session,
                ts: m.ts,
                turn: m.turn,
                text: m.text,
                who: who.into(),
                model,
                model_source: model_source.into(),
                reply: m.reply,
            }
        })
        .collect()
}

fn temporal_backfill(rows: &[(u32, Arc<str>)], turn: u32) -> Option<Arc<str>> {
    let before = rows
        .iter()
        .rev()
        .find(|(known_turn, _)| *known_turn < turn)
        .map(|(_, model)| model);
    let after = rows
        .iter()
        .find(|(known_turn, _)| *known_turn > turn)
        .map(|(_, model)| model);
    match (before, after) {
        (Some(a), Some(b)) if a == b => Some(a.clone()),
        (None, Some(model)) => Some(model.clone()),
        (Some(model), None) => Some(model.clone()),
        _ => None,
    }
}

/// `agrep index` - ingest and (re)write data/messages.jsonl + data/replies.jsonl. The embeddings,
/// affect, topics, and arcs are produced by the Python sidecar (`python reindex.py`).
fn index_cmd(agent: &str, full: bool) -> anyhow::Result<()> {
    let t0 = Instant::now();
    let _index_lock = IndexLock::acquire()?;
    // Per-phase wall clock, printed at the end: optimize from these numbers, not intuition.
    let mut phases: Vec<(&'static str, u128)> = Vec::new();
    let mut mark = Instant::now();
    macro_rules! lap {
        ($name:expr) => {{
            phases.push(($name, mark.elapsed().as_millis()));
            mark = Instant::now();
        }};
    }
    let cache_path = data_dir().join(".ingest_cache.bin");
    let mut pcache = if full {
        IngestCache::cold()
    } else {
        IngestCache::load(&cache_path)
    };
    lap!("load-cache");
    // a complete parse (cold cache or --full) yields the full event set; a warm/incremental
    // run only carries touched sessions' events, so the pulse rollup is left as-is until the
    // next complete run (its aggregates over ~480k events don't move on a handful of new ones).
    let complete = full || !pcache.warm;
    let (msgs, evts) = ingest_agent(agent, &mut pcache)?;
    lap!("ingest+dedupe");
    // persist the refreshed parse cache for the next run (ignored on load if schema bumps)
    if let Err(e) = pcache.save(&cache_path) {
        eprintln!("  ! parse-cache save failed (next run re-parses): {e}");
    }
    lap!("save-cache");
    let n = msgs.len();
    let path = data_dir().join("messages.jsonl");
    cache::write_messages(&msgs, &path)?;
    let rpath = data_dir().join("replies.jsonl");
    cache::write_replies(&msgs, &rpath)?;
    let n_sessions = cache::write_session_index(&msgs, &data_dir().join("sessions.jsonl"))?;
    lap!("write-msgs");
    // keep = every live session's event filename, so unchanged event files aren't deleted.
    // The delete sweep is scoped to the agents actually ingested this run - keep covers
    // only their sessions, so an unscoped sweep on `--agent opencode` would wipe the
    // claude/codex event files.
    let keep: std::collections::HashSet<String> = msgs
        .iter()
        .map(|m| cache::event_fname(m.agent, &m.session))
        .collect();
    let run_agents: Vec<&str> = match agent {
        "all" => vec![
            "claude",
            "codex",
            "opencode",
            "antigravity",
            "kimi",
            "cline",
        ],
        one => vec![one],
    };
    let edir = data_dir().join("events");
    let (n_files, n_events, n_rewritten) = cache::write_events(&evts, &edir, &keep, &run_agents)?;
    if complete {
        cache::write_event_stats(&evts, &data_dir().join("event_stats.json"))?;
    }
    lap!("write-events");
    let _ = mark;
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
    let breakdown: Vec<String> = phases.iter().map(|(k, ms)| format!("{k} {ms}ms")).collect();
    println!("  phases: {}", breakdown.join(" · "));
    println!("  next: `agrep reindex` to (re)build embeddings / affect / topics / arcs");
    Ok(())
}
