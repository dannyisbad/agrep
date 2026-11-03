//! Native-source 200k-row benchmark with deterministic, synthetic vector geometry.
//! Actual ingestion builds the keyword indexes and census; only model inference
//! is replaced by disclosed perturbations of the committed ONNX vectors.
use agrep_core::{data::DataRoot, semantic, semantic_model::DIM, store};
use anyhow::{Context, Result};
use rusqlite::params;
use std::{fs, io::Write, path::PathBuf};
fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let root = DataRoot::open(
        PathBuf::from(args.get(1).context(
            "usage: semantic_scale DATA_DIR [ROWS]; AGREP_HOME must be a temporary directory",
        )?),
        true,
    )?;
    let count: usize = args
        .get(2)
        .map(|s| s.parse())
        .transpose()?
        .unwrap_or(200000);
    let home = PathBuf::from(
        std::env::var_os("AGREP_HOME").context("set AGREP_HOME to an empty fixture HOME")?,
    );
    let source = home.join(".pi/agent/sessions/semantic-scale");
    fs::create_dir_all(&source)?;
    let data: serde_json::Value =
        serde_json::from_str(include_str!("../tests/fixtures/semantic-eval.json"))?;
    let docs: Vec<String> = serde_json::from_value(data["texts"].clone())?;
    let vectors: Vec<Vec<f32>> = serde_json::from_value(data["vectors"].clone())?;
    for start in (0..count).step_by(50) {
        let session = format!("fixture-{:05}", start / 50);
        let mut file =
            std::io::BufWriter::new(fs::File::create(source.join(format!("{session}.jsonl")))?);
        writeln!(
            file,
            "{}",
            serde_json::json!({"type":"session","id":session,"version":3,"cwd":"/fixture/semantic-scale","timestamp":"2026-10-29T10:00:00Z"})
        )?;
        for i in start..(start + 50).min(count) {
            let text = format!("{}\nExperiment {i}.", docs[i % 20 + 10]);
            writeln!(
                file,
                "{}",
                serde_json::json!({"type":"message","id":format!("m{i}"),"parentId":if i==start{None}else{Some(format!("m{}",i-1))},"timestamp":"2026-10-29T10:01:00Z","message":{"role":"user","content":[{"type":"text","text":text}],"timestamp":"2026-10-29T10:01:00Z"}})
            )?;
        }
        file.flush()?;
    }
    let refresh = store::refresh(&root, true, false)?;
    semantic::initialize(&root)?;
    let mut c = store::writer(&root)?;
    let rows: Vec<(i64, String, Vec<u8>)> = c
        .prepare("SELECT id,digest,text FROM prose WHERE visible=1 ORDER BY id")?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
        .collect::<rusqlite::Result<_>>()?;
    assert_eq!(rows.len(), count);
    fs::create_dir_all(root.path().join("vectors"))?;
    let tx = c.transaction()?;
    let mut bytes = Vec::with_capacity(512 * (DIM * 3 + 4));
    for start in (0..count).step_by(512) {
        bytes.clear();
        let block = format!("{start:064x}.q8f16");
        let end = (start + 512).min(count);
        for (slot, (id, digest, text)) in rows[start..end].iter().enumerate() {
            let text = store::decode("prose", text.clone())?;
            let i: usize = text
                .rsplit_once("Experiment ")
                .context("fixture marker")?
                .1
                .trim_end_matches('.')
                .parse()?;
            let mut random = (i as u64 + 1).wrapping_mul(104729);
            let mut v = vectors[i % 20 + 10].clone();
            for x in &mut v {
                random ^= random << 13;
                random ^= random >> 7;
                random ^= random << 17;
                *x += ((random >> 40) as f32 / 16777216. - 0.5) * 0.06;
            }
            let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt();
            v.iter_mut().for_each(|x| *x /= norm);
            let scale = v.iter().fold(0f32, |p, x| p.max(x.abs())) / 127.;
            bytes.extend_from_slice(&scale.to_le_bytes());
            bytes.extend(v.iter().map(|x| (x / scale).round() as i8 as u8));
            for x in &v {
                bytes.extend_from_slice(&half::f16::from_f32(*x).to_le_bytes());
            }
            tx.execute(
                "INSERT INTO semantic_refs VALUES(?,?,?)",
                params![id, digest, digest],
            )?;
            tx.execute(
                "INSERT INTO semantic_chunks VALUES(?,0,?,?)",
                params![digest, block, slot],
            )?;
        }
        let mut file = fs::File::create(root.path().join("vectors").join(&block))?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        tx.execute(
            "INSERT INTO semantic_blocks VALUES(?,?)",
            params![block, end - start],
        )?;
    }
    tx.execute("DELETE FROM semantic_queue", [])?;
    tx.commit()?;
    c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")?;
    println!(
        "{}",
        serde_json::json!({"rows":count,"kind":"native sources with synthetic ONNX-vector perturbations","refresh":refresh,"coverage":semantic::coverage(&store::reader(&root)?)?})
    );
    Ok(())
}
