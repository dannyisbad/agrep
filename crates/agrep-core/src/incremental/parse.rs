use super::checkpoint::{read_at, verify_chunks, Checkpoint};
use super::graph_codec::{bucket, graph};
use super::references::restore_text;
use super::stream::{identity, malformed_record, read_jsonl_blocks, JsonlBlock, READ_CHUNK};
use super::{Cursor, Node, Parsed};
use crate::ingest::{self, registry};
use anyhow::Context;
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::Read;
use std::io::Seek;
use std::io::SeekFrom;
use std::io::Write;
use std::path::Path;

pub fn parse(
    agent: &str,
    path: &Path,
    previous: Option<&[u8]>,
    database: Option<&Path>,
) -> Result<Parsed> {
    let (mut file, meta, id) = open_source(path)?;
    let mut old: Option<Cursor> = previous
        .and_then(|b| lz4_flex::decompress_size_prepended(b).ok())
        .and_then(|b| serde_json::from_slice(&b).ok());
    let mut bytes_read = 0;
    if let Some(c) = &old {
        let valid = c.identity == id && meta.len() >= c.offset;
        if !valid {
            old = None;
        }
    }
    let mut partial_chunk = Vec::new();
    if let Some(cursor) = &old {
        let (valid, tail, read) = verify_chunks(&file, cursor)?;
        bytes_read += read;
        if valid {
            partial_chunk = tail;
        } else {
            old = None;
        }
    }
    let start = old.as_ref().map_or(0, |c| c.offset);
    file.seek(SeekFrom::Start(start))?;
    let mut checkpoint = Checkpoint::new(old.as_ref(), partial_chunk);
    let mut data = String::new();
    let mut spool = (agent != "pi" && meta.len().saturating_sub(start) > READ_CHUNK as u64)
        .then(tempfile::tempfile)
        .transpose()?;
    let mut records = old.as_ref().map_or(0, |c| c.records);
    let mut header = old.as_ref().map_or_else(String::new, |c| c.header.clone());
    let mut leaf = old.as_ref().and_then(|c| c.leaf.clone());
    let mut pi_values = vec![];
    let mut malformed = None;
    let mut replay = None;
    let mut branched = false;
    let (read, complete) =
        read_jsonl_blocks(&mut file, meta.len().saturating_sub(start), |mut block| {
            block.chunks(|bytes| {
                checkpoint.extend(bytes);
                if agent != "pi" {
                    if let Some(file) = &mut spool {
                        file.write_all(bytes)?;
                    } else {
                        data.push_str(&String::from_utf8_lossy(bytes));
                    }
                }
                Ok(())
            })?;
            let base = records;
            if agent != "pi" {
                records += match &mut block {
                    JsonlBlock::Bytes(bytes) => String::from_utf8_lossy(bytes).lines().count(),
                    JsonlBlock::Record(_) => 1,
                };
                return Ok(());
            }
            let parsed = parse_pi_block(block, base, &mut records, &mut malformed)?;
            for record in parsed {
                let Some(value) = record.value() else {
                    pi_values.push(record);
                    continue;
                };
                if value["type"] == "session" {
                    header = serde_json::to_string(&value)?;
                } else if let Some(entry) = value.get("id").and_then(|v| v.as_str()) {
                    let parent = value
                        .get("parentId")
                        .and_then(|v| v.as_str())
                        .map(str::to_owned);
                    if old.is_some() && parent != leaf {
                        branched = true;
                    }
                    leaf = Some(entry.to_string());
                }
                pi_values.push(record);
            }
            Ok(())
        })?;
    bytes_read += read;
    let (first_chunk, last_chunk) = checkpoint.finish();
    if agent == "pi" && branched {
        replay = Some(replay_branch(database, path, &pi_values, &leaf, &header)?);
    }

    let prior = if branched {
        None
    } else {
        old.as_ref().map(|c| c.state.as_str())
    };
    let bytes_parsed = match &replay {
        Some(records) => records
            .iter()
            .map(ingest::pi::PreparedRecord::encoded_len)
            .sum::<serde_json::Result<usize>>()?,
        None => complete,
    };
    let input = spool.map(ingest::jsonl::FileText::new);
    let (messages, events, outcome, state) = match agent {
        "claude" => match &input {
            Some(input) => ingest::claude::resume_parse(path, input, prior)?,
            None => ingest::claude::resume_parse(path, &data, prior)?,
        },
        "codex" => match &input {
            Some(input) => ingest::codex::resume_parse(path, input, prior)?,
            None => ingest::codex::resume_parse(path, &data, prior)?,
        },
        "pi" => ingest::pi::resume_prepared(path, replay.as_deref().unwrap_or(&pi_values), prior)?,
        _ => anyhow::bail!("unsupported incremental adapter"),
    };
    if let Some(input) = &input {
        input.check()?;
    }
    let mut state: serde_json::Value = serde_json::from_str(&state)?;
    let reset = state
        .get("reset")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    state["ordinal"] = serde_json::json!(records);
    let offset = start + complete as u64;
    let cursor = Cursor {
        offset,
        records,
        identity: id,
        state: state.to_string(),
        first_chunk,
        last_chunk,
        header,
        leaf,
    };
    let nodes = parsed_nodes(pi_values);
    Ok(Parsed {
        messages,
        events,
        outcome,
        cursor: lz4_flex::compress_prepend_size(&serde_json::to_vec(&cursor)?),
        nodes,
        append: old.is_some() && !branched && !reset,
        reset_nodes: old.is_none(),
        bytes_read,
        bytes_parsed,
    })
}

fn parse_pi_block(
    block: JsonlBlock<'_>,
    base: usize,
    records: &mut usize,
    malformed: &mut Option<ingest::jsonl::RawRecords>,
) -> Result<Vec<ingest::pi::PreparedRecord>> {
    let compact = |value, ordinal| -> Result<_> {
        let mut value = ingest::pi::compact_record(value);
        value["__agrep_ordinal"] = serde_json::json!(ordinal);
        Ok(Some(value))
    };
    let parsed: Vec<ingest::pi::PreparedRecord> = match block {
        JsonlBlock::Bytes(bytes) => {
            let text = String::from_utf8_lossy(bytes);
            let lines: Vec<_> = text.lines().collect();
            *records += lines.len();
            let parse = |(n, line): (usize, &&str)| {
                if line.trim().is_empty() {
                    return Ok(None);
                }
                compact(ingest::pi::projected_record(line)?, base + n)
            };
            let parsed: Vec<_> = if lines.len() <= 1 || bytes.len() <= 64 << 10 {
                lines.iter().enumerate().map(parse).collect()
            } else {
                lines.par_iter().enumerate().map(parse).collect()
            };
            let mut prepared = Vec::new();
            for (n, value) in parsed.into_iter().enumerate() {
                match value {
                    Ok(Some(value)) => prepared.push(ingest::pi::PreparedRecord::Value(value)),
                    Ok(None) => {}
                    Err(error) => prepared.push(malformed_record(
                        malformed,
                        error.downcast::<serde_json::Error>()?,
                        lines[n].as_bytes(),
                    )?),
                }
            }
            prepared
        }
        JsonlBlock::Record(file) => {
            *records += 1;
            file.rewind()?;
            let mut length = file.metadata()?.len();
            if length > 0 {
                let mut tail = [0; 2];
                let n = length.min(2) as usize;
                read_at(file, &mut tail[..n], length - n as u64)?;
                if tail[n - 1] == b'\n' {
                    length -= 1;
                    if n == 2 && tail[0] == b'\r' {
                        length -= 1;
                    }
                }
            }
            let value = ingest::pi::optional_projected_record_file(file, length);
            match value {
                Ok(Some(value)) => vec![ingest::pi::PreparedRecord::Value(
                    compact(value, base)?.expect("compacted record"),
                )],
                Ok(None) => Vec::new(),
                Err(error) => {
                    file.rewind()?;
                    vec![malformed_record(malformed, error, file.take(length))?]
                }
            }
        }
    };
    Ok(parsed)
}

fn replay_branch(
    database: Option<&Path>,
    path: &Path,
    pi_values: &[ingest::pi::PreparedRecord],
    leaf: &Option<String>,
    header: &str,
) -> Result<Vec<ingest::pi::PreparedRecord>> {
    let c = Connection::open_with_flags(
        database.context("pi branch requires prior index")?,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    c.execute_batch("BEGIN")?;
    let fresh: HashMap<_, _> = pi_values
        .iter()
        .filter_map(ingest::pi::PreparedRecord::value)
        .filter_map(|v| v.get("id").and_then(|v| v.as_str()).map(|id| (id, v)))
        .collect();
    let mut at = leaf.clone();
    let mut seen = HashSet::new();
    let mut chain = Vec::new();
    let source: i64 = c.query_row(
        "SELECT rowid FROM sources WHERE path=?",
        [path.to_string_lossy()],
        |r| r.get(0),
    )?;
    let mut graphs = HashMap::new();
    let mut texts = HashMap::new();
    let mut event_cache = HashMap::new();
    while let Some(id) = at {
        if !seen.insert(id.clone()) {
            break;
        }
        let (parent, body) = if let Some(n) = fresh.get(id.as_str()) {
            (
                n.get("parentId")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned),
                (*n).clone(),
            )
        } else {
            let key = bucket(&id);
            if let std::collections::hash_map::Entry::Vacant(e) = graphs.entry(key) {
                e.insert(graph(&c, source, key)?);
            }
            let Some(node) = graphs[&key].get(&id) else {
                break;
            };
            let mut record = node.record.clone();
            restore_text(&c, &mut record, &mut texts, &mut event_cache)?;
            if record.is_null() {
                record = serde_json::json!({"type":"noop"});
            }
            record["id"] = serde_json::json!(id);
            record["parentId"] = serde_json::json!(node.parent);
            record["__agrep_ordinal"] = serde_json::json!(node.ordinal);
            (node.parent.clone(), record)
        };
        chain.push(body);
        at = parent;
    }
    let mut rebuilt = Vec::new();
    if !header.is_empty() {
        rebuilt.push(ingest::pi::PreparedRecord::Value(serde_json::from_str(
            header,
        )?));
    }
    rebuilt.extend(
        chain
            .into_iter()
            .rev()
            .map(ingest::pi::PreparedRecord::Value),
    );
    // Malformed append records follow the rebuilt readable chain, as in a full replay.
    rebuilt.extend(pi_values.iter().filter(|v| v.value().is_none()).cloned());
    Ok(rebuilt)
}

fn parsed_nodes(pi_values: Vec<ingest::pi::PreparedRecord>) -> Vec<Node> {
    pi_values
        .into_iter()
        .filter_map(ingest::pi::PreparedRecord::into_value)
        .filter_map(|body| {
            if body["type"] == "session" {
                return None;
            }
            let id = body.get("id")?.as_str()?.to_owned();
            let parent = body
                .get("parentId")
                .and_then(|v| v.as_str())
                .map(str::to_owned);
            let ordinal = body["__agrep_ordinal"].as_u64()? as usize;
            Some(Node {
                id,
                parent,
                ordinal,
                body,
            })
        })
        .collect()
}

fn open_source(path: &Path) -> Result<(std::fs::File, std::fs::Metadata, String)> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    registry::nofollow_read_options(&mut options);
    if registry::metadata_is_link(&std::fs::symlink_metadata(path)?) {
        anyhow::bail!("source is a symlink")
    }
    let file = options.open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() {
        anyhow::bail!("source is not a regular file")
    }
    let id = identity(path, &meta, &file)?;
    Ok((file, meta, id))
}
