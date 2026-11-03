//! Aggregate-only storage experiments over an isolated index; never emits source identities.
use agrep_core::{ingest::pi, store};
use anyhow::Result;
use clap::Parser;
use rusqlite::{Connection, OpenFlags};
use serde_json::json;
use std::{fs, io::Write, path::PathBuf, time::Instant};
#[derive(Parser)]
struct Args {
    #[arg(long)]
    data: PathBuf,
    #[arg(long)]
    scratch: PathBuf,
    #[arg(long)]
    output: PathBuf,
    #[arg(long, default_value_t = 30_000)]
    rows: usize,
    #[arg(long)]
    nodes_only: bool,
}
fn baseline_args(term: &str) -> [&str; 1] {
    [match term {
        "release" => "release workflow",
        "drafting" => "how did the drafting go last run",
        other => other,
    }]
}
fn host_load() -> Vec<f64> {
    let Ok(out) = std::process::Command::new("uptime").output() else {
        return vec![];
    };
    let text = String::from_utf8_lossy(&out.stdout);
    let tail = text
        .split("load averages:")
        .nth(1)
        .or_else(|| text.split("load average:").nth(1))
        .unwrap_or("");
    tail.split(|c: char| c == ',' || c.is_whitespace())
        .filter_map(|s| s.parse().ok())
        .take(3)
        .collect()
}
fn main() -> Result<()> {
    let a = Args::parse();
    let c = Connection::open_with_flags(a.data.join("index.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let mut old_bytes = 0u64;
    let mut new_bytes = 0u64;
    let mut nodes = 0u64;
    let mut deflate_bytes = 0u64;
    let mut chunk_bytes = 0u64;
    let mut chunk = Vec::new();
    let mut source = -1i64;
    let individual_nodes: bool = c.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('parser_nodes') WHERE name='body')",
        [],
        |r| r.get(0),
    )?;
    let node_sql = if individual_nodes {
        "SELECT source,body FROM parser_nodes ORDER BY source,ordinal"
    } else {
        "SELECT 0,zeroblob(4) WHERE 0"
    };
    let compressed_size = |body: &[u8]| -> Result<u64> {
        let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::fast());
        encoder.write_all(body)?;
        Ok(encoder.finish()?.len() as u64)
    };
    for body in c
        .prepare(node_sql)?
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?)))?
    {
        let (next_source, body) = body?;
        old_bytes += body.len() as u64;
        let value = serde_json::from_slice(&lz4_flex::decompress_size_prepended(&body)?)?;
        let body = serde_json::to_vec(&pi::compact_record(value))?;
        if source != next_source || chunk.len() + body.len() > 1024 * 1024 {
            if !chunk.is_empty() {
                chunk_bytes += compressed_size(&chunk)?;
                chunk.clear();
            }
            source = next_source;
        }
        chunk.extend_from_slice(&body);
        new_bytes += lz4_flex::compress_prepend_size(&body).len() as u64;
        let mut compressed =
            flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::fast());
        compressed.write_all(&body)?;
        deflate_bytes += compressed.finish()?.len() as u64;
        nodes += 1;
    }
    if !chunk.is_empty() {
        chunk_bytes += compressed_size(&chunk)?;
    }
    if !individual_nodes {
        nodes = c.query_row("SELECT count(*) FROM parser_nodes", [], |r| r.get(0))?;
        chunk_bytes = c.query_row(
            "SELECT coalesce(sum(length(body)),0) FROM parser_chunks",
            [],
            |r| r.get(0),
        )?;
    }
    let count: usize = c.query_row("SELECT count(*) FROM tools WHERE visible=1", [], |r| {
        r.get(0)
    })?;
    let stride = count.div_ceil(a.rows).max(1);
    let mut texts = vec![];
    for (i, body) in c
        .prepare("SELECT text FROM tools WHERE visible=1 ORDER BY id")?
        .query_map([], |r| r.get::<_, Vec<u8>>(0))?
        .enumerate()
    {
        if i % stride == 0 {
            texts.push(store::normalize(&store::decode("tools", body?)?));
        }
    }
    fs::create_dir_all(&a.scratch)?;
    let mut layouts = vec![];
    for (name, tokenizer, detail) in [
        ("trigram_full", "trigram case_sensitive 1", "full"),
        ("trigram_none", "trigram case_sensitive 1", "none"),
        ("word_full", "unicode61 remove_diacritics 0", "full"),
        ("head_tail_full", "trigram case_sensitive 1", "full"),
        ("trigram_bulk", "trigram case_sensitive 1", "full"),
        ("word_bulk", "unicode61 remove_diacritics 0", "full"),
    ] {
        if a.nodes_only {
            break;
        }
        let p = a.scratch.join(format!("{name}.db"));
        let mut db = Connection::open(&p)?;
        db.execute_batch(&format!("PRAGMA journal_mode=OFF;CREATE VIRTUAL TABLE f USING fts5(text,content='',contentless_delete=1,tokenize='{tokenizer}',detail={detail});CREATE VIRTUAL TABLE v USING fts5vocab(f,row);"))?;
        let load = host_load();
        let start = Instant::now();
        let tx = db.transaction()?;
        if name.ends_with("_bulk") {
            tx.execute_batch("INSERT INTO f(f,rank) VALUES('automerge',0);INSERT INTO f(f,rank) VALUES('crisismerge',64);INSERT INTO f(f,rank) VALUES('hashsize',16777216);")?;
        }
        {
            let mut insert = tx.prepare("INSERT INTO f(text) VALUES(?)")?;
            for s in &texts {
                if name == "head_tail_full" && s.chars().count() > 1536 {
                    let head: String = s.chars().take(1024).collect();
                    let tail: String = s
                        .chars()
                        .rev()
                        .take(512)
                        .collect::<String>()
                        .chars()
                        .rev()
                        .collect();
                    insert.execute([format!("{head}\n{tail}")])?;
                } else {
                    insert.execute([s])?;
                }
            }
        }
        if name.ends_with("_bulk") {
            tx.execute("INSERT INTO f(f) VALUES('optimize')", [])?;
        }
        tx.commit()?;
        let build_ms = start.elapsed().as_secs_f64() * 1000.;
        let vocab: usize = db.query_row("SELECT count(*) FROM v", [], |r| r.get(0))?;
        let mut queries = vec![];
        for query in ["error", "release", "drafting", "000d"] {
            let expr = if detail == "none" {
                query
                    .as_bytes()
                    .windows(3)
                    .map(|g| format!("\"{}\"", std::str::from_utf8(g).unwrap()))
                    .collect::<Vec<_>>()
                    .join(" AND ")
            } else {
                format!("\"{query}\"")
            };
            let mut ms = vec![];
            let mut hits = 0;
            for _ in 0..7 {
                let start = Instant::now();
                hits = db.query_row("SELECT count(*) FROM f WHERE f MATCH ?", [&expr], |r| {
                    r.get::<_, usize>(0)
                })?;
                ms.push(start.elapsed().as_secs_f64() * 1000.);
            }
            ms.sort_by(f64::total_cmp);
            queries.push(json!({"args":baseline_args(query),"term_index":if query=="drafting"{3}else{0},"count":hits,"median_ms":ms[3]}));
        }
        if name == "word_full" {
            db.execute_batch("CREATE VIRTUAL TABLE lex USING fts5(word, tokenize='trigram case_sensitive 1', detail=none); INSERT INTO lex(word) SELECT term FROM v WHERE length(term)>=3;")?;
            let lex_bytes: usize = db.query_row(
                "SELECT sum(pgsize) FROM dbstat WHERE name LIKE 'lex_%'",
                [],
                |r| r.get(0),
            )?;
            let mut expanded = vec![];
            for needle in ["error", "release", "drafting", "000d"] {
                let grams = needle
                    .as_bytes()
                    .windows(3)
                    .map(|g| format!("\"{}\"", std::str::from_utf8(g).unwrap()))
                    .collect::<Vec<_>>()
                    .join(" AND ");
                let mut ms = vec![];
                let mut count = 0;
                let mut alternatives = 0;
                for _ in 0..7 {
                    let start = Instant::now();
                    let words = db
                        .prepare("SELECT word FROM lex WHERE lex MATCH ?1 AND instr(word,?2)>0")?
                        .query_map([&grams, needle], |r| r.get::<_, String>(0))?
                        .collect::<rusqlite::Result<Vec<_>>>()?;
                    alternatives = words.len();
                    if !words.is_empty() {
                        let expression = words
                            .iter()
                            .map(|s| format!("\"{}\"", s.replace('"', "\"\"")))
                            .collect::<Vec<_>>()
                            .join(" OR ");
                        count = db.query_row(
                            "SELECT count(*) FROM f WHERE f MATCH ?",
                            [expression],
                            |r| r.get::<_, usize>(0),
                        )?;
                    }
                    ms.push(start.elapsed().as_secs_f64() * 1000.);
                }
                ms.sort_by(f64::total_cmp);
                expanded.push(json!({"args":baseline_args(needle),"term_index":if needle=="drafting"{3}else{0},"count":count,"alternatives":alternatives,"median_ms":ms[3]}));
            }
            layouts.push(json!({"layout":"word_with_lexicon","rows":texts.len(),"lexicon_bytes":lex_bytes,"queries":expanded}));
        }
        drop(db);
        layouts.push(json!({"layout":name,"rows":texts.len(),"bytes":fs::metadata(&p)?.len(),"build_ms":build_ms,"vocabulary":vocab,"queries":queries,"load_averages":load}));
        fs::remove_file(p)?;
    }
    let out = json!({"nodes":nodes,"individual_nodes":individual_nodes,"old_node_payload_bytes":individual_nodes.then_some(old_bytes),"new_node_payload_bytes":individual_nodes.then_some(new_bytes),"deflate_node_payload_bytes":individual_nodes.then_some(deflate_bytes),"chunk_payload_bytes":chunk_bytes,"layouts":layouts});
    fs::write(a.output, serde_json::to_vec_pretty(&out)?)?;
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}
