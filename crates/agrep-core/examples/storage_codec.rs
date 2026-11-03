//! Aggregate-only codec measurements on an isolated index.
use agrep_core::{data::DataRoot, store};
use anyhow::Result;
use flate2::{read::ZlibDecoder, write::ZlibEncoder, Compression};
use serde_json::json;
use std::{
    io::{Read, Write},
    time::Instant,
};

fn load() -> Vec<f64> {
    let Ok(out) = std::process::Command::new("uptime").output() else {
        return vec![];
    };
    let text = String::from_utf8_lossy(&out.stdout);
    text.rsplit_once(':')
        .map(|(_, s)| {
            s.split_whitespace()
                .filter_map(|n| n.trim_end_matches(',').parse().ok())
                .collect()
        })
        .unwrap_or_default()
}

fn main() -> Result<()> {
    let path = std::env::args().nth(1).expect("isolated data directory");
    let root = DataRoot::open(std::path::Path::new(&path), false)?;
    if std::env::args().nth(2).as_deref() == Some("--rewrite-lz4") {
        let _lock = root.lock(true)?;
        let mut c = store::writer(&root)?;
        let tx = c.transaction()?;
        let rows = tx
            .prepare("SELECT id,body FROM tool_blobs")?
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        for (id, body) in &rows {
            let text = store::decode("tools", body.clone())?;
            let mut encoded = vec![0];
            encoded.extend(lz4_flex::compress_prepend_size(text.as_bytes()));
            tx.execute(
                "UPDATE tool_blobs SET body=?2 WHERE id=?1",
                rusqlite::params![id, encoded],
            )?;
        }
        tx.commit()?;
        println!("{}", json!({"rewritten_blobs":rows.len()}));
        return Ok(());
    }
    let c = store::reader(&root)?;
    let graph = std::env::args().nth(2).as_deref() == Some("--graph");
    let mut q = c.prepare(if graph {
        "SELECT body FROM parser_graph ORDER BY source,bucket"
    } else {
        "SELECT text FROM tools WHERE visible=1 ORDER BY id"
    })?;
    let encoded = q
        .query_map([], |r| r.get::<_, Vec<u8>>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let texts = encoded
        .iter()
        .map(|bytes| {
            if graph {
                let mut text = Vec::new();
                ZlibDecoder::new(&bytes[2..]).read_to_end(&mut text)?;
                Ok(text)
            } else {
                store::decode("tools", bytes.clone()).map(String::into_bytes)
            }
        })
        .collect::<Result<Vec<_>>>()?;
    let mut codecs = vec![];
    for level in [0, 1, 3, 6, 101, 102, 103, 104] {
        let encode_load = load();
        let start = Instant::now();
        let packed: Vec<Vec<u8>> = texts
            .iter()
            .map(|text| -> Result<_> {
                if level == 0 {
                    return Ok(lz4_flex::compress_prepend_size(text.as_slice()));
                }
                if level >= 100 {
                    let mut encoder =
                        brotli::CompressorWriter::new(Vec::new(), 16384, level - 100, 22);
                    encoder.write_all(text.as_slice())?;
                    return Ok(encoder.into_inner());
                }
                let mut encoder = ZlibEncoder::new(Vec::new(), Compression::new(level));
                encoder.write_all(text.as_slice())?;
                Ok(encoder.finish()?)
            })
            .collect::<Result<_>>()?;
        let encode_ms = start.elapsed().as_secs_f64() * 1000.;
        let mut times = vec![];
        let mut loads = vec![];
        for _ in 0..7 {
            loads.push(load());
            let start = Instant::now();
            for (body, original) in packed.iter().zip(&texts) {
                let decoded = if level == 0 {
                    lz4_flex::decompress_size_prepended(body)?
                } else if level >= 100 {
                    let mut out = Vec::new();
                    brotli::Decompressor::new(body.as_slice(), 16384).read_to_end(&mut out)?;
                    out
                } else {
                    let mut out = Vec::new();
                    ZlibDecoder::new(body.as_slice()).read_to_end(&mut out)?;
                    out
                };
                anyhow::ensure!(
                    decoded == original.as_slice(),
                    "codec round trip changed text"
                );
            }
            times.push(start.elapsed().as_secs_f64() * 1000.);
        }
        let mut sorted = times.clone();
        sorted.sort_by(f64::total_cmp);
        codecs.push(json!({"codec":if level==0 {"lz4"}else if level>=100 {"brotli"}else{"deflate"},"level":if level>=100 {level-100}else{level},"bytes":packed.iter().map(Vec::len).sum::<usize>(),"encode_ms":encode_ms,"encode_load_averages":encode_load,"decode_samples_ms":times,"decode_load_averages":loads,"decode_median_ms":sorted[3]}));
    }
    println!(
        "{}",
        json!({"graph":graph,"rows":texts.len(),"text_bytes":texts.iter().map(Vec::len).sum::<usize>(),"stored_bytes":encoded.iter().map(Vec::len).sum::<usize>(),"codecs":codecs})
    );
    Ok(())
}
