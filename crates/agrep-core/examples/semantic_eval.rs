//! Actual-model parity, labeled recall and bulk throughput. Requires an explicit
//! model directory; never discovers a user store or silently downloads a model.
use agrep_core::semantic_model::{Embedder, DIM};
use anyhow::{bail, Context, Result};
use candle_core::Device;
use serde_json::json;
use std::{path::Path, time::Instant};
fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let path = args
        .get(1)
        .context("usage: semantic_eval MODEL_DIR cpu|metal [batch]")?;
    let device = match args.get(2).map(String::as_str).unwrap_or("cpu") {
        "metal" => Device::new_metal(0)?,
        "cpu" => Device::Cpu,
        _ => bail!("expected cpu or metal"),
    };
    let started = Instant::now();
    let mut model = Embedder::load(Path::new(path), device)?;
    let window: usize = args.get(4).map(|s| s.parse()).transpose()?.unwrap_or(1024);
    model = model.with_token_limit(window)?;
    let load_s = started.elapsed().as_secs_f64();
    if args.get(3).is_some_and(|s| s == "batch") {
        let batch: Vec<String> =
            serde_json::from_str(include_str!("../../../fixtures/semantic-batch.json"))?;
        let batches: usize = args.get(5).map(|s| s.parse()).transpose()?.unwrap_or(16);
        if batches == 0 {
            bail!("batch count must be positive");
        }
        let start = Instant::now();
        let mut batch_seconds = Vec::new();
        let mut rows = 0;
        for _ in 0..batches {
            let tick = Instant::now();
            let v = model.embed(&batch)?;
            rows += v.len();
            batch_seconds.push(tick.elapsed().as_secs_f64());
        }
        let seconds = start.elapsed().as_secs_f64();
        println!(
            "{}",
            json!({"load_s":load_s,"rows":rows,"batches":batches,"rows_per_batch":batch.len(),"batch_seconds":batch_seconds,"inference_s":seconds,"rows_s":rows as f64/seconds})
        );
        return Ok(());
    }
    let data: serde_json::Value =
        serde_json::from_str(include_str!("../tests/fixtures/semantic-eval.json"))?;
    let texts: Vec<String> = serde_json::from_value(data["texts"].clone())?;
    let expected: Vec<Vec<f32>> = serde_json::from_value(data["vectors"].clone())?;
    let ids: Vec<Vec<u32>> = serde_json::from_value(data["ids"].clone())?;
    for (i, t) in texts.iter().enumerate() {
        if window == 1024 && model.token_ids(t)? != ids[i] {
            bail!("token ids differ at {i}");
        }
    }
    let vectors = model.embed(&texts)?;
    let mut minimum = 1f64;
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != DIM {
            bail!("wrong dimension");
        }
        let dot: f64 = v
            .iter()
            .zip(&expected[i])
            .map(|(a, b)| *a as f64 * *b as f64)
            .sum();
        let norm = |x: &[f32]| x.iter().map(|a| (*a as f64).powi(2)).sum::<f64>().sqrt();
        minimum = minimum.min(dot / (norm(v) * norm(&expected[i])));
    }
    let mut recall = 0;
    let mut agree = 0;
    for i in 0..10 {
        let rank = |vs: &Vec<Vec<f32>>| {
            let mut r: Vec<_> = (10..30)
                .map(|j| {
                    (
                        j,
                        vs[i]
                            .iter()
                            .zip(&vs[j])
                            .map(|(a, b)| *a as f64 * *b as f64)
                            .sum::<f64>(),
                    )
                })
                .collect();
            r.sort_by(|a, b| b.1.total_cmp(&a.1));
            r.iter().take(5).map(|h| h.0).collect::<Vec<_>>()
        };
        let actual = rank(&vectors);
        if actual.contains(&(i + 10)) {
            recall += 1;
        }
        if actual == rank(&expected) {
            agree += 1;
        }
    }
    // All ten answers fit one reference byte window, but their shared preamble
    // extends past token 128. This detects a shorter-window loss of tail evidence.
    let long_docs: Vec<_> = texts[10..20]
        .iter()
        .map(|t| format!("{}{t}", "We reviewed it. ".repeat(32)))
        .collect();
    let long_vectors = model.embed(&long_docs)?;
    let mut long_recall = 0;
    for (i, vector) in vectors.iter().take(10).enumerate() {
        let best = (0..10)
            .max_by(|&a, &b| {
                let dot = |j: usize| {
                    vector
                        .iter()
                        .zip(&long_vectors[j])
                        .map(|(x, y)| x * y)
                        .sum::<f32>()
                };
                dot(a).total_cmp(&dot(b))
            })
            .unwrap();
        if best == i {
            long_recall += 1;
        }
    }
    let reference: serde_json::Value = serde_json::from_str(include_str!(
        "../tests/fixtures/semantic-recall-reference.json"
    ))?;
    let tasks = reference["tasks"].as_array().unwrap();
    let mut inputs: Vec<String> = tasks
        .iter()
        .map(|t| t["query"].as_str().unwrap().into())
        .collect();
    inputs.extend(
        tasks
            .iter()
            .map(|t| t["semantic_text"].as_str().unwrap().into()),
    );
    let rs = model.embed(&inputs)?;
    let mut reference_recall = 0;
    for i in 0..tasks.len() {
        let best = (0..tasks.len())
            .max_by(|&a, &b| {
                let dot = |j: usize| {
                    rs[i]
                        .iter()
                        .zip(&rs[tasks.len() + j])
                        .map(|(x, y)| x * y)
                        .sum::<f32>()
                };
                dot(a).total_cmp(&dot(b))
            })
            .unwrap();
        if best == i {
            reference_recall += 1;
        }
    }
    println!(
        "{}",
        json!({"load_s":load_s,"total_s":started.elapsed().as_secs_f64(),"minimum_cosine":minimum,"recall_at_5":recall as f64/10.,"top5_agreement":agree,"ids_equal":window==1024,"window":window,"reference_recall_at_1":reference_recall as f64/tasks.len() as f64,"long_tail_recall_at_1":long_recall as f64/10.})
    );
    if args.get(2).is_none_or(|s| s == "cpu") && window == 1024 && minimum < 0.99999999999 {
        bail!("F32 model parity failed: cosine {minimum}");
    }
    if args.get(2).is_some_and(|s| s == "metal") && window == 1024 && minimum < 0.9999 {
        bail!("F16 model parity failed: cosine {minimum}");
    }
    if window == 1024
        && (recall != 10 || agree != 10 || reference_recall != tasks.len() || long_recall != 10)
    {
        bail!("semantic recall/neighbor gate failed");
    }
    Ok(())
}
