//! Matched full-process comparison of agrep 0.4.9 (the reference) and this build (the
//! candidate) on the same corpus, machine and load. Samples alternate reference and
//! candidate, every sample is kept and verified, and a case is won only when the
//! candidate's median and p95 are both below the reference's.
//!
//! Minimal mappings where one command line cannot serve both versions:
//! - Each lane has its own temporary AGREP_DATA_DIR; both read the same agent stores.
//!   0.4.9 also gets a temporary AGREP_MODEL_DIR: its model cache otherwise lives in the
//!   user's default 0.4.9 data directory.
//! - 0.4.9's `index` returns while its daemon still builds the search database ("the search
//!   database is still building in the background"); 0.5's `index` returns with it built.
//!   A reference index sample ends when the process has exited and the build request it
//!   queued (`.search_index_request`) has been served, observed by watching the data
//!   directory. The process exit alone is recorded too.
//! - Semantic preparation is untimed: 0.4.9 `reindex` (fetches its ONNX model and embeds in
//!   the foreground); 0.5 `setup --yes --no-teach --no-hook --no-archive`, waiting for the
//!   detached `__work` it starts to exit, then `__work`.
//! - Agent-profile hybrid cases read rendered rows: 0.4.9's `--json` turns its automatic
//!   meaning lane off.
//! - The no-change census is the public `index` with nothing changed; 0.4.9 has no
//!   census-only command.
use anyhow::{bail, ensure, Context, Result};
use clap::{Parser, ValueEnum};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::{Duration, Instant},
};
#[path = "support/corpus.rs"]
mod corpus;
#[path = "support/matched.rs"]
mod matched;
#[path = "support/process.rs"]
mod process;
#[path = "support/reap.rs"]
mod reap;
#[path = "support/results.rs"]
mod results;

const SEARCH_REQUEST: &str = ".search_index_request";
const SETTLE: Duration = Duration::from_secs(3600);
const REAP: Duration = Duration::from_secs(300);
const NATURAL: &str = "how did the drafting go last run";

#[derive(Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Workload {
    Mixed,
    Dense,
    Semantic,
    Real,
}

#[derive(Parser)]
struct Args {
    /// agrep 0.4.9, the reference: its installed `agrep` command.
    #[arg(long)]
    reference: PathBuf,
    /// The 0.5 executable; defaults to target/release/agrep beside this example.
    #[arg(long)]
    candidate: Option<PathBuf>,
    #[arg(long, value_enum)]
    workload: Workload,
    /// Synthetic rows; defaults to 100000 mixed, 2000000 dense, 1000 semantic.
    #[arg(long)]
    rows: Option<usize>,
    #[arg(long, default_value_t = 20)]
    runs: usize,
    /// Samples of each first build and first hybrid query.
    #[arg(long, default_value_t = 3)]
    build_runs: usize,
    /// A verified 0.5 model directory to copy instead of downloading one.
    #[arg(long)]
    model_dir: Option<PathBuf>,
    /// 0.4.9's model root (its AGREP_MODEL_DIR); 0.4.9 fetches its model into it if empty.
    #[arg(long)]
    reference_model_dir: Option<PathBuf>,
    /// Agent stores of --workload real, read only; defaults to $HOME.
    #[arg(long)]
    home: Option<PathBuf>,
    #[arg(long)]
    output: Option<PathBuf>,
    /// Fail unless every case of a full-size workload is a win.
    #[arg(long)]
    check: bool,
    /// Comma-separated case names, for diagnosis.
    #[arg(long, conflicts_with = "check")]
    only: Option<String>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    /// From an empty data directory, `build_runs` samples.
    FirstIndex,
    /// A warm command, after one unmeasured warm-up per lane.
    Warm,
    /// One appended turn before each lane's sample.
    Refresh,
    /// One appended turn per pair, then each lane's query and an untimed drain.
    Changed,
    /// No process of the lane running before each sample, `build_runs` samples.
    Cold,
}

struct Case {
    name: String,
    args: Vec<String>,
    /// Arguments as reported: real-history session ids and probe tokens are withheld.
    shown: Vec<String>,
    profile: &'static str,
    expected_exit: i32,
    kind: Kind,
}

impl Case {
    fn new(name: &str, args: &[&str], profile: &'static str, kind: Kind) -> Self {
        let args: Vec<String> = args.iter().map(|a| a.to_string()).collect();
        let expected_exit = if matches!(name, "rare" | "compact_rare" | "rare_filtered") {
            1
        } else {
            0
        };
        Case {
            name: name.into(),
            shown: args.clone(),
            args,
            profile,
            expected_exit,
            kind,
        }
    }
    fn is_index(&self) -> bool {
        self.args.first().is_some_and(|a| a == "index")
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Role {
    Reference,
    Candidate,
}

struct Lane {
    role: Role,
    bin: PathBuf,
    data: PathBuf,
    /// The reference's AGREP_MODEL_DIR.
    model: Option<PathBuf>,
    reap: reap::Lane,
}

impl Lane {
    fn new(
        role: Role,
        bin: &Path,
        data: PathBuf,
        model: Option<PathBuf>,
        candidate: &Path,
    ) -> Result<Self> {
        fs::create_dir_all(&data)?;
        Ok(Lane {
            role,
            bin: bin.to_path_buf(),
            reap: reap::Lane::new(&data, candidate, role == Role::Candidate),
            data,
            model,
        })
    }
    fn name(&self) -> &'static str {
        match self.role {
            Role::Reference => "reference",
            Role::Candidate => "candidate",
        }
    }
    fn reset(&self) -> Result<()> {
        match fs::remove_dir_all(&self.data) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
            _ => {}
        }
        fs::create_dir_all(&self.data)?;
        Ok(())
    }
    fn end(&self) -> Result<usize> {
        reap::end(&self.reap, REAP)
    }
}

struct Sample {
    ms: f64,
    exit_ms: f64,
    exit: i32,
    settled: bool,
    evidence: Value,
    load: Option<[f64; 3]>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

/// One corpus and the two lanes that read it.
struct Bench {
    lanes: [Lane; 2],
    /// HOME and USERPROFILE.
    home: PathBuf,
    /// AGREP_HOME: the agent stores.
    sources: PathBuf,
    /// TMPDIR, short enough for 0.4.9's resident socket path.
    tmp: Option<PathBuf>,
    /// Real history: diagnostics never print command output.
    private: bool,
    trace: Option<PathBuf>,
}

fn load_average() -> Option<[f64; 3]> {
    #[cfg(unix)]
    {
        let mut load = [0f64; 3];
        (unsafe { libc::getloadavg(load.as_mut_ptr(), 3) } == 3).then_some(load)
    }
    #[cfg(not(unix))]
    {
        None
    }
}

fn millis(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.
}

/// The last complete line of a session file, read from its end.
fn last_line(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path)?;
    let len = file.metadata()?.len();
    let mut window = 4096u64;
    loop {
        let start = len.saturating_sub(window);
        file.seek(SeekFrom::Start(start))?;
        let mut buffer = Vec::new();
        file.read_to_end(&mut buffer)?;
        let body = buffer.strip_suffix(b"\n").unwrap_or(&buffer);
        if let Some(at) = body.iter().rposition(|b| *b == b'\n') {
            return Ok(String::from_utf8_lossy(&body[at + 1..]).into_owned());
        }
        if start == 0 {
            return Ok(String::from_utf8_lossy(body).into_owned());
        }
        window *= 4;
    }
}

/// Appends one user turn to a pi/omp session, parented on its last entry.
fn append_turn(path: &Path, id: &str, text: &str) -> Result<()> {
    let parent: Value = serde_json::from_str(&last_line(path)?).context("last session entry")?;
    let mut file = fs::OpenOptions::new().append(true).open(path)?;
    writeln!(
        file,
        "{}",
        json!({"type":"message","id":id,"parentId":parent["id"],"timestamp":chrono::Utc::now().to_rfc3339(),"message":{"role":"user","content":text}})
    )?;
    Ok(())
}

fn bytes(path: &Path) -> u64 {
    match fs::symlink_metadata(path) {
        Ok(m) if m.is_dir() => fs::read_dir(path)
            .map(|entries| entries.flatten().map(|e| bytes(&e.path())).sum())
            .unwrap_or(0),
        Ok(m) => m.len(),
        Err(_) => 0,
    }
}

impl Bench {
    fn reference(&self) -> &Lane {
        &self.lanes[0]
    }
    fn candidate(&self) -> &Lane {
        &self.lanes[1]
    }

    fn invoke(&self, lane: &Lane, args: &[String], profile: &str) -> Result<Sample> {
        let mut command = process::command(
            &lane.bin,
            &self.home,
            &lane.data,
            profile,
            std::env::var_os("PATH").unwrap_or_default(),
            std::env::var_os("SystemRoot"),
        );
        command.env("AGREP_HOME", &self.sources);
        if let Some(tmp) = &self.tmp {
            command.env("TMPDIR", tmp).env("TMP", tmp).env("TEMP", tmp);
        }
        if let Some(model) = &lane.model {
            command.env("AGREP_MODEL_DIR", model);
        }
        command.args(args).stdin(Stdio::null());
        let load = load_average();
        let start = Instant::now();
        let out = command
            .output()
            .with_context(|| format!("starting {}", lane.bin.display()))?;
        let exit_ms = millis(start);
        let exit = out.status.code().unwrap_or(-1);
        let index = args.first().is_some_and(|a| a == "index");
        let settled = if index && lane.role == Role::Reference {
            reap::wait_absent(&lane.data.join(SEARCH_REQUEST), &lane.reap, SETTLE)?
        } else {
            true
        };
        let ms = if index { millis(start) } else { exit_ms };
        Ok(Sample {
            ms,
            exit_ms,
            exit,
            settled,
            evidence: results::evidence(&out.stdout, &out.stderr),
            load,
            stdout: out.stdout,
            stderr: out.stderr,
        })
    }

    /// An untimed step that must succeed.
    fn prepare(&self, lane: &Lane, args: &[&str]) -> Result<Sample> {
        let args: Vec<String> = args.iter().map(|a| a.to_string()).collect();
        let sample = self.invoke(lane, &args, "classic")?;
        if sample.exit != 0 || !sample.settled {
            self.diagnose(lane, &args.join(" "), &sample);
            bail!(
                "{} preparation `{}` failed: exit {}, settled {}",
                lane.name(),
                args.join(" "),
                sample.exit,
                sample.settled
            );
        }
        Ok(sample)
    }

    fn diagnose(&self, lane: &Lane, what: &str, sample: &Sample) {
        if self.private {
            eprintln!(
                "  {} {what}: exit {}, {} stderr bytes",
                lane.name(),
                sample.exit,
                sample.stderr.len()
            );
        } else {
            let text = String::from_utf8_lossy(&sample.stderr);
            let tail: Vec<_> = text.lines().rev().take(6).collect();
            eprintln!(
                "  {} {what}: exit {}, settled {}; stderr tail: {:?}",
                lane.name(),
                sample.exit,
                sample.settled,
                tail
            );
        }
    }

    fn trace(&self, value: &Value) -> Result<()> {
        if let Some(path) = &self.trace {
            let mut file = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(path)?;
            writeln!(file, "{value}")?;
        }
        Ok(())
    }

    fn run(
        &self,
        case: &Case,
        runs: usize,
        session: Option<&Path>,
        oracle: Option<&(Vec<String>, usize)>,
    ) -> Result<Value> {
        let mut ended = [0usize; 2];
        for (k, lane) in self.lanes.iter().enumerate() {
            ended[k] += lane.end()?;
        }
        let mut warmups = [Value::Null, Value::Null];
        if matches!(case.kind, Kind::Warm | Kind::Refresh | Kind::Changed) {
            for (k, lane) in self.lanes.iter().enumerate() {
                let s = self.invoke(lane, &case.args, case.profile)?;
                warmups[k] = json!({"ms":s.ms,"exit":s.exit,"load":s.load});
            }
        }
        let mut lanes = [matched::Lane::default(), matched::Lane::default()];
        let mut loads = [vec![], vec![]];
        let mut exits_ms = [vec![], vec![]];
        let mut last = [Value::Null, Value::Null];
        let mut drains = [vec![], vec![]];
        for i in 0..runs {
            if case.kind == Kind::Changed {
                append_turn(
                    session.context("changed-source case without a session")?,
                    &format!("changed-{i}"),
                    &format!("active session update {i}"),
                )?;
            }
            for (k, lane) in self.lanes.iter().enumerate() {
                match case.kind {
                    Kind::FirstIndex => {
                        ended[k] += lane.end()?;
                        lane.reset()?;
                    }
                    Kind::Cold => ended[k] += lane.end()?,
                    Kind::Refresh => append_turn(
                        session.context("refresh case without a session")?,
                        &format!("refresh-{}-{i}", lane.name()),
                        &format!("one changed session incremental probe {} {i}", lane.name()),
                    )?,
                    Kind::Warm | Kind::Changed => {}
                }
                let s = self.invoke(lane, &case.args, case.profile)?;
                let lane_oracle = if lane.role == Role::Candidate {
                    oracle
                } else {
                    None
                };
                let mut verified = s.settled
                    && s.exit == case.expected_exit
                    && matched::sample_verified(&case.name, &case.args, &s.evidence, lane_oracle);
                if !verified {
                    self.diagnose(lane, &format!("{} sample {i}", case.name), &s);
                }
                if case.kind == Kind::Changed {
                    // The next pair starts from a refreshed index; a failed refresh fails the sample.
                    let drain = self.invoke(lane, &["index".into()], "classic")?;
                    if drain.exit != 0 || !drain.settled {
                        self.diagnose(lane, &format!("{} drain {i}", case.name), &drain);
                        verified = false;
                    }
                    drains[k].push(drain.ms);
                }
                lanes[k].record(s.ms, s.exit, verified);
                loads[k].push(s.load);
                exits_ms[k].push(s.exit_ms);
                last[k] = summary(&s.evidence);
                if lane_oracle.is_none() {
                    if let Some(o) = oracle {
                        last[k]["oracle_match"] = json!(matched::sample_verified(
                            &case.name,
                            &case.args,
                            &s.evidence,
                            Some(o)
                        ));
                    }
                }
            }
        }
        for (k, lane) in self.lanes.iter().enumerate() {
            ended[k] += lane.end()?;
        }
        let mut value = matched::verdict(&lanes[0], &lanes[1], case.expected_exit, runs);
        for (k, lane) in self.lanes.iter().enumerate() {
            let entry = &mut value[lane.name()];
            entry["samples_ms"] = json!(lanes[k].samples_ms);
            entry["exit_codes"] = json!(lanes[k].exit_codes);
            entry["verified"] = json!(lanes[k].verified);
            entry["load_averages"] = json!(loads[k]);
            if case.is_index() {
                entry["process_exit_ms"] = json!(exits_ms[k]);
            }
            if case.kind == Kind::Changed {
                entry["drain_ms"] = json!(drains[k]);
            }
            entry["warmup"] = warmups[k].clone();
            entry["last_result"] = last[k].clone();
            entry["background_processes_ended"] = json!(ended[k]);
        }
        value["name"] = json!(case.name);
        value["args"] = json!(case.shown);
        value["profile"] = json!(case.profile);
        value["expected_exit"] = json!(case.expected_exit);
        value["samples_per_lane"] = json!(runs);
        let line = format!(
            "{:28} ref {:>10.2} / {:>10.2}  cand {:>9.2} / {:>9.2} ms  ratio {:>6.3} / {:>6.3}  {}",
            case.name,
            value["reference"]["median_ms"].as_f64().unwrap_or(f64::NAN),
            value["reference"]["p95_ms"].as_f64().unwrap_or(f64::NAN),
            value["candidate"]["median_ms"].as_f64().unwrap_or(f64::NAN),
            value["candidate"]["p95_ms"].as_f64().unwrap_or(f64::NAN),
            value["median_ratio"].as_f64().unwrap_or(f64::NAN),
            value["p95_ratio"].as_f64().unwrap_or(f64::NAN),
            value["verdict"].as_str().unwrap_or("?"),
        );
        eprintln!("{line}");
        self.trace(&value)?;
        Ok(value)
    }

    /// Both lanes build the index of their stores, untimed.
    fn build(&self) -> Result<()> {
        for lane in &self.lanes {
            self.prepare(lane, &["index"])?;
            lane.end()?;
        }
        Ok(())
    }

    /// Untimed: both lanes reach a fully embedded corpus with the model on disk and no
    /// semantic process running.
    fn embed(&self, model_dir: Option<&Path>) -> Result<Value> {
        let candidate = self.candidate();
        if let Some(source) = model_dir {
            agrep_core::semantic_model::verify_installed_at(source)?;
            let destination = candidate
                .data
                .join("model")
                .join(agrep_core::semantic_model::MODEL_ID);
            fs::create_dir_all(&destination)?;
            for (name, _, _) in agrep_core::semantic_model::FILES {
                fs::copy(source.join(name), destination.join(name))?;
            }
        }
        let start = Instant::now();
        self.prepare(
            candidate,
            &["setup", "--yes", "--no-teach", "--no-hook", "--no-archive"],
        )?;
        reap::wait_idle(&candidate.reap, SETTLE)?;
        self.prepare(candidate, &["__work"])?;
        let candidate_ms = millis(start);
        let candidate_status = self.prepare(candidate, &["status", "--json", "--no-auto"])?;
        let start = Instant::now();
        self.prepare(self.reference(), &["reindex"])?;
        let reference_ms = millis(start);
        for lane in &self.lanes {
            lane.end()?;
        }
        Ok(json!({
            "candidate_embed_ms": candidate_ms,
            "reference_reindex_ms": reference_ms,
            "candidate_semantic_rows": candidate_status.evidence["semantic"]["indexed_rows"].clone(),
            "candidate_semantic_total": candidate_status.evidence["semantic"]["total_rows"].clone(),
            "candidate_semantic_backlog": candidate_status.evidence["semantic"]["backlog"].clone(),
        }))
    }
}

/// Numbers only: result counts, never text, handles or paths.
fn summary(evidence: &Value) -> Value {
    let mut value = json!({});
    for key in [
        "bench_result_rows",
        "bench_text_hits",
        "bench_count",
        "bench_semantic_rows",
        "bench_meaning_lines",
        "bench_stdout_bytes",
    ] {
        value[key.trim_start_matches("bench_")] = evidence[key].clone();
    }
    for key in ["indexed", "total"] {
        if let Some(n) = evidence["semantic_coverage"][key].as_u64() {
            value[format!("semantic_{key}")] = json!(n);
        }
    }
    value
}

fn version(bench: &Bench, lane: &Lane) -> Result<String> {
    let sample = bench.prepare(lane, &["--version"])?;
    lane.end()?;
    Ok(String::from_utf8_lossy(&sample.stdout)
        .lines()
        .next()
        .unwrap_or_default()
        .to_string())
}

fn keyword_cases(miss: &str, session: &str) -> Vec<Case> {
    let list: Vec<(&str, Vec<&str>)> = vec![
        ("release", vec!["release workflow"]),
        ("error", vec!["error"]),
        ("natural", vec![NATURAL]),
        ("flaky", vec!["flaky windows cache"]),
        ("rare", vec![miss]),
        ("short", vec!["hi"]),
        ("regex", vec!["-E", "release.*workflow"]),
        ("list", vec!["release workflow", "-l"]),
        ("json", vec!["release workflow", "--json"]),
        ("count", vec!["error", "-c"]),
        ("rare_hit", vec!["000d"]),
    ];
    let mut cases = Vec::new();
    for (name, args) in &list {
        cases.push(Case::new(name, args, "classic", Kind::Warm));
    }
    for (name, args) in list.iter().take(5) {
        cases.push(Case::new(
            &format!("compact_{name}"),
            args,
            "compact",
            Kind::Warm,
        ));
    }
    cases.push(Case::new(
        "rare_hit_pi_filtered",
        &["000d", "--agent", "pi", "--since", "90d"],
        "classic",
        Kind::Warm,
    ));
    for (name, args) in &list {
        let mut args = args.clone();
        args.extend(["--agent", "codex", "--since", "14d"]);
        cases.push(Case::new(
            &format!("{name}_filtered"),
            &args,
            "classic",
            Kind::Warm,
        ));
    }
    cases.push(Case::new(
        "tool_error",
        &["error", "--who", "tool", "--json"],
        "classic",
        Kind::Warm,
    ));
    cases.push(Case::new(
        "all_error",
        &[
            "error",
            "--who",
            "user,agent,subagent,recap,control,synthetic,harness,tool",
            "--json",
        ],
        "classic",
        Kind::Warm,
    ));
    for (name, args) in [
        ("recall", vec!["recall", "release workflow"]),
        (
            "pack",
            vec!["pack", "release workflow", "flaky windows cache"],
        ),
        ("chats", vec!["chats"]),
        ("status", vec!["status"]),
        ("around", vec!["around", session, "0"]),
    ] {
        cases.push(Case::new(name, &args, "classic", Kind::Warm));
    }
    cases
}

fn dense_cases() -> Vec<Case> {
    [
        ("old_dense", vec!["deepold", "--json", "-n", "40"]),
        (
            "old_dense_no_auto",
            vec!["deepold", "--json", "-n", "40", "--no-auto"],
        ),
        (
            "old_dense_word",
            vec!["deepold", "-w", "--json", "-n", "40"],
        ),
        (
            "old_dense_regex",
            vec!["-E", "deepold", "--json", "-n", "40"],
        ),
        (
            "old_dense_time",
            vec!["deepold", "--sort", "time", "--json", "-n", "40"],
        ),
        (
            "old_dense_agent",
            vec!["deepold", "--agent", "pi", "--json", "-n", "40"],
        ),
        (
            "old_dense_project",
            vec!["deepold", "--project", "project-7", "--json", "-n", "40"],
        ),
        (
            "old_dense_since",
            vec!["deepold", "--since", "400d", "--json", "-n", "40"],
        ),
        (
            "old_dense_phrase",
            vec!["deepold ordinary", "--json", "-n", "40"],
        ),
        ("old_dense_count", vec!["deepold", "-c"]),
    ]
    .iter()
    .map(|(name, args)| Case::new(name, args, "classic", Kind::Warm))
    .collect()
}

fn hybrid_cases(query: &str) -> Vec<Case> {
    vec![
        Case::new("first_hybrid", &[query], "compact", Kind::Cold),
        Case::new("warm_hybrid", &[query], "compact", Kind::Warm),
        Case::new(
            "semantic_only",
            &["-s", query, "--json"],
            "classic",
            Kind::Warm,
        ),
    ]
}

/// The candidate index's first session, as the benchmark picks it.
fn first_session(lane: &Lane) -> Result<String> {
    let reader = rusqlite::Connection::open_with_flags(
        lane.data.join("index.db"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    Ok(reader.query_row(
        "SELECT id FROM sessions ORDER BY agent,id LIMIT 1",
        [],
        |row| row.get(0),
    )?)
}

/// The most recently written pi/omp session under `home`: the active one.
fn active_session(home: &Path) -> Result<PathBuf> {
    let mut best: Option<(std::time::SystemTime, PathBuf)> = None;
    for root in [".omp/agent/sessions", ".pi/agent/sessions"] {
        let Ok(dirs) = fs::read_dir(home.join(root)) else {
            continue;
        };
        for dir in dirs.flatten() {
            let Ok(files) = fs::read_dir(dir.path()) else {
                continue;
            };
            for file in files.flatten() {
                let path = file.path();
                if path.extension().is_none_or(|e| e != "jsonl") {
                    continue;
                }
                let Ok(modified) = file.metadata().and_then(|m| m.modified()) else {
                    continue;
                };
                if best.as_ref().is_none_or(|(t, _)| modified > *t) {
                    best = Some((modified, path));
                }
            }
        }
    }
    best.map(|(_, p)| p)
        .context("no pi or omp session to copy for the refresh case")
}

/// A probe token no history can contain before this run.
fn absent_token() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    format!("zzmatched{:x}{:x}", nanos, std::process::id())
}

fn main() -> Result<()> {
    let a = Args::parse();
    ensure!(
        a.runs > 0 && a.build_runs > 0,
        "--runs and --build-runs must be positive"
    );
    let rows = a.rows.unwrap_or(match a.workload {
        Workload::Mixed => 100_000,
        Workload::Dense => 2_000_000,
        Workload::Semantic => 1_000,
        Workload::Real => 0,
    });
    if a.check {
        ensure!(
            a.runs >= 20 && a.build_runs >= 3,
            "the gate requires --runs >= 20 and --build-runs >= 3"
        );
        match a.workload {
            Workload::Mixed => ensure!(
                rows >= 100_000,
                "the mixed gate requires at least 100000 rows"
            ),
            Workload::Dense => ensure!(
                rows == 2_000_000,
                "the dense gate requires exactly 2000000 rows"
            ),
            Workload::Semantic => ensure!(
                rows >= 1_000,
                "the semantic gate requires at least 1000 rows"
            ),
            Workload::Real => bail!("the gate runs on synthetic workloads"),
        }
    }
    let candidate_bin = match &a.candidate {
        Some(path) => path.clone(),
        None => std::env::current_exe()?
            .parent()
            .and_then(Path::parent)
            .context("example location")?
            .join(if cfg!(windows) { "agrep.exe" } else { "agrep" }),
    };
    let candidate_bin = candidate_bin
        .canonicalize()
        .context("candidate executable")?;
    let reference_bin = a.reference.canonicalize().context("reference executable")?;
    reap::init()?;
    // 0.4.9 binds its resident socket under TMPDIR, so the root stays short on Unix.
    let scratch = if cfg!(unix) {
        tempfile::Builder::new().prefix("agm").tempdir_in("/tmp")?
    } else {
        tempfile::TempDir::new()?
    };
    let root = scratch.path();
    let outcome = measure(&a, root, &candidate_bin, &reference_bin, rows);
    let cleaned = cleanup(root, &candidate_bin);
    outcome.and(cleaned)
}

/// Ends whatever either lane left running, and removes the semantic coordination
/// directories 0.4.9 keeps for the reference data directories outside them.
fn cleanup(root: &Path, candidate: &Path) -> Result<()> {
    for (dir, is_candidate) in [
        ("reference-data", false),
        ("candidate-data", true),
        ("reference-active-data", false),
        ("candidate-active-data", true),
    ] {
        let data = root.join(dir);
        if !data.exists() {
            continue;
        }
        reap::end(&reap::Lane::new(&data, candidate, is_candidate), REAP)?;
        #[cfg(unix)]
        if !is_candidate {
            remove_namespace(&data)?;
        }
    }
    Ok(())
}

/// 0.4.9 names its per-data-directory semantic namespace in /tmp after a digest of the
/// directory's real path.
#[cfg(unix)]
fn remove_namespace(data: &Path) -> Result<()> {
    use sha2::{Digest, Sha256};
    use std::os::unix::ffi::OsStrExt;
    let uid = unsafe { libc::geteuid() };
    let mut hash = Sha256::new();
    hash.update(format!("v1\0{uid}\0"));
    hash.update(data.canonicalize()?.as_os_str().as_bytes());
    let digest: String = hash.finalize()[..10]
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    match fs::remove_dir_all(format!("/tmp/agrep-semantic-v1-{uid}-{digest}")) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
        _ => Ok(()),
    }
}

fn measure(
    a: &Args,
    root: &Path,
    candidate_bin: &Path,
    reference_bin: &Path,
    rows: usize,
) -> Result<()> {
    let only: Option<Vec<String>> = a
        .only
        .as_ref()
        .map(|s| s.split(',').map(|n| n.trim().to_string()).collect());
    let selected = |name: &str| only.as_ref().is_none_or(|o| o.iter().any(|n| n == name));
    let tmp = if cfg!(unix) {
        let tmp = root.join("t");
        fs::create_dir_all(&tmp)?;
        Some(tmp)
    } else {
        None
    };
    // Keyword cases run both lanes without a model; hybrid cases with both models.
    let keyword_model = root.join("reference-model");
    fs::create_dir_all(&keyword_model)?;
    let semantic_model = match &a.reference_model_dir {
        Some(dir) => {
            fs::create_dir_all(dir)?;
            dir.canonicalize()?
        }
        None => keyword_model.clone(),
    };
    let semantic = a.workload == Workload::Semantic;
    let (home, sources) = match a.workload {
        Workload::Real => {
            let sources = match &a.home {
                Some(home) => home.clone(),
                None => std::env::var_os("HOME").context("HOME")?.into(),
            };
            let shadow = root.join("shadow-home");
            fs::create_dir_all(&shadow)?;
            (shadow, sources)
        }
        _ => {
            let home = root.join("home");
            (home.clone(), home)
        }
    };
    let mut text_bytes = 0;
    let mut session = None;
    if a.workload != Workload::Real {
        let (last, generated) = corpus::generate(&home, rows, a.workload == Workload::Dense)?;
        text_bytes = generated;
        if semantic {
            append_turn(&last, "semantic-fixture", corpus::SEMANTIC_FIXTURE)?;
            text_bytes += corpus::SEMANTIC_FIXTURE.len() as u64;
        }
        session = Some(last);
    }
    let lanes = |suffix: &str, model: &Path| -> Result<[Lane; 2]> {
        Ok([
            Lane::new(
                Role::Reference,
                reference_bin,
                root.join(format!("reference{suffix}-data")),
                Some(model.to_path_buf()),
                candidate_bin,
            )?,
            Lane::new(
                Role::Candidate,
                candidate_bin,
                root.join(format!("candidate{suffix}-data")),
                None,
                candidate_bin,
            )?,
        ])
    };
    let trace = a
        .output
        .as_ref()
        .map(|p| PathBuf::from(format!("{}.events.jsonl", p.display())));
    if let Some(path) = &trace {
        fs::write(path, [])?;
    }
    let bench = Bench {
        lanes: lanes(
            "",
            if semantic {
                &semantic_model
            } else {
                &keyword_model
            },
        )?,
        home: home.clone(),
        sources: sources.clone(),
        tmp: tmp.clone(),
        private: a.workload == Workload::Real,
        trace: trace.clone(),
    };
    let versions = json!({
        "reference": version(&bench, bench.reference())?,
        "candidate": version(&bench, bench.candidate())?,
    });
    let source_bytes = if a.workload == Workload::Real {
        0
    } else {
        bytes(&home)
    };
    let mut cases = Vec::new();
    let mut required: Vec<String> = Vec::new();
    let mut preparation = json!({});
    let mut run = |bench: &Bench,
                   case: Case,
                   runs: usize,
                   session: Option<&Path>,
                   oracle: Option<&(Vec<String>, usize)>,
                   cases: &mut Vec<Value>|
     -> Result<()> {
        required.push(case.name.clone());
        if selected(&case.name) {
            cases.push(bench.run(&case, runs, session, oracle)?);
        }
        Ok(())
    };
    if semantic {
        bench.build()?;
        preparation = bench.embed(a.model_dir.as_deref())?;
        for case in hybrid_cases(corpus::SEMANTIC_QUERY) {
            let runs = if case.kind == Kind::Cold {
                a.build_runs
            } else {
                a.runs
            };
            run(&bench, case, runs, None, None, &mut cases)?;
        }
    } else {
        let first = Case::new("first_index", &["index"], "classic", Kind::FirstIndex);
        run(&bench, first, a.build_runs, None, None, &mut cases)?;
        if !selected("first_index") {
            bench.build()?;
        }
        let refresh = Case::new("no_change_index", &["index"], "classic", Kind::Warm);
        run(&bench, refresh, a.runs, None, None, &mut cases)?;
        if a.workload != Workload::Real {
            let source = session.as_deref();
            let refresh = Case::new("one_session_refresh", &["index"], "classic", Kind::Refresh);
            run(&bench, refresh, a.runs, source, None, &mut cases)?;
            let query = if a.workload == Workload::Dense {
                "deepold"
            } else {
                "release workflow"
            };
            let changed = Case::new(
                "search_changed_source",
                &[query, "--json"],
                "classic",
                Kind::Changed,
            );
            run(&bench, changed, a.runs, source, None, &mut cases)?;
        }
        if a.workload == Workload::Dense {
            for case in dense_cases() {
                let oracle = corpus::dense_expected(
                    rows,
                    &case.args.iter().map(String::as_str).collect::<Vec<_>>(),
                )?;
                run(&bench, case, a.runs, None, Some(&oracle), &mut cases)?;
            }
        } else {
            let real = a.workload == Workload::Real;
            let miss = if real {
                absent_token()
            } else {
                "zzperfnever_7f91c44".into()
            };
            let around = first_session(bench.candidate())?;
            for mut case in keyword_cases(&miss, &around) {
                if real {
                    for shown in &mut case.shown {
                        if *shown == miss {
                            *shown = "<absent token>".into();
                        } else if *shown == around {
                            *shown = "<first session>".into();
                        }
                    }
                }
                run(&bench, case, a.runs, None, None, &mut cases)?;
            }
            if real && a.model_dir.is_some() && a.reference_model_dir.is_some() {
                let hybrid = Bench {
                    lanes: lanes("", &semantic_model)?,
                    home: home.clone(),
                    sources: sources.clone(),
                    tmp: tmp.clone(),
                    private: true,
                    trace: trace.clone(),
                };
                preparation = hybrid.embed(a.model_dir.as_deref())?;
                for case in hybrid_cases(NATURAL) {
                    let runs = if case.kind == Kind::Cold {
                        a.build_runs
                    } else {
                        a.runs
                    };
                    run(&hybrid, case, runs, None, None, &mut cases)?;
                }
            }
        }
        run(
            &bench,
            Case::new("version_control", &["--version"], "classic", Kind::Warm),
            a.runs,
            None,
            None,
            &mut cases,
        )?;
        if a.workload == Workload::Real && selected("one_session_refresh") {
            // The refresh case appends to a copy of the active session in its own stores.
            let active = active_session(&sources)?;
            let relative = active.strip_prefix(&sources)?;
            let copy_home = root.join("active-home");
            let copy = copy_home.join(relative);
            fs::create_dir_all(copy.parent().context("session directory")?)?;
            fs::copy(&active, &copy)?;
            preparation["active_session_bytes"] = json!(bytes(&copy));
            let active_bench = Bench {
                lanes: lanes("-active", &keyword_model)?,
                home: copy_home.clone(),
                sources: copy_home,
                tmp: tmp.clone(),
                private: true,
                trace: trace.clone(),
            };
            active_bench.build()?;
            let refresh = Case::new("one_session_refresh", &["index"], "classic", Kind::Refresh);
            run(
                &active_bench,
                refresh,
                a.runs,
                Some(&copy),
                None,
                &mut cases,
            )?;
        }
    }
    for lane in &bench.lanes {
        lane.end()?;
    }
    let required: Vec<&str> = required.iter().map(String::as_str).collect();
    let failures = matched::gate(&cases, &required);
    let count = |verdict: &str| cases.iter().filter(|c| c["verdict"] == verdict).count();
    let report = json!({
        "workload": match a.workload {
            Workload::Mixed => "mixed",
            Workload::Dense => "dense",
            Workload::Semantic => "semantic",
            Workload::Real => "real",
        },
        "requested_rows": rows,
        "text_bytes": text_bytes,
        "source_bytes": source_bytes,
        "runs": a.runs,
        "build_runs": a.build_runs,
        "versions": versions,
        "host": {"os": std::env::consts::OS, "arch": std::env::consts::ARCH,
                 "cpus": std::thread::available_parallelism().map_or(0, |n| n.get())},
        "scope": "fresh processes through full exit, reference then candidate per sample, every sample kept; index samples of the reference end when its queued search-database build is served; one recorded warm-up per lane before warm cases; background processes of both lanes ended between cases",
        "gate": "every case: candidate median < reference median and candidate p95 < reference p95, with every sample of both lanes verified",
        "preparation": preparation,
        "summary": {
            "cases": cases.len(),
            "wins": count("win"),
            "losses": count("loss"),
            "reference_failures": count("reference_failure"),
            "candidate_failures": count("candidate_failure"),
            "both_failed": count("both_failed"),
        },
        "gate_failures": failures,
        "cases": cases,
    });
    let text = serde_json::to_string_pretty(&report)?;
    if let Some(path) = &a.output {
        fs::write(path, &text)?;
    } else {
        println!("{text}");
    }
    eprintln!(
        "{} cases: {} wins, {} losses, {} reference failures, {} candidate failures, {} both failed",
        report["summary"]["cases"], report["summary"]["wins"], report["summary"]["losses"],
        report["summary"]["reference_failures"], report["summary"]["candidate_failures"],
        report["summary"]["both_failed"]
    );
    if a.check && !failures.is_empty() {
        bail!("matched gate failed:\n{}", failures.join("\n"));
    }
    Ok(())
}
