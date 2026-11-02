//! Full-process benchmark. All generated agent stores and indexes are disposable.
use anyhow::{bail, Result};
use clap::Parser;
use serde_json::{json, Value};
use std::{
    fs::{self, File},
    io::{BufWriter, Write},
    path::{Path, PathBuf},
    time::Instant,
};
use tempfile::TempDir;
#[path = "support/perf.rs"]
mod perf;
#[path = "support/process.rs"]
mod process;

static TRACE: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
fn trace(value: &Value) -> Result<()> {
    if let Some(path) = TRACE.get() {
        let mut f = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        writeln!(f, "{value}")?;
    }
    Ok(())
}
#[derive(Parser)]
struct Args {
    #[arg(long)]
    binary: Option<PathBuf>,
    #[arg(long, default_value_t = 100_000)]
    rows: usize,
    #[arg(long, default_value_t = 5)]
    runs: usize,
    #[arg(long)]
    build_runs: Option<usize>,
    #[arg(long)]
    dense: bool,
    /// Install the model, drain this synthetic corpus, and measure hybrid startup.
    #[arg(long, conflicts_with_all = ["real", "dense"])]
    semantic: bool,
    #[arg(long)]
    real: bool,
    /// Re-measure an existing isolated real-history index after a code change.
    #[arg(long, requires = "real")]
    reuse: bool,
    #[arg(long)]
    home: Option<PathBuf>,
    #[arg(long)]
    data: Option<PathBuf>,
    #[arg(long)]
    layout: bool,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long)]
    check: bool,
}
fn utterance(i: usize, tool: bool, dense: bool) -> String {
    if dense {
        return format!("ticket {i:08} deepold ordinary historical explanation with a single aligned occurrence");
    }
    const WORDS: &[&str] = &[
        "the",
        "build",
        "history",
        "project",
        "test",
        "code",
        "cache",
        "release",
        "error",
        "workflow",
        "windows",
        "flaky",
        "query",
        "how",
        "did",
        "go",
        "drafting",
        "last",
        "run",
        "design",
        "token",
        "file",
        "index",
        "stream",
        "request",
        "response",
        "result",
        "configuration",
        "permission",
        "path",
        "content",
        "safe",
        "read",
        "write",
        "model",
        "source",
        "revision",
        "worktree",
        "compile",
        "benchmark",
        "counter",
        "boundary",
        "unicode",
        "session",
        "tool",
        "message",
        "trace",
        "agent",
        "commit",
        "dependency",
        "package",
        "database",
        "active",
        "old",
        "new",
        "fast",
        "slow",
        "state",
        "analysis",
        "summary",
        "check",
        "concurrency",
        "hello",
    ];
    let target = if tool {
        match i % 20 {
            0 => 4000,
            1..=5 => 1500,
            _ => 450,
        }
    } else {
        match i % 20 {
            0 => 8000,
            1..=5 => 800,
            _ => 170,
        }
    };
    let mut state = (i as u64 + 71) * 0x9e3779b1;
    let mut s = format!("reference {i:08x}: ");
    while s.len() < target {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        s.push_str(WORDS[(state as usize) % WORDS.len()]);
        s.push(' ');
    }
    s
}
fn generate(home: &Path, rows: usize, dense: bool) -> Result<(PathBuf, u64)> {
    let base = home.join(".pi/agent/sessions/bench");
    fs::create_dir_all(&base)?;
    let sessions = (rows / 170).max(1);
    let mut last = PathBuf::new();
    let mut at = 0;
    let mut text_bytes = 0u64;
    for sid in 0..sessions {
        let codex = !dense && sid % 5 == 1;
        let file_base = if codex {
            home.join(".codex/sessions/bench")
        } else if !dense && sid % 10 != 0 {
            base.join(format!("2024-01-01T00-00-00-000Z_bench-{}", sid / 10 * 10))
        } else {
            base.clone()
        };
        fs::create_dir_all(&file_base)?;
        let path = file_base.join(format!(
            "{}2024-01-01T00-00-00-000Z_bench-{sid}.jsonl",
            if codex { "rollout-" } else { "" }
        ));
        let mut f = BufWriter::new(File::create(&path)?);
        let date = if dense {
            "2023-01-01T12:00:00Z".to_owned()
        } else {
            chrono::Utc::now().to_rfc3339()
        };
        writeln!(
            f,
            "{}",
            if codex {
                json!({"type":"session_meta","timestamp":date,"payload":{"id":format!("bench-{sid}"),"cwd":format!("/work/project-{}",sid%50)}})
            } else {
                json!({"type":"session","version":3,"id":format!("bench-{sid}"),"cwd":format!("/work/project-{}",sid%50),"timestamp":date})
            }
        )?;
        let n = (rows - at) / (sessions - sid);
        let mut parent: Option<String> = None;
        for j in 0..n {
            let assistant = !dense && j % 80 == 1;
            let tool = !dense && j % 9 != 0 && !assistant;
            let id = format!("e{j}");
            let text = utterance(at, tool, dense);
            text_bytes += text.len() as u64;
            if codex {
                if !assistant {
                    writeln!(
                        f,
                        "{}",
                        json!({"type":"event_msg","timestamp":date,"payload":{"type":"user_message","message":text}})
                    )?;
                }
                writeln!(
                    f,
                    "{}",
                    json!({"type":"response_item","timestamp":date,"payload":{"type":"message","role":if assistant{"assistant"}else{"user"},"content":[{"type":if assistant{"output_text"}else{"input_text"},"text":text}]}})
                )?;
            } else if tool {
                let call = format!("c{j}");
                let command = format!("inspect source {at}");
                text_bytes += command.len() as u64 + 1;
                writeln!(
                    f,
                    "{}",
                    json!({"type":"message","id":&call,"parentId":parent,"timestamp":date,"message":{"role":"assistant","content":[{"type":"toolCall","id":&id,"name":"shell","arguments":{"command":command}}]}})
                )?;
                writeln!(
                    f,
                    "{}",
                    json!({"type":"message","id":&id,"parentId":call,"timestamp":date,"message":{"role":"toolResult","toolCallId":&id,"toolName":"shell","content":[{"type":"text","text":text}]}})
                )?;
            } else {
                writeln!(
                    f,
                    "{}",
                    json!({"type":"message","id":&id,"parentId":parent,"timestamp":date,"message":{"role":if assistant{"assistant"}else{"user"},"content":text}})
                )?;
            }
            parent = Some(id);
            at += 1;
        }
        f.flush()?;
        if !codex {
            last = path;
        }
    }
    Ok((last, text_bytes))
}
fn invoke(
    bin: &Path,
    home: &Path,
    data: &Path,
    args: &[&str],
    profile: &str,
) -> Result<(f64, i32, Value)> {
    let t = Instant::now();
    let out = process::command(
        bin,
        home,
        data,
        profile,
        std::env::var_os("PATH").unwrap_or_default(),
        std::env::var_os("SystemRoot"),
    )
    .args(args)
    .output()?;
    let ms = t.elapsed().as_secs_f64() * 1000.;
    let rc = out.status.code().unwrap_or(2);
    let first = String::from_utf8_lossy(&out.stdout)
        .lines()
        .next()
        .map(str::to_string)
        .unwrap_or_default();
    let mut parsed: Value = serde_json::from_str(&first).unwrap_or_else(
        |_| json!({"stdout_bytes":out.stdout.len(),"stderr_bytes":out.stderr.len()}),
    );
    if !parsed.is_object() {
        parsed = json!({"value":parsed});
    }
    if parsed.is_object() {
        let semantic_rows = String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter_map(|line| serde_json::from_str::<Value>(line).ok())
            .filter(|row| {
                row.get("sem_score").is_some_and(|v| v.is_number()) || row["lane"] == "semantic"
            })
            .count();
        parsed["bench_semantic_rows"] = json!(semantic_rows);
        parsed["bench_stdout_bytes"] = json!(out.stdout.len());
        let objects: Vec<Value> = String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect();
        let rows = objects
            .iter()
            .filter(|r| r["kind"] != "agrep-meta" && r["session"].is_string())
            .count();
        parsed["bench_result_rows"] = json!(rows);
        parsed["bench_text_hits"] = json!(String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter(|line| perf::flat_hit(line)
                || line.contains('@')
                || line.contains('›')
                || line.contains('→')
                || line.contains('⚙')
                || line.contains("pi/user ")
                || line.contains("codex/user ")
                || line.starts_with("user: "))
            .count());
        parsed["bench_count"] = json!(String::from_utf8_lossy(&out.stdout)
            .trim()
            .parse::<u64>()
            .ok());
    }
    Ok((ms, rc, parsed))
}
fn case(
    bin: &Path,
    home: &Path,
    data: &Path,
    name: &str,
    args: &[&str],
    runs: usize,
) -> Result<Value> {
    let mut times = vec![];
    let mut codes = vec![];
    let mut evidence = Value::Null;
    let mut verified = vec![];
    for i in 0..runs {
        if name == "first_index" && i > 0 {
            fs::remove_dir_all(data)?;
        }
        let (ms, rc, p) = invoke(
            bin,
            home,
            data,
            args,
            if name.starts_with("compact_") {
                "compact"
            } else {
                "classic"
            },
        )?;
        verified.push(perf::valid_result(name, &p));
        times.push(ms);
        codes.push(rc);
        evidence = p;
    }
    let mut sorted = times.clone();
    sorted.sort_by(f64::total_cmp);
    let median = sorted[sorted.len() / 2];
    eprintln!("{name:24} {median:10.2} ms  rc={:?}", codes);
    let value = json!({"name":name,"args":args,"median_ms":median,"p95_ms":if runs>=20{Some(sorted[(runs*95).div_ceil(100)-1])}else{None},"samples_ms":times,"exit_codes":codes,"verified_results":verified,"evidence":evidence});
    trace(&value)?;
    Ok(value)
}
fn bytes(path: &Path) -> u64 {
    if path.is_dir() {
        fs::read_dir(path)
            .unwrap()
            .map(|e| bytes(&e.unwrap().path()))
            .sum()
    } else {
        fs::metadata(path).map_or(0, |m| m.len())
    }
}
fn layout(t: &Path) -> Result<Value> {
    let mut results = vec![];
    for (name, tokenizer, detail) in [
        ("trigram-full", "trigram", "full"),
        ("trigram-none", "trigram", "none"),
        ("word-full", "unicode61", "full"),
        ("trigram-dual", "trigram", "full"),
    ] {
        let p = t.join(format!("{name}.db"));
        let mut c = rusqlite::Connection::open(&p)?;
        let columns = if name == "trigram-dual" {
            "text,joined"
        } else {
            "text"
        };
        c.execute_batch(&format!("PRAGMA journal_mode=OFF; CREATE VIRTUAL TABLE f USING fts5({columns},tokenize='{tokenizer}',detail={detail});"))?;
        let start = Instant::now();
        let tx = c.transaction()?;
        {
            let mut insert = tx.prepare(if name == "trigram-dual" {
                "INSERT INTO f VALUES(?1,?2)"
            } else {
                "INSERT INTO f VALUES(?1)"
            })?;
            for i in 0..20_000 {
                let text = utterance(i, i % 9 != 0, false);
                if name == "trigram-dual" {
                    insert.execute(rusqlite::params![text, agrep_core::store::joined(&text)])?;
                } else {
                    insert.execute([text])?;
                }
            }
        }
        tx.commit()?;
        let build = start.elapsed().as_secs_f64() * 1000.;
        let mut measurements = vec![];
        for needle in ["error", "release AND workflow", "ork", "hi"] {
            let expression = if tokenizer == "trigram" && detail == "none" && needle == "error" {
                "err AND rro AND ror"
            } else if tokenizer == "trigram" && detail == "none" && needle == "release AND workflow"
            {
                "rel AND ele AND lea AND eas AND ase AND wor AND ork AND rkf AND kfl AND flo AND low"
            } else {
                needle
            };
            let mut ms = vec![];
            let mut count = 0;
            for _ in 0..5 {
                let now = Instant::now();
                count = c
                    .query_row(
                        "SELECT count(*) FROM f WHERE f MATCH ?",
                        [expression],
                        |r| r.get::<_, i64>(0),
                    )
                    .unwrap_or(-1);
                ms.push(now.elapsed().as_secs_f64() * 1000.);
            }
            ms.sort_by(f64::total_cmp);
            measurements.push(json!({"query":needle,"count":count,"median_ms":ms[2]}));
        }
        drop(c);
        results
            .push(json!({"layout":name,"build_ms":build,"bytes":bytes(&p),"queries":measurements}));
        fs::remove_file(p)?;
    }
    Ok(json!(results))
}
fn main() -> Result<()> {
    let a = Args::parse();
    if a.runs == 0 || a.build_runs == Some(0) {
        bail!("--runs must be positive")
    }
    if let Some(path) = &a.output {
        let path = PathBuf::from(format!("{}.events.jsonl", path.display()));
        fs::write(&path, [])?;
        let _ = TRACE.set(path);
    }
    let temp = TempDir::new()?;
    if a.layout {
        let r = layout(temp.path())?;
        println!("{}", serde_json::to_string_pretty(&r)?);
        if let Some(p) = a.output {
            fs::write(p, serde_json::to_vec_pretty(&r)?)?;
        }
        return Ok(());
    }
    let bin = a.binary.unwrap_or(
        std::env::current_exe()?
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("agrep"),
    );
    let home = a.home.unwrap_or_else(|| {
        if a.real {
            std::env::var_os("HOME").unwrap().into()
        } else {
            temp.path().join("home")
        }
    });
    let data = a.data.unwrap_or(temp.path().join("data"));
    if !a.reuse && data.exists() && fs::read_dir(&data)?.next().is_some() {
        bail!("benchmark data directory must be empty")
    }
    if a.reuse && !data.join("index.db").is_file() {
        bail!("--reuse requires an existing index.db")
    }
    let (last, mut text_bytes) = if a.real {
        (None, 0)
    } else {
        let (last, text_bytes) = generate(&home, a.rows, a.dense)?;
        (Some(last), text_bytes)
    };
    if a.semantic {
        let path = last.as_ref().expect("synthetic semantic source");
        let mut source = fs::OpenOptions::new().append(true).open(path)?;
        let fixture = "The cobalt lantern project's migration checklist is stored beside its release plan. We agreed to preserve the deployment instructions there.";
        text_bytes += fixture.len() as u64;
        writeln!(
            source,
            "{}",
            json!({"type":"message","id":"semantic-fixture","timestamp":chrono::Utc::now().to_rfc3339(),"message":{"role":"user","content":fixture}})
        )?;
    }
    let source_bytes = if a.real { 0 } else { bytes(&home) };
    let mut cases = vec![];
    if !a.reuse {
        cases.push(case(
            &bin,
            &home,
            &data,
            "first_index",
            &["index"],
            a.build_runs.unwrap_or(a.runs),
        )?);
        if cases[0]["exit_codes"][0] != 0 {
            bail!("index failed: {}", cases[0])
        }
    }
    cases.push(case(
        &bin,
        &home,
        &data,
        "no_change_census",
        &["index"],
        a.runs,
    )?);
    if let Some(path) = &last {
        let mut trials = vec![];
        for i in 0..a.runs {
            let body = fs::read_to_string(path)?;
            let parent: Value = serde_json::from_str(body.lines().last().unwrap())?;
            {
                let mut f = fs::OpenOptions::new().append(true).open(path)?;
                writeln!(
                    f,
                    "{}",
                    json!({"type":"message","id":format!("incremental-{i}"),"parentId":parent["id"],"timestamp":chrono::Utc::now().to_rfc3339(),"message":{"role":"user","content":format!("one changed session incremental probe {i}")}})
                )?;
            }
            trials.push(invoke(&bin, &home, &data, &["index"], "classic")?);
        }
        let times: Vec<_> = trials.iter().map(|t| t.0).collect();
        let mut sorted = times.clone();
        sorted.sort_by(f64::total_cmp);
        let median = sorted[sorted.len() / 2];
        eprintln!("one_session_refresh {median:.2} ms");
        let value = json!({"name":"one_session_refresh","args":["index"],"median_ms":median,"samples_ms":times,"exit_codes":trials.iter().map(|t|t.1).collect::<Vec<_>>(),"evidence":trials.last().unwrap().2});
        trace(&value)?;
        cases.push(value);
    }
    if !a.real {
        let path = last.as_ref().expect("synthetic pi source");
        let mut trials = vec![];
        let query = if a.dense {
            "deepold"
        } else {
            "release workflow"
        };
        for i in 0..a.runs {
            let mut f = fs::OpenOptions::new().append(true).open(path)?;
            writeln!(
                f,
                "{}",
                json!({"type":"message","id":format!("pending-{i}"),"timestamp":chrono::Utc::now().to_rfc3339(),"message":{"role":"user","content":format!("active session update {i}")}})
            )?;
            drop(f);
            trials.push(invoke(&bin, &home, &data, &[query, "--json"], "classic")?);
            let (_, rc, _) = invoke(&bin, &home, &data, &["index"], "classic")?;
            if rc != 0 {
                bail!("changed-source drain failed");
            }
        }
        let mut times: Vec<_> = trials.iter().map(|t| t.0).collect();
        let samples = times.clone();
        times.sort_by(f64::total_cmp);
        let value = json!({"name":"search_changed_source","median_ms":times[times.len()/2],"p95_ms":if times.len()>=20 { Some(times[(times.len()*95).div_ceil(100)-1]) }else{None},"samples_ms":samples,"exit_codes":trials.iter().map(|t|t.1).collect::<Vec<_>>(),"verified_results":trials.iter().map(|t|perf::valid_result("json", &t.2)).collect::<Vec<_>>()});
        trace(&value)?;
        cases.push(value);
    }
    if a.semantic {
        for args in [
            vec!["setup", "--yes", "--no-teach", "--no-hook", "--no-archive"],
            vec!["__work"],
        ] {
            let (_, rc, evidence) = invoke(&bin, &home, &data, &args, "classic")?;
            if rc != 0 {
                bail!("semantic preparation failed: {args:?}: {evidence}");
            }
        }
        let args = [
            "where did we save the cobalt lantern deployment checklist",
            "--json",
        ];
        cases.push(case(&bin, &home, &data, "compact_first_hybrid", &args, 1)?);
        cases.push(case(
            &bin,
            &home,
            &data,
            "compact_warm_hybrid",
            &args,
            a.runs,
        )?);
    } else if a.dense {
        cases.push(case(
            &bin,
            &home,
            &data,
            "old_dense",
            &["deepold", "--json", "-n", "40"],
            a.runs,
        )?);
        cases.push(case(
            &bin,
            &home,
            &data,
            "old_dense_no_auto",
            &["deepold", "--json", "-n", "40", "--no-auto"],
            a.runs,
        )?);
    } else {
        let list: Vec<(&str, Vec<&str>)> = vec![
            ("release", vec!["release workflow"]),
            ("error", vec!["error"]),
            ("natural", vec!["how did the drafting go last run"]),
            ("flaky", vec!["flaky windows cache"]),
            ("rare", vec!["zzperfnever_7f91c44"]),
            ("short", vec!["hi"]),
            ("regex", vec!["-E", "release.*workflow"]),
            ("list", vec!["release workflow", "-l"]),
            ("json", vec!["release workflow", "--json"]),
            ("count", vec!["error", "-c"]),
            ("rare_hit", vec!["000d"]),
        ];
        for (name, args) in &list {
            cases.push(case(&bin, &home, &data, name, args, a.runs)?);
        }
        for (name, args) in list.iter().take(5) {
            cases.push(case(
                &bin,
                &home,
                &data,
                &format!("compact_{name}"),
                args,
                a.runs,
            )?);
        }
        cases.push(case(
            &bin,
            &home,
            &data,
            "rare_hit_pi_filtered",
            &["000d", "--agent", "pi", "--since", "90d"],
            a.runs,
        )?);
        for (name, mut args) in list {
            args.extend(["--agent", "codex", "--since", "14d"]);
            cases.push(case(
                &bin,
                &home,
                &data,
                &format!("{name}_filtered"),
                &args,
                a.runs,
            )?);
        }
        cases.push(case(
            &bin,
            &home,
            &data,
            "tool_error",
            &["error", "--who", "tool", "--json"],
            a.runs,
        )?);
        cases.push(case(
            &bin,
            &home,
            &data,
            "all_error",
            &[
                "error",
                "--who",
                "user,agent,subagent,recap,control,synthetic,harness,tool",
                "--json",
            ],
            a.runs,
        )?);
        let reader = rusqlite::Connection::open_with_flags(
            data.join("index.db"),
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        let session: String = reader.query_row(
            "SELECT id FROM sessions ORDER BY agent,id LIMIT 1",
            [],
            |row| row.get(0),
        )?;
        for (name, args) in [
            ("recall", vec!["recall", "release workflow"]),
            (
                "pack",
                vec!["pack", "release workflow", "flaky windows cache"],
            ),
            ("chats", vec!["chats"]),
            ("status", vec!["status"]),
            ("around", vec!["around", session.as_str(), "0"]),
        ] {
            cases.push(case(&bin, &home, &data, name, &args, a.runs)?);
        }
    }
    cases.push(case(
        &bin,
        &home,
        &data,
        "version_control",
        &["--version"],
        a.runs,
    )?);
    let corpus = invoke(
        &bin,
        &home,
        &data,
        &["status", "--json", "--no-auto"],
        "classic",
    )?
    .2;
    let failures = if a.check {
        perf::check(&cases, a.dense, a.semantic, a.rows, text_bytes)
            .err()
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let report = json!({"corpus":corpus,"scope":"fresh process through full exit, no discarded warm-up, normal refresh, nice inherited","real_history":a.real,"reuse":a.reuse,"dense":a.dense,"requested_rows":a.rows,"source_bytes":source_bytes,"text_bytes":text_bytes,"index_bytes":bytes(&data),"cases":cases});
    println!("{}", serde_json::to_string_pretty(&report)?);
    if let Some(p) = a.output {
        fs::write(p, serde_json::to_vec_pretty(&report)?)?;
    }
    if !failures.is_empty() {
        bail!("performance budgets exceeded: {}", failures.join("; "))
    }
    Ok(())
}
