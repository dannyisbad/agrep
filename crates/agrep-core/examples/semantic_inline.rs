//! Real CPU model proof and timing for the command-local semantic overlay.
//! Requires an explicitly provisioned model and CLI; all stores are temporary.
use agrep_core::{
    data::DataRoot,
    semantic_model::Embedder,
    semantic_store::{self, Block},
    store,
};
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{path::Path, process::Command, time::Instant};

fn transcript(home: &Path, id: usize, text: &str) -> Result<()> {
    let dir = home.join(".pi/agent/sessions/inline");
    std::fs::create_dir_all(&dir)?;
    let session = format!("inline-{id}");
    std::fs::write(
        dir.join(format!("{session}.jsonl")),
        format!(
            "{}\n{}\n",
            json!({"type":"session","id":session,"version":3,"cwd":"/fixture","timestamp":"2026-10-29T10:00:00Z"}),
            json!({"type":"message","id":"m0","parentId":null,"timestamp":"2026-10-29T10:01:00Z","message":{"role":"user","content":[{"type":"text","text":text}]}})
        ),
    )?;
    Ok(())
}
fn insert(root: &DataRoot, id: usize, text: &str, binary: &Path, home: &Path) -> Result<()> {
    transcript(home, id, text)?;
    // Only ingestion runs here: the unavailable model root prevents a worker
    // from racing the next-query proof's deliberately undrained row.
    let out = cli(
        binary,
        &home.join("uninstalled-model"),
        home,
        root,
        &["index"],
    )?;
    if !out.status.success() {
        bail!(
            "fixture index failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    Ok(())
}
fn cli(
    binary: &Path,
    model: &Path,
    home: &Path,
    root: &DataRoot,
    args: &[&str],
) -> Result<std::process::Output> {
    let mut cmd = Command::new(binary);
    for (key, _) in std::env::vars_os().filter(|(k, _)| k.to_string_lossy().starts_with("AGREP_")) {
        cmd.env_remove(key);
    }
    cmd.env("HOME", home)
        .env("USERPROFILE", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", root.path())
        .env("AGREP_MODEL_DIR", model)
        .env("AGREP_PROFILE", "compact")
        .args(args);
    Ok(cmd.output()?)
}
fn load() -> String {
    Command::new("uptime")
        .output()
        .ok()
        .map(|v| String::from_utf8_lossy(&v.stdout).trim().to_owned())
        .unwrap_or_default()
}
// The previous production path, retained only as the timing comparator.
fn previous(model: &Embedder, texts: &[String]) -> Result<Vec<Vec<f32>>> {
    let stream = |offset| {
        texts
            .iter()
            .skip(offset)
            .step_by(2)
            .map(|text| {
                model
                    .embed(std::slice::from_ref(text))
                    .map(|mut v| v.remove(0))
            })
            .collect::<Result<Vec<_>>>()
    };
    let (a, b) = rayon::join(|| stream(0), || stream(1));
    let (mut a, mut b) = (a?.into_iter(), b?.into_iter());
    Ok((0..texts.len())
        .map(|i| {
            if i % 2 == 0 {
                a.next().unwrap()
            } else {
                b.next().unwrap()
            }
        })
        .collect())
}
fn chunks() -> Result<Vec<String>> {
    let batch: Vec<String> =
        serde_json::from_str(include_str!("../../../fixtures/semantic-batch.json"))?;
    Ok(batch
        .iter()
        .flat_map(|s| agrep_core::semantic_model::chunks("codex", "user", s))
        .collect())
}
fn vector_digest(vectors: &[Vec<f32>]) -> String {
    let mut hash = Sha256::new();
    for value in vectors.iter().flatten() {
        hash.update(value.to_bits().to_le_bytes());
    }
    hash.finalize().iter().map(|b| format!("{b:02x}")).collect()
}
fn fresh(model_dir: &Path, backend: &str, n: usize, sample: usize) -> Result<()> {
    if backend == "worker" {
        agrep_core::semantic::configure_background()?;
    }
    let chunks = chunks()?;
    let start = sample % (chunks.len() - 20);
    let texts = &chunks[start..start + n];
    let tick = Instant::now();
    let device = if backend == "metal" {
        let Some(device) = agrep_core::semantic_device::metal_device() else {
            println!("{}", json!({"available":false}));
            return Ok(());
        };
        device
    } else {
        candle_core::Device::Cpu
    };
    let model = if backend == "previous" {
        Embedder::load_query(model_dir)?
    } else {
        Embedder::load(model_dir, device)?
    };
    let setup_ms = tick.elapsed().as_secs_f64() * 1000.;
    let (vectors, embed_ms) = timed(|| {
        if backend == "previous" {
            previous(&model, texts)
        } else {
            model.embed(texts)
        }
    })?;
    let (_, query_ms) = timed(|| model.embed(&["connection pool timeout".into()]))?;
    println!(
        "{}",
        json!({"available":true,"setup_ms":setup_ms,"embed_ms":embed_ms,"query_ms":query_ms,"total_ms":tick.elapsed().as_secs_f64()*1000.,"vector_sha256":vector_digest(&vectors)})
    );
    Ok(())
}
fn child(model_dir: &Path, backend: &str, n: usize, sample: usize) -> Result<Value> {
    let mut cmd = Command::new(std::env::current_exe()?);
    // Reproduce the old default, before the CPU bulk loader bounds BLAS.
    if backend == "previous" {
        cmd.env_remove("VECLIB_MAXIMUM_THREADS");
    }
    let out = cmd
        .arg(model_dir)
        .args(["__sample", backend, &n.to_string(), &sample.to_string()])
        .output()?;
    if !out.status.success() {
        bail!(
            "fresh {backend} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    Ok(serde_json::from_slice(&out.stdout)?)
}
fn timed(f: impl FnOnce() -> Result<Vec<Vec<f32>>>) -> Result<(Vec<Vec<f32>>, f64)> {
    let tick = Instant::now();
    let vectors = f()?;
    Ok((vectors, tick.elapsed().as_secs_f64() * 1000.))
}

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let model_dir = Path::new(
        args.get(1)
            .context("usage: semantic_inline MODEL_DIR CLI [BASELINE_CLI]")?,
    )
    .canonicalize()?;
    if args.get(2).is_some_and(|v| v == "__sample") {
        return fresh(&model_dir, &args[3], args[4].parse()?, args[5].parse()?);
    }
    let binary = Path::new(args.get(2).context("CLI required")?).canonicalize()?;
    let baseline = args
        .get(3)
        .map(|s| Path::new(s).canonicalize())
        .transpose()?;
    let started = Instant::now();
    let model = Embedder::load(&model_dir, candle_core::Device::Cpu)?;
    let load_ms = started.elapsed().as_secs_f64() * 1000.;
    let data: Value = serde_json::from_str(include_str!("../tests/fixtures/semantic-eval.json"))?;
    let texts: Vec<String> = serde_json::from_value(data["texts"].clone())?;
    let chunks = chunks()?;
    let temp = tempfile::tempdir()?;
    let root = DataRoot::open(temp.path().join("data"), true)?;
    store::writer(&root)?;
    insert(&root, 1, &texts[11], &binary, temp.path())?;
    let pending = semantic_store::batch(&store::reader(&root)?, 128)?;
    let initial = model.embed(&pending[0].chunks)?;
    let refs: Vec<_> = (0..initial.len())
        .map(|i| (pending[0].key.clone(), i))
        .collect();
    semantic_store::publish(&root, &pending, &refs, &initial)?;
    // The next command must supply the vector; the worker cannot satisfy this proof.
    let _work = root.work_lock(true)?;
    transcript(temp.path(), 2, &texts[10])?;
    let mut proof = Vec::new();
    for command in ["search", "recall", "pack"] {
        let out = cli(
            &binary,
            &model_dir,
            temp.path(),
            &root,
            &[command, "-s", "--json", &texts[0]],
        )?;
        let found = String::from_utf8_lossy(&out.stdout).contains("inline-2");
        if !out.status.success() || !found {
            bail!(
                "{command} missed newly queued prose: exit {:?}, stdout {}, stderr {}",
                out.status.code(),
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            );
        }
        let payload = if command == "search" {
            out.stdout.split(|b| *b == b'\n').next().unwrap_or_default()
        } else {
            &out.stdout
        };
        let value: Value = serde_json::from_slice(payload)?;
        if value["semantic_coverage"]["complete"] != true {
            bail!("{command} did not report a complete meaning lane");
        }
        proof.push(command);
    }
    let snapshot = store::reader(&root)?;
    if semantic_store::coverage(&snapshot)?.embedded != 1
        || semantic_store::block_list(&snapshot)?.len() != 1
    {
        bail!("query published command-local vectors");
    }
    drop(snapshot);
    let mut samples = Vec::new();
    let mut command_samples = Vec::new();
    for sample in 0..20 {
        let counts = if sample % 2 == 0 {
            vec![1, 2, 5, 10, 20]
        } else {
            vec![20, 10, 5, 2, 1]
        };
        for n in counts {
            let start = sample % (chunks.len() - 20);
            let texts = &chunks[start..start + n];
            let tokens: usize = texts
                .iter()
                .map(|s| model.token_ids(s).map(|v| v.len()))
                .sum::<Result<_>>()?;
            let uptime = load();
            let (old_fresh, cpu_fresh, metal_fresh) = if sample % 2 == 0 {
                let old = child(&model_dir, "previous", n, sample)?;
                let cpu = child(&model_dir, "cpu", n, sample)?;
                let metal = if sample < 3 {
                    child(&model_dir, "metal", n, sample)?
                } else {
                    json!({"measured":false})
                };
                (old, cpu, metal)
            } else {
                let metal = if sample < 3 {
                    child(&model_dir, "metal", n, sample)?
                } else {
                    json!({"measured":false})
                };
                let cpu = child(&model_dir, "cpu", n, sample)?;
                (child(&model_dir, "previous", n, sample)?, cpu, metal)
            };
            let before_ms = old_fresh["embed_ms"]
                .as_f64()
                .context("previous embedding time")?;
            let (vectors, embed_ms) = timed(|| model.embed_inline(texts))?;
            let published = model.embed(texts)?;
            if !vectors
                .iter()
                .flatten()
                .map(|v| v.to_bits())
                .eq(published.iter().flatten().map(|v| v.to_bits()))
            {
                bail!("inline CPU batching differs from publication batching");
            }
            if cpu_fresh["vector_sha256"] != vector_digest(&vectors) {
                bail!("inline vectors differ from a fresh CPU publication model");
            }
            if sample == 0
                && child(&model_dir, "worker", n, sample)?["vector_sha256"]
                    != vector_digest(&vectors)
            {
                bail!("inline vectors differ with the worker's CPU allowance");
            }
            let tick = Instant::now();
            let memory = Block::memory(&vectors)?;
            let memory_ms = tick.elapsed().as_secs_f64() * 1000.;

            let tick = Instant::now();
            let refs: Vec<_> = (0..vectors.len())
                .map(|i| (format!("benchmark-{sample}-{n}"), i))
                .collect();
            semantic_store::publish(&root, &[], &refs, &vectors)?;
            let publish_ms = tick.elapsed().as_secs_f64() * 1000.;
            let blocks = semantic_store::open_blocks(
                &root,
                semantic_store::block_list(&store::reader(&root)?)?,
            )?;
            if !blocks.iter().any(|b| b.map[..] == memory.map[..]) {
                bail!("inline block bytes differ from publication");
            }
            samples.push(json!({"sample":sample,"rows":n,"tokens":tokens,"embed_ms":embed_ms,"before_ms":before_ms,"old_fresh":old_fresh,"cpu_fresh":cpu_fresh,"metal_fresh":metal_fresh,"memory_ms":memory_ms,"publish_ms":publish_ms,"uptime":uptime}));
            eprintln!("{}", samples.last().unwrap());
        }
        let bins: Vec<_> = if sample % 2 == 0 {
            baseline.iter().chain(std::iter::once(&binary)).collect()
        } else {
            std::iter::once(&binary).chain(baseline.iter()).collect()
        };
        for bin in bins {
            for semantic_only in [sample % 2 == 0, sample % 2 != 0] {
                let uptime = load();
                let tick = Instant::now();
                let mut command = vec!["search", data["texts"][0].as_str().unwrap()];
                if semantic_only {
                    command.extend(["-s", "--json"]);
                }
                let out = cli(bin, &model_dir, temp.path(), &root, &command)?;
                let found = String::from_utf8_lossy(&out.stdout).contains("inline-2");
                let wall_ms = tick.elapsed().as_secs_f64() * 1000.;
                command_samples.push(json!({"sample":sample,"candidate":bin==&binary,"semantic_only":semantic_only,"wall_ms":wall_ms,"exit":out.status.code(),"found_new":found,"uptime":uptime}));
                eprintln!("{}", command_samples.last().unwrap());
                if bin == &binary && (!out.status.success() || !found) {
                    bail!(
                        "next-query proof failed: {}",
                        String::from_utf8_lossy(&out.stderr)
                    );
                }
            }
        }
    }
    println!(
        "{}",
        json!({"cpu":"F32 bulk CPU batching; previous query two-stream comparator","model_load_ms":load_ms,"next_query_proof":proof,"query_vectors_published":false,"samples":samples,"command_samples":command_samples})
    );
    Ok(())
}
