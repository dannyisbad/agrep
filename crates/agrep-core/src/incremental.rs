//! Persisted JSONL parser checkpoints and pi's native entry graph.
use crate::{
    ingest::{self, registry},
    intake_batch::ReadOutcome,
    model::{Event, Message},
};
use anyhow::{Context, Result};
use rayon::prelude::*;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Transaction};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom},
    path::Path,
};

#[derive(Serialize, Deserialize)]
struct Cursor {
    offset: u64,
    records: usize,
    identity: String,
    state: String,
    prefix_chunks: Vec<[u8; 32]>,
    header: String,
    leaf: Option<String>,
}
pub struct Node {
    pub id: String,
    pub parent: Option<String>,
    pub ordinal: usize,
    pub body: serde_json::Value,
}
pub struct Parsed {
    pub messages: Vec<Message>,
    pub events: Vec<Event>,
    pub outcome: ReadOutcome,
    pub cursor: Vec<u8>,
    pub nodes: Vec<Node>,
    pub append: bool,
    pub reset_nodes: bool,
    pub bytes_read: usize,
    pub bytes_parsed: usize,
}
const CHECKPOINT_CHUNK: usize = 1024 * 1024;

fn chunk_hashes(bytes: &[u8]) -> Vec<[u8; 32]> {
    bytes
        .par_chunks(CHECKPOINT_CHUNK)
        .map(|chunk| Sha256::digest(chunk).into())
        .collect()
}

fn read_at(file: &std::fs::File, mut bytes: &mut [u8], mut offset: u64) -> std::io::Result<()> {
    while !bytes.is_empty() {
        #[cfg(unix)]
        let n = std::os::unix::fs::FileExt::read_at(file, bytes, offset)?;
        #[cfg(windows)]
        let n = std::os::windows::fs::FileExt::seek_read(file, bytes, offset)?;
        #[cfg(not(any(unix, windows)))]
        let n = {
            let _ = (file, offset);
            return Err(std::io::Error::from(std::io::ErrorKind::Unsupported));
        };
        if n == 0 {
            return Err(std::io::ErrorKind::UnexpectedEof.into());
        }
        offset += n as u64;
        bytes = &mut bytes[n..];
    }
    Ok(())
}

/// Verify every prior byte. Independent chunk digests permit parallel reads and
/// hashing; the final partial chunk is returned for the next checkpoint.
fn verify_chunks(file: &std::fs::File, cursor: &Cursor) -> Result<(bool, Vec<u8>)> {
    anyhow::ensure!(
        cursor.prefix_chunks.len() == (cursor.offset as usize).div_ceil(CHECKPOINT_CHUNK),
        "invalid prefix checkpoint"
    );
    let checked: Vec<_> = cursor
        .prefix_chunks
        .par_iter()
        .enumerate()
        .map(|(i, expected)| -> Result<_> {
            let offset = i * CHECKPOINT_CHUNK;
            let length = (cursor.offset as usize - offset).min(CHECKPOINT_CHUNK);
            let mut bytes = vec![0; length];
            read_at(file, &mut bytes, offset as u64)?;
            let valid = <[u8; 32]>::from(Sha256::digest(&bytes)) == *expected;
            Ok((
                valid,
                if length < CHECKPOINT_CHUNK {
                    bytes
                } else {
                    Vec::new()
                },
            ))
        })
        .collect::<Result<_>>()?;
    let valid = checked.iter().all(|(valid, _)| *valid);
    Ok((
        valid,
        checked
            .into_iter()
            .last()
            .map(|(_, tail)| tail)
            .unwrap_or_default(),
    ))
}
/// Packed native graph pages use DEFLATE; row text uses its independent codec.
pub fn pack_node(bytes: &[u8]) -> Result<Vec<u8>> {
    crate::compression::deflate(b"G2", bytes)
}
pub fn unpack_node(bytes: &[u8]) -> Result<Vec<u8>> {
    let input = bytes
        .strip_prefix(b"G2")
        .context("invalid native entry codec")?;
    crate::compression::inflate(input)
}
#[derive(Debug, PartialEq)]
struct GraphNode {
    parent: Option<String>,
    ordinal: usize,
    record: serde_json::Value,
}

#[cfg(test)]
mod graph_codec_tests {
    use super::*;
    #[test]
    fn chunk_checkpoints_detect_edits_at_every_boundary() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("source");
        let original = vec![b'x'; 3 * CHECKPOINT_CHUNK + 17];
        std::fs::write(&path, &original).unwrap();
        let cursor = Cursor {
            offset: original.len() as u64,
            records: 0,
            identity: String::new(),
            state: String::new(),
            prefix_chunks: chunk_hashes(&original),
            header: String::new(),
            leaf: None,
        };
        let file = std::fs::File::open(&path).unwrap();
        let (valid, tail) = verify_chunks(&file, &cursor).unwrap();
        assert!(valid);
        assert_eq!(tail, vec![b'x'; 17]);
        for at in [
            0,
            CHECKPOINT_CHUNK - 1,
            CHECKPOINT_CHUNK,
            2 * CHECKPOINT_CHUNK + 1,
            original.len() - 1,
        ] {
            let mut changed = original.clone();
            changed[at] = b'y';
            std::fs::write(&path, changed).unwrap();
            assert!(!verify_chunks(&file, &cursor).unwrap().0, "edit at {at}");
        }
    }
    #[test]
    fn packed_graph_preserves_hex_and_named_identifiers() {
        let mut nodes = Graph::new();
        nodes.insert(
            "0000000a".into(),
            GraphNode {
                parent: None,
                ordinal: 0,
                record: serde_json::Value::Null,
            },
        );
        nodes.insert(
            "00abcdef".into(),
            GraphNode {
                parent: Some("0000000a".into()),
                ordinal: 128,
                record: serde_json::json!({"message":{"role":"user","content":"literal 界 text"}}),
            },
        );
        nodes.insert(
            "ABCDEF12".into(),
            GraphNode {
                parent: Some("00abcdef".into()),
                ordinal: 1_000_000,
                record: serde_json::json!({"type":"model_change","modelId":"fixture-model"}),
            },
        );
        nodes.insert(
            "named-entry".into(),
            GraphNode {
                parent: Some("ABCDEF12".into()),
                ordinal: 1_000_001,
                record: serde_json::json!({"message":{"__agrep_event_output":[42,"\n",""]}}),
            },
        );
        let encoded = pack_graph(&nodes).unwrap();
        assert_eq!(unpack_graph(&encoded).unwrap(), nodes);
        assert!(unpack_graph(b"not a graph").is_err());
    }
}
type Graph = BTreeMap<String, GraphNode>;
fn bucket(id: &str) -> i64 {
    i64::from(
        id.bytes()
            .fold(0u8, |n, b| n.wrapping_mul(31).wrapping_add(b))
            & 15,
    )
}
fn put_integer(mut n: u64, out: &mut Vec<u8>) {
    while n >= 128 {
        out.push(n as u8 | 128);
        n >>= 7;
    }
    out.push(n as u8);
}
fn take_integer(bytes: &[u8], at: &mut usize) -> Result<u64> {
    let mut n = 0;
    for shift in (0..=63).step_by(7) {
        let b = *bytes.get(*at).context("truncated graph integer")?;
        *at += 1;
        anyhow::ensure!(shift != 63 || b < 2, "graph integer overflow");
        n |= u64::from(b & 127) << shift;
        if b < 128 {
            return Ok(n);
        }
    }
    anyhow::bail!("invalid graph integer")
}
fn put_id(id: Option<&str>, out: &mut Vec<u8>) {
    match id {
        None => out.push(0),
        Some(id)
            if id.len() == 8
                && id
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) =>
        {
            out.push(1);
            out.extend(u32::from_str_radix(id, 16).unwrap().to_le_bytes());
        }
        Some(id) => {
            out.push(2);
            put_integer(id.len() as u64, out);
            out.extend(id.as_bytes());
        }
    }
}
fn take_bytes<'a>(bytes: &'a [u8], at: &mut usize, len: usize) -> Result<&'a [u8]> {
    let end = at.checked_add(len).context("graph span overflow")?;
    let part = bytes.get(*at..end).context("truncated graph span")?;
    *at = end;
    Ok(part)
}
fn take_id(bytes: &[u8], at: &mut usize) -> Result<Option<String>> {
    let tag = take_bytes(bytes, at, 1)?[0];
    match tag {
        0 => Ok(None),
        1 => Ok(Some(format!(
            "{:08x}",
            u32::from_le_bytes(take_bytes(bytes, at, 4)?.try_into()?)
        ))),
        2 => {
            let len = usize::try_from(take_integer(bytes, at)?)?;
            Ok(Some(
                std::str::from_utf8(take_bytes(bytes, at, len)?)?.to_owned(),
            ))
        }
        _ => anyhow::bail!("invalid graph identifier"),
    }
}
fn pack_graph(graph: &Graph) -> Result<Vec<u8>> {
    let mut nodes: Vec<_> = graph.iter().collect();
    nodes.sort_by(|a, b| a.1.ordinal.cmp(&b.1.ordinal).then(a.0.cmp(b.0)));
    let (mut bytes, mut payload, mut previous) = (Vec::new(), Vec::new(), 0);
    put_integer(nodes.len() as u64, &mut bytes);
    for (id, node) in nodes {
        put_id(Some(id), &mut bytes);
        put_id(node.parent.as_deref(), &mut bytes);
        put_integer((node.ordinal - previous) as u64, &mut bytes);
        previous = node.ordinal;
        if node.record.is_null() {
            bytes.push(0);
        } else {
            bytes.push(1);
            payload.clear();
            serde_json::to_writer(&mut payload, &node.record)?;
            put_integer(payload.len() as u64, &mut bytes);
            bytes.extend_from_slice(&payload);
        }
    }
    pack_node(&bytes)
}
fn unpack_graph(body: &[u8]) -> Result<Graph> {
    let bytes = unpack_node(body)?;
    let (mut at, mut ordinal, mut out) = (0, 0usize, Graph::new());
    let count = usize::try_from(take_integer(&bytes, &mut at)?)?;
    anyhow::ensure!(count <= bytes.len(), "invalid graph record count");
    for _ in 0..count {
        let id = take_id(&bytes, &mut at)?.context("missing graph identifier")?;
        let parent = take_id(&bytes, &mut at)?;
        ordinal = ordinal
            .checked_add(usize::try_from(take_integer(&bytes, &mut at)?)?)
            .context("graph order overflow")?;
        let record = match take_bytes(&bytes, &mut at, 1)?[0] {
            0 => serde_json::Value::Null,
            1 => {
                let len = usize::try_from(take_integer(&bytes, &mut at)?)?;
                serde_json::from_slice(take_bytes(&bytes, &mut at, len)?)?
            }
            _ => anyhow::bail!("invalid graph record"),
        };
        out.insert(
            id,
            GraphNode {
                parent,
                ordinal,
                record,
            },
        );
    }
    anyhow::ensure!(at == bytes.len(), "trailing graph bytes");
    Ok(out)
}
fn graph(c: &Connection, source: i64, bucket: i64) -> Result<Graph> {
    let bytes: Option<Vec<u8>> = c
        .query_row(
            "SELECT body FROM parser_graph WHERE source=?1 AND bucket=?2",
            params![source, bucket],
            |r| r.get(0),
        )
        .optional()?;
    bytes.map_or_else(|| Ok(Graph::new()), |b| unpack_graph(&b))
}
fn blob_text<'a>(c: &Connection, cache: &'a mut HashMap<i64, String>, id: i64) -> Result<&'a str> {
    if let std::collections::hash_map::Entry::Vacant(e) = cache.entry(id) {
        let body: Vec<u8> = c
            .prepare_cached("SELECT body FROM tool_blobs WHERE id=?")?
            .query_row([id], |r| r.get(0))?;
        e.insert(crate::tool_store::decode(&body)?);
    }
    Ok(&cache[&id])
}
fn reference_text(
    tx: &Transaction<'_>,
    source: i64,
    value: &mut serde_json::Value,
    preferred: Option<i64>,
    writer: &mut crate::tool_store::Writer,
    cache: &mut HashMap<i64, String>,
) -> Result<()> {
    let Some(text) = value.as_str().filter(|s| s.len() >= 96).map(str::to_owned) else {
        return Ok(());
    };
    let trimmed = text.trim();
    let found = if let Some(id) = preferred {
        if trimmed.is_empty() {
            None
        } else {
            blob_text(tx, cache, id)?
                .find(trimmed)
                .map(|start| (id, start, trimmed.len()))
        }
    } else {
        None
    };
    let (id, start, len, left, right) = if let Some((id, start, len)) = found {
        let left = &text[..text.len() - text.trim_start().len()];
        let right = &text[text.trim_end().len()..];
        (id, start, len, left, right)
    } else {
        (
            writer.blob(tx, source, crate::tool_store::Text::Plain(&text))?,
            0,
            text.len(),
            "",
            "",
        )
    };
    writer.own_blob(tx, source, id)?;
    *value = serde_json::json!({"__agrep_text_ref":[id,start,len,left,right]});
    Ok(())
}
#[derive(Clone)]
struct EventParts {
    call: String,
    name: String,
    input: String,
    output: String,
    state: String,
    input_chars: i64,
    output_chars: i64,
    output_bytes: i64,
    ok: Option<bool>,
}
fn event_parts<'a>(
    c: &Connection,
    id: i64,
    cache: &'a mut HashMap<i64, EventParts>,
) -> Result<&'a EventParts> {
    if cache.contains_key(&id) {
        return Ok(&cache[&id]);
    }
    let mut p = c.prepare_cached("SELECT e.call_id,n.value,b.body,e.input_end,e.output_start,e.input_chars,e.output_chars,e.output_bytes,e.ok,e.state FROM event_rows e JOIN symbols n ON n.id=e.name_key JOIN tool_blobs b ON b.id=e.blob WHERE e.id=?")?.query_row([id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,Vec<u8>>(2)?,r.get::<_,Option<usize>>(3)?,r.get::<_,Option<usize>>(4)?,r.get::<_,i64>(5)?,r.get::<_,i64>(6)?,r.get::<_,i64>(7)?,r.get::<_,Option<bool>>(8)?,r.get::<_,String>(9)?)))?;
    let text = crate::tool_store::decode(&p.2)?;
    let head: String = text.chars().take(p.3.unwrap_or(usize::MAX)).collect();
    let prefix = format!(
        "{}{}",
        p.1,
        if p.8 == Some(false) { " [failed]" } else { "" }
    );
    let input = if head == prefix {
        String::new()
    } else {
        head.strip_prefix(&format!("{prefix}: "))
            .unwrap_or(&head)
            .to_string()
    };
    let output =
        p.4.map_or_else(String::new, |start| text.chars().skip(start).collect());
    let parts = EventParts {
        call: std::mem::take(&mut p.0),
        name: std::mem::take(&mut p.1),
        input,
        output,
        input_chars: p.5,
        output_chars: p.6,
        output_bytes: p.7,
        ok: p.8,
        state: std::mem::take(&mut p.9),
    };
    cache.insert(id, parts);
    Ok(&cache[&id])
}
fn padding(original: &str, stored: &str) -> Option<(String, String)> {
    (original.trim() == stored.trim()).then(|| {
        (
            original[..original.len() - original.trim_start().len()].to_string(),
            original[original.trim_end().len()..].to_string(),
        )
    })
}
fn restore_event(
    c: &Connection,
    value: &mut serde_json::Value,
    events: &mut HashMap<i64, EventParts>,
) -> Result<bool> {
    for (key, call) in [
        ("__agrep_event_call", true),
        ("__agrep_event_output", false),
    ] {
        if let Some(a) = value.get(key).and_then(|v| v.as_array()) {
            anyhow::ensure!(a.len() == 3, "invalid native event reference");
            let p = event_parts(c, a[0].as_i64().context("invalid event id")?, events)?;
            let text = format!(
                "{}{}{}",
                a[1].as_str().context("invalid event prefix")?,
                if call {
                    p.input.trim()
                } else {
                    p.output.trim()
                },
                a[2].as_str().context("invalid event suffix")?
            );
            *value = if call {
                serde_json::json!({"type":"toolCall","id":p.call,"name":p.name,"__agrep_input":text,"__agrep_input_chars":p.input_chars,"__agrep_state":p.state})
            } else {
                serde_json::json!({"role":"toolResult","toolCallId":p.call,"content":text,"__agrep_chars":p.output_chars,"__agrep_bytes":p.output_bytes,"isError":p.ok.map(|ok|!ok)})
            };
            return Ok(true);
        }
    }
    Ok(false)
}
fn restore_text(
    c: &Connection,
    value: &mut serde_json::Value,
    cache: &mut HashMap<i64, String>,
    events: &mut HashMap<i64, EventParts>,
) -> Result<()> {
    if restore_event(c, value, events)? {
        return Ok(());
    }
    if value
        .as_object()
        .is_some_and(|v| v.len() == 1 && v.contains_key("__agrep_text_ref"))
    {
        let a = value["__agrep_text_ref"]
            .as_array()
            .context("invalid graph text reference")?;
        anyhow::ensure!(a.len() == 5, "invalid graph text reference");
        let id = a[0].as_i64().context("invalid graph blob")?;
        let start = a[1].as_u64().context("invalid graph offset")? as usize;
        let len = a[2].as_u64().context("invalid graph length")? as usize;
        let text = blob_text(c, cache, id)?
            .get(start..start.checked_add(len).context("graph range overflow")?)
            .context("invalid graph text range")?;
        *value = serde_json::Value::String(format!(
            "{}{}{}",
            a[3].as_str().context("invalid graph prefix")?,
            text,
            a[4].as_str().context("invalid graph suffix")?
        ));
    } else {
        match value {
            serde_json::Value::Array(a) => {
                for v in a {
                    restore_text(c, v, cache, events)?;
                }
            }
            serde_json::Value::Object(a) => {
                for v in a.values_mut() {
                    restore_text(c, v, cache, events)?;
                }
            }
            _ => {}
        }
    }
    Ok(())
}
fn event_blob(
    tx: &Transaction<'_>,
    source: i64,
    session: Option<i64>,
    call: &str,
) -> Result<Option<(i64, i64)>> {
    if let Some(session) = session {
        Ok(tx.prepare_cached("SELECT id,blob FROM event_rows WHERE source=?1 AND session_key=?2 AND call_id=?3 AND active=1 AND blob IS NOT NULL")?.query_row(params![source,session,call], |r|Ok((r.get(0)?,r.get(1)?))).optional()?)
    } else {
        Ok(None)
    }
}
/// Saved graph pages, packed in parallel batches: one parallel pack per small
/// source left the writer waiting on the pool for every source. A refresh saves
/// each source at most once, so no save reads a page that is still batched.
pub(crate) struct GraphPages {
    /// Idle while sources parse on threads of their own, whose work fills the global pool.
    pool: &'static rayon::ThreadPool,
    batch: Vec<(i64, i64, Graph)>,
    nodes: usize,
}
impl GraphPages {
    const BATCH_NODES: usize = 8_192;
    pub(crate) fn new(pool: &'static rayon::ThreadPool) -> Self {
        Self {
            pool,
            batch: Vec::new(),
            nodes: 0,
        }
    }
    fn queue(
        &mut self,
        tx: &Transaction<'_>,
        source: i64,
        pages: BTreeMap<i64, Graph>,
    ) -> Result<()> {
        for (bucket, graph) in pages {
            self.nodes += graph.len();
            self.batch.push((source, bucket, graph));
        }
        if self.nodes >= Self::BATCH_NODES {
            self.flush(tx)?;
        }
        Ok(())
    }
    fn flush(&mut self, tx: &Transaction<'_>) -> Result<()> {
        let batch = std::mem::take(&mut self.batch);
        let packed: Vec<(i64, i64, Vec<u8>)> = self.pool.install(|| {
            batch
                .into_par_iter()
                .map(|(source, bucket, graph)| Ok((source, bucket, pack_graph(&graph)?)))
                .collect::<Result<_>>()
        })?;
        self.nodes = 0;
        for (source, bucket, body) in packed {
            tx.prepare_cached("INSERT INTO parser_graph VALUES(?1,?2,?3) ON CONFLICT(source,bucket) DO UPDATE SET body=excluded.body")?.execute(params![source,bucket,body])?;
        }
        Ok(())
    }
    /// Inserts every batched page; required before the transaction commits.
    pub(crate) fn finish(mut self, tx: &Transaction<'_>) -> Result<()> {
        self.flush(tx)
    }
}
pub(crate) fn save_nodes(
    tx: &Transaction<'_>,
    source: i64,
    nodes: Vec<Node>,
    reset: bool,
    session: Option<(&str, &str)>,
    events: &[Event],
    pages: &mut GraphPages,
) -> Result<()> {
    if reset {
        tx.execute("DELETE FROM parser_graph WHERE source=?", [source])?;
    }
    let session = match session {
        Some((agent, session)) => tx
            .query_row(
                "SELECT id FROM event_sessions WHERE agent=?1 AND session=?2",
                params![agent, session],
                |r| r.get::<_, i64>(0),
            )
            .optional()?,
        None => None,
    };
    let mut changed: BTreeMap<i64, Graph> = BTreeMap::new();
    let mut writer = crate::tool_store::Writer::default();
    let mut cache = HashMap::new();
    let mut event_blobs: HashMap<String, (i64, i64)> = if reset {
        tx.prepare("SELECT call_id,id,blob FROM event_rows WHERE source=?1 AND active=1 AND blob IS NOT NULL")?
            .query_map(params![source], |r| Ok((r.get(0)?, (r.get(1)?,r.get(2)?))))?
            .collect::<rusqlite::Result<_>>()?
    } else {
        HashMap::new()
    };
    // Initial parsing already owns exactly the event revisions being stored.
    // Seed their lossless parser fields rather than immediately querying and
    // decompressing every newly written tool blob again.
    let mut event_cache = HashMap::new();
    if reset {
        for event in events {
            if let Some(&(id, _)) = event_blobs.get(event.call_id.as_str()) {
                event_cache.entry(id).or_insert_with(|| EventParts {
                    call: event.call_id.clone(),
                    name: event.name.clone(),
                    input: event.input.trim().to_owned(),
                    output: event.output.trim().to_owned(),
                    state: event.state.clone(),
                    input_chars: event.input_chars as i64,
                    output_chars: event.output_chars as i64,
                    output_bytes: event.output_bytes as i64,
                    ok: event.ok,
                });
            }
        }
    }
    let mut lookup = |call: &str| -> Result<Option<(i64, i64)>> {
        if let Some(blob) = event_blobs.get(call) {
            return Ok(Some(*blob));
        }
        let blob = if reset {
            None
        } else {
            event_blob(tx, source, session, call)?
        };
        if let Some(blob) = blob {
            event_blobs.insert(call.to_string(), blob);
        }
        Ok(blob)
    };
    for node in nodes {
        let mut record = node.body;
        let call = record["message"]["toolCallId"]
            .as_str()
            .unwrap_or(&node.id)
            .to_owned();
        let role = record["message"]["role"].as_str().unwrap_or("").to_owned();
        if role == "toolResult" {
            let preferred = lookup(&call)?;
            let marker = if let (Some((id, _)), Some(original)) =
                (preferred, record["message"]["content"].as_str())
            {
                let p = event_parts(tx, id, &mut event_cache)?;
                padding(original, &p.output).map(
                    |(left, right)| serde_json::json!({"__agrep_event_output":[id,left,right]}),
                )
            } else {
                None
            };
            if let Some(marker) = marker {
                record["message"] = marker;
            } else {
                reference_text(
                    tx,
                    source,
                    &mut record["message"]["content"],
                    preferred.map(|(_, blob)| blob),
                    &mut writer,
                    &mut cache,
                )?;
            }
        } else if role == "bashExecution" {
            let preferred = lookup(&call)?;
            reference_text(
                tx,
                source,
                &mut record["message"]["output"],
                preferred.map(|(_, blob)| blob),
                &mut writer,
                &mut cache,
            )?;
            reference_text(
                tx,
                source,
                &mut record["message"]["command"],
                preferred.map(|(_, blob)| blob),
                &mut writer,
                &mut cache,
            )?;
        } else if role == "assistant" {
            if let Some(blocks) = record["message"]["content"].as_array_mut() {
                for block in blocks {
                    if block["type"] == "toolCall" {
                        let call = block["id"].as_str().unwrap_or("");
                        let preferred = lookup(call)?;
                        let marker = if let (Some((id, _)), Some(original)) =
                            (preferred, block["__agrep_input"].as_str())
                        {
                            let p = event_parts(tx, id, &mut event_cache)?;
                            if block["name"].as_str() == Some(p.name.as_str())
                                && block["__agrep_state"].as_str() == Some(p.state.as_str())
                            {
                                padding(original,&p.input).map(|(left,right)|serde_json::json!({"__agrep_event_call":[id,left,right]}))
                            } else {
                                None
                            }
                        } else {
                            None
                        };
                        if let Some(marker) = marker {
                            *block = marker;
                        } else {
                            reference_text(
                                tx,
                                source,
                                &mut block["__agrep_input"],
                                preferred.map(|(_, blob)| blob),
                                &mut writer,
                                &mut cache,
                            )?;
                        }
                    }
                }
            }
        }
        if !matches!(
            record["type"].as_str(),
            Some("message" | "model_change" | "compaction")
        ) {
            record = serde_json::Value::Null;
        }
        if let Some(fields) = record.as_object_mut() {
            fields.remove("id");
            fields.remove("parentId");
            fields.remove("__agrep_ordinal");
        }
        let key = bucket(&node.id);
        if let std::collections::btree_map::Entry::Vacant(e) = changed.entry(key) {
            e.insert(if reset {
                Graph::new()
            } else {
                graph(tx, source, key)?
            });
        }
        changed.get_mut(&key).unwrap().insert(
            node.id,
            GraphNode {
                parent: node.parent,
                ordinal: node.ordinal,
                record,
            },
        );
    }
    pages.queue(tx, source, changed)
}
pub fn supports(agent: &str, path: &Path) -> bool {
    matches!(agent, "pi" | "claude" | "codex") && path.extension().is_some_and(|s| s == "jsonl")
}
fn identity(path: &Path, m: &std::fs::Metadata) -> Result<String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let _ = path;
        Ok(format!("{}:{}", m.dev(), m.ino()))
    }
    #[cfg(windows)]
    {
        Ok(registry::metadata_change_token_with_file_identity(path, m)?
            .1
            .cache_key())
    }
    #[cfg(not(any(unix, windows)))]
    {
        Ok(format!("{:?}", m.created()?))
    }
}
pub fn parse(
    agent: &str,
    path: &Path,
    previous: Option<&[u8]>,
    database: Option<&Path>,
) -> Result<Parsed> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    registry::nofollow_read_options(&mut options);
    if registry::metadata_is_link(&std::fs::symlink_metadata(path)?) {
        anyhow::bail!("source is a symlink")
    }
    let mut file = options.open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() {
        anyhow::bail!("source is not a regular file")
    }
    let id = identity(path, &meta)?;
    let mut old: Option<Cursor> = previous
        .and_then(|b| lz4_flex::decompress_size_prepended(b).ok())
        .and_then(|b| serde_json::from_slice(&b).ok());
    let mut bytes_read = 0;
    if let Some(c) = &old {
        // Every prior chunk is checked below; identity/length only reject an
        // obviously replaced or truncated source before doing that work.
        let valid = c.identity == id && meta.len() >= c.offset;
        if !valid {
            old = None;
        }
    }
    let mut partial_chunk = Vec::new();
    if let Some(cursor) = &old {
        let (valid, tail) = verify_chunks(&file, cursor)?;
        bytes_read += cursor.offset as usize;
        if valid {
            partial_chunk = tail;
        } else {
            old = None;
        }
    }
    let start = old.as_ref().map_or(0, |c| c.offset);
    file.seek(SeekFrom::Start(start))?;
    let mut bytes = Vec::new();
    std::io::Read::by_ref(&mut file)
        .take(meta.len().saturating_sub(start))
        .read_to_end(&mut bytes)?;
    bytes_read += bytes.len();
    // A torn final JSON record waits for the next append; valid non-newline-terminated JSON is complete.
    let tail_start = bytes.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
    let complete = if tail_start == bytes.len()
        || serde_json::from_slice::<serde_json::Value>(&bytes[tail_start..]).is_ok()
    {
        bytes.len()
    } else {
        tail_start
    };
    let prefix_chunks = if let Some(cursor) = &old {
        let mut chunks = cursor.prefix_chunks[..cursor.offset as usize / CHECKPOINT_CHUNK].to_vec();
        partial_chunk.extend_from_slice(&bytes[..complete]);
        chunks.extend(chunk_hashes(&partial_chunk));
        chunks
    } else {
        chunk_hashes(&bytes[..complete])
    };
    let mut data = String::from_utf8_lossy(&bytes[..complete]).into_owned();
    drop(bytes);
    let records = old.as_ref().map_or(0, |c| c.records) + data.lines().count();
    let mut header = old.as_ref().map_or_else(String::new, |c| c.header.clone());
    let mut leaf = old.as_ref().and_then(|c| c.leaf.clone());
    let mut nodes = vec![];
    let mut branched = false;
    if agent == "pi" {
        // Records parse independently, so a large source spreads over the pool; the
        // header, leaf and graph below still follow file order.
        let base = old.as_ref().map_or(0, |c| c.records);
        let lines: Vec<&str> = data.lines().collect();
        let records: Vec<Result<Option<(serde_json::Value, String)>>> = lines
            .par_iter()
            .enumerate()
            .map(|(n, line)| {
                if line.trim().is_empty() {
                    return Ok(None);
                }
                let mut value: serde_json::Value = serde_json::from_str(line)
                    .with_context(|| format!("invalid JSONL record {}", n + 1))?;
                value = ingest::pi::compact_record(value);
                value["__agrep_ordinal"] = serde_json::json!(base + n);
                let encoded = serde_json::to_string(&value)?;
                Ok(Some((value, encoded)))
            })
            .collect();
        let mut normalized = String::new();
        let mut malformed = Vec::new();
        for (n, record) in records.into_iter().enumerate() {
            let (value, encoded) = match record {
                Ok(Some(record)) => record,
                Ok(None) => {
                    normalized.push('\n');
                    continue;
                }
                Err(_) => {
                    // The adapter counts malformed records and retains readable ones.
                    normalized.push_str(lines[n]);
                    normalized.push('\n');
                    malformed.push(lines[n].to_owned());
                    continue;
                }
            };
            if value["type"] == "session" {
                header = encoded.clone();
            } else if let Some(entry) = value.get("id").and_then(|v| v.as_str()) {
                let parent = value
                    .get("parentId")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned);
                if old.is_some() && parent != leaf {
                    branched = true;
                }
                leaf = Some(entry.to_string());
                nodes.push(Node {
                    id: entry.into(),
                    parent,
                    ordinal: base + n,
                    body: value,
                });
            }
            normalized.push_str(&encoded);
            normalized.push('\n');
        }
        drop(lines);
        data = normalized;
        if branched {
            let c = Connection::open_with_flags(
                database.context("pi branch requires prior index")?,
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )?;
            c.execute_batch("BEGIN")?;
            let fresh: HashMap<_, _> = nodes.iter().map(|n| (n.id.as_str(), n)).collect();
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
                    (n.parent.clone(), serde_json::to_vec(&n.body)?)
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
                    (node.parent.clone(), serde_json::to_vec(&record)?)
                };
                chain.push(String::from_utf8(body)?);
                at = parent;
            }
            data = header.clone();
            data.push('\n');
            for record in chain.iter().rev() {
                data.push_str(record);
                data.push('\n');
            }
            for record in malformed {
                data.push_str(&record);
                data.push('\n');
            }
        }
    }
    let prior = if branched {
        None
    } else {
        old.as_ref().map(|c| c.state.as_str())
    };
    let bytes_parsed = if branched { data.len() } else { complete };
    let (messages, events, outcome, state) = match agent {
        "claude" => ingest::claude::resume_parse(path, &data, prior)?,
        "codex" => ingest::codex::resume_parse(path, &data, prior)?,
        "pi" => ingest::pi::resume_parse(path, &data, prior)?,
        _ => anyhow::bail!("unsupported incremental adapter"),
    };
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
        prefix_chunks,
        header,
        leaf,
    };
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
