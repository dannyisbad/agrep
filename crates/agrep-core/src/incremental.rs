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
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    path::Path,
};

#[derive(Serialize, Deserialize)]
struct Cursor {
    offset: u64,
    records: usize,
    identity: String,
    state: String,
    first_chunk: [u8; 32],
    last_chunk: [u8; 32],
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
const CHECKPOINT_CHUNK: usize = 64 * 1024;

fn chunk_digest(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

struct Checkpoint {
    first: Option<[u8; 32]>,
    last: [u8; 32],
    tail: Vec<u8>,
}
impl Checkpoint {
    fn new(cursor: Option<&Cursor>, tail: Vec<u8>) -> Self {
        Self {
            first: cursor
                .filter(|c| c.offset >= CHECKPOINT_CHUNK as u64)
                .map(|c| c.first_chunk),
            last: cursor.map_or_else(|| chunk_digest(&[]), |c| c.last_chunk),
            tail,
        }
    }
    fn extend(&mut self, mut bytes: &[u8]) {
        if !self.tail.is_empty() {
            let take = bytes.len().min(CHECKPOINT_CHUNK - self.tail.len());
            self.tail.extend_from_slice(&bytes[..take]);
            bytes = &bytes[take..];
            if self.tail.len() == CHECKPOINT_CHUNK {
                self.last = chunk_digest(&self.tail);
                self.first.get_or_insert(self.last);
                self.tail.clear();
            }
        }
        let full = bytes.len() / CHECKPOINT_CHUNK * CHECKPOINT_CHUNK;
        if full != 0 {
            self.first
                .get_or_insert_with(|| chunk_digest(&bytes[..CHECKPOINT_CHUNK]));
            self.last = chunk_digest(&bytes[full - CHECKPOINT_CHUNK..full]);
        }
        self.tail.extend_from_slice(&bytes[full..]);
    }
    fn finish(mut self) -> ([u8; 32], [u8; 32]) {
        if !self.tail.is_empty() {
            self.last = chunk_digest(&self.tail);
        }
        (self.first.unwrap_or(self.last), self.last)
    }
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

/// Append-only logs need only the first and last parsed chunks checked.
fn verify_chunks(file: &std::fs::File, cursor: &Cursor) -> Result<(bool, Vec<u8>, usize)> {
    let length = usize::try_from(cursor.offset)?;
    if length == 0 {
        return Ok((
            cursor.first_chunk == chunk_digest(&[]) && cursor.last_chunk == chunk_digest(&[]),
            Vec::new(),
            0,
        ));
    }
    let mut first = vec![0; length.min(CHECKPOINT_CHUNK)];
    read_at(file, &mut first, 0)?;
    let first_valid = chunk_digest(&first) == cursor.first_chunk;
    let last_at = (length - 1) / CHECKPOINT_CHUNK * CHECKPOINT_CHUNK;
    let (last, read) = if last_at == 0 {
        let read = first.len();
        (first, read)
    } else {
        let mut last = vec![0; length - last_at];
        read_at(file, &mut last, last_at as u64)?;
        let read = first.len() + last.len();
        (last, read)
    };
    let valid = first_valid && chunk_digest(&last) == cursor.last_chunk;
    Ok((
        valid,
        if last.len() < CHECKPOINT_CHUNK {
            last
        } else {
            Vec::new()
        },
        read,
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
    use std::io::Write;

    fn source(path: &Path, padding: usize) -> Vec<u8> {
        let header =
            serde_json::json!({"type":"session","version":3,"id":"resume-test","cwd":"/work/test"});
        let message = serde_json::json!({"type":"message","id":"m0","parentId":null,"message":{"role":"user","content":"x".repeat(padding)}});
        let bytes = format!("{header}\n{message}\n").into_bytes();
        std::fs::write(path, &bytes).unwrap();
        bytes
    }

    #[test]
    fn resumes_read_append_plus_at_most_two_chunks() {
        for padding in [17, CHECKPOINT_CHUNK - 17, 4 * CHECKPOINT_CHUNK + 17] {
            let temp = tempfile::tempdir().unwrap();
            let path = temp.path().join("session.jsonl");
            source(&path, padding);
            let mut parsed = parse("pi", &path, None, None).unwrap();
            for n in 1..=3 {
                let append = format!(
                    "{}\n",
                    serde_json::json!({"type":"message","id":format!("m{n}"),"parentId":format!("m{}",n-1),"message":{"role":"user","content":"appended"}})
                );
                OpenOptions::new()
                    .append(true)
                    .open(&path)
                    .unwrap()
                    .write_all(append.as_bytes())
                    .unwrap();
                parsed = parse("pi", &path, Some(&parsed.cursor), None).unwrap();
                assert!(parsed.append);
                assert_eq!(&*parsed.messages.last().unwrap().text, "appended");
                assert_eq!(parsed.bytes_parsed, append.len());
                assert!(parsed.bytes_read <= append.len() + 2 * CHECKPOINT_CHUNK);
            }
        }
    }

    #[test]
    fn replacement_truncation_and_boundary_rewrites_restart_parser() {
        for change in ["replacement", "truncation", "first", "last"] {
            let temp = tempfile::tempdir().unwrap();
            let path = temp.path().join("session.jsonl");
            let original = source(&path, 4 * CHECKPOINT_CHUNK + 17);
            let first = parse("pi", &path, None, None).unwrap();
            match change {
                "replacement" => {
                    let replacement = temp.path().join("replacement");
                    std::fs::write(&replacement, &original).unwrap();
                    std::fs::remove_file(&path).unwrap();
                    std::fs::rename(replacement, &path).unwrap();
                }
                "truncation" => {
                    let header = original.iter().position(|&b| b == b'\n').unwrap() + 1;
                    OpenOptions::new()
                        .write(true)
                        .open(&path)
                        .unwrap()
                        .set_len(header as u64)
                        .unwrap();
                }
                _ => {
                    let at = if change == "first" {
                        original.iter().position(|&b| b == b'x').unwrap()
                    } else {
                        original.len() - 20
                    };
                    assert_eq!(original[at], b'x');
                    let mut writer = OpenOptions::new().write(true).open(&path).unwrap();
                    writer.seek(SeekFrom::Start(at as u64)).unwrap();
                    writer.write_all(b"y").unwrap();
                }
            }
            let restarted = parse("pi", &path, Some(&first.cursor), None).unwrap();
            let fresh = parse("pi", &path, None, None).unwrap();
            assert!(!restarted.append, "{change}");
            assert_eq!(restarted.messages.len(), fresh.messages.len());
            assert_eq!(restarted.cursor, fresh.cursor);
            assert_eq!(restarted.bytes_parsed, fresh.bytes_parsed);
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
    /// Small updates do not initialize workers just to compress one graph page.
    pool: &'static std::sync::LazyLock<rayon::ThreadPool>,
    batch: Vec<(i64, i64, Graph)>,
    nodes: usize,
}
impl GraphPages {
    const BATCH_NODES: usize = 8_192;
    pub(crate) fn new(pool: &'static std::sync::LazyLock<rayon::ThreadPool>) -> Self {
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
        let pack = |(source, bucket, graph)| Ok((source, bucket, pack_graph(&graph)?));
        let packed: Vec<(i64, i64, Vec<u8>)> = if batch.len() <= 1 || self.nodes <= 512 {
            batch.into_iter().map(pack).collect::<Result<_>>()?
        } else {
            self.pool
                .install(|| batch.into_par_iter().map(pack).collect::<Result<_>>())?
        };
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
fn identity(path: &Path, m: &std::fs::Metadata, file: &std::fs::File) -> Result<String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let _ = (path, file);
        Ok(format!("{}:{}", m.dev(), m.ino()))
    }
    #[cfg(windows)]
    {
        let _ = (path, m);
        Ok(registry::opened_file_identity(file)?.cache_key())
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = (path, file);
        Ok(format!("{:?}", m.created()?))
    }
}
const READ_CHUNK: usize = 16 * 1024 * 1024;

enum JsonlBlock<'a> {
    Bytes(&'a [u8]),
    Record(&'a mut std::fs::File),
}
impl JsonlBlock<'_> {
    fn chunks(&mut self, mut consume: impl FnMut(&[u8]) -> Result<()>) -> Result<()> {
        match self {
            Self::Bytes(bytes) => consume(bytes),
            Self::Record(file) => {
                file.rewind()?;
                let mut bytes = vec![0; 64 << 10];
                loop {
                    let n = file.read(&mut bytes)?;
                    if n == 0 {
                        break;
                    }
                    consume(&bytes[..n])?;
                }
                Ok(())
            }
        }
    }
}

/// Keep raw input bounded even when a single JSONL record is enormous. Oversized
/// records spill to disk and are deserialized from a buffered reader.
fn read_jsonl_blocks(
    file: impl Read,
    length: u64,
    mut consume: impl FnMut(JsonlBlock<'_>) -> Result<()>,
) -> Result<(usize, usize)> {
    let read_capacity = length.clamp(1, 64 << 10) as usize;
    let mut reader = BufReader::with_capacity(read_capacity, file.take(length));
    let mut block = Vec::with_capacity(length.min(READ_CHUNK as u64) as usize);
    let mut read = 0;
    let mut complete = 0;
    loop {
        let n = (&mut reader)
            .take((READ_CHUNK - block.len()) as u64)
            .read_until(b'\n', &mut block)?;
        read += n;
        if n == 0 {
            if !block.is_empty() {
                let tail = block.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
                if ingest::pi::validate_record(&block[tail..]).is_err() {
                    block.truncate(tail);
                }
                complete += block.len();
                consume(JsonlBlock::Bytes(&block))?;
            }
            break;
        }
        if block.len() < READ_CHUNK {
            continue;
        }
        if block.last() == Some(&b'\n') {
            complete += block.len();
            consume(JsonlBlock::Bytes(&block))?;
            block.clear();
            continue;
        }
        let tail = block.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
        if tail > 0 {
            complete += tail;
            consume(JsonlBlock::Bytes(&block[..tail]))?;
            let remaining = block.len() - tail;
            block.copy_within(tail.., 0);
            block.truncate(remaining);
            continue;
        }
        let mut record = tempfile::tempfile()?;
        record.write_all(&block)?;
        let mut record_len = block.len();
        loop {
            block.clear();
            let n = (&mut reader)
                .take(READ_CHUNK as u64)
                .read_until(b'\n', &mut block)?;
            read += n;
            record.write_all(&block)?;
            record_len += n;
            if n == 0 || block.last() == Some(&b'\n') {
                let terminated = block.last() == Some(&b'\n');
                record.rewind()?;
                if terminated || ingest::pi::validate_record_reader(BufReader::new(&record)).is_ok()
                {
                    complete += record_len;
                    consume(JsonlBlock::Record(&mut record))?;
                }
                break;
            }
        }
        block.clear();
    }
    Ok((read, complete))
}

#[cfg(test)]
fn read_jsonl(
    file: impl Read,
    length: u64,
    mut consume: impl FnMut(&[u8]) -> Result<()>,
) -> Result<(usize, usize)> {
    read_jsonl_blocks(file, length, |mut block| block.chunks(&mut consume))
}

fn malformed_record(
    spool: &mut Option<ingest::jsonl::RawRecords>,
    error: serde_json::Error,
    reader: impl Read,
) -> Result<ingest::pi::PreparedRecord> {
    if error.is_io() {
        return Err(error.into());
    }
    if spool.is_none() {
        *spool = Some(ingest::jsonl::RawRecords::new()?);
    }
    let raw = spool
        .as_mut()
        .expect("created malformed spool")
        .push(reader)?;
    Ok(ingest::pi::PreparedRecord::Malformed {
        error: error.to_string(),
        raw: Box::new(raw),
    })
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
    let id = identity(path, &meta, &file)?;
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
            let compact = |value, ordinal| -> Result<_> {
                let mut value = ingest::pi::compact_record(value);
                value["__agrep_ordinal"] = serde_json::json!(ordinal);
                Ok(Some(value))
            };
            let parsed: Vec<ingest::pi::PreparedRecord> = match block {
                JsonlBlock::Bytes(bytes) => {
                    let text = String::from_utf8_lossy(bytes);
                    let lines: Vec<_> = text.lines().collect();
                    records += lines.len();
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
                    let mut records = Vec::new();
                    for (n, value) in parsed.into_iter().enumerate() {
                        match value {
                            Ok(Some(value)) => {
                                records.push(ingest::pi::PreparedRecord::Value(value))
                            }
                            Ok(None) => {}
                            Err(error) => records.push(malformed_record(
                                &mut malformed,
                                error.downcast::<serde_json::Error>()?,
                                lines[n].as_bytes(),
                            )?),
                        }
                    }
                    records
                }
                JsonlBlock::Record(file) => {
                    records += 1;
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
                            vec![malformed_record(&mut malformed, error, file.take(length))?]
                        }
                    }
                }
            };
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
                &header,
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
        replay = Some(rebuilt);
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
    let nodes = pi_values
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
        .collect();
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

#[cfg(test)]
mod stream_tests {
    use super::*;

    #[test]
    fn oversized_final_records_keep_the_original_completion_rules() {
        let mut source = b"{}\n{\"ignored\":\"".to_vec();
        source.resize(2 * READ_CHUNK, b'x');
        for (tail, valid) in [
            (b"\"}".as_slice(), true),
            (b"\xff\"}", false),
            (b"unfinished", false),
        ] {
            let mut input = source.clone();
            input.extend_from_slice(tail);
            let mut consumed = 0;
            let extent = read_jsonl_blocks(input.as_slice(), input.len() as u64, |mut block| {
                block.chunks(|bytes| {
                    consumed += bytes.len();
                    Ok(())
                })
            })
            .unwrap();
            let expected = if valid { input.len() } else { 3 };
            assert_eq!(extent, (input.len(), expected));
            assert_eq!(consumed, expected);
        }
    }

    #[test]
    fn a_single_large_record_spills_before_deserialization() {
        let dir = tempfile::tempdir().unwrap();
        let mut file = tempfile::tempfile_in(dir.path()).unwrap();
        file.write_all(b"{\"details\":\"").unwrap();
        let padding = vec![b'x'; 64 << 10];
        for _ in 0..1024 {
            file.write_all(&padding).unwrap();
        }
        file.write_all(b"\"}\n").unwrap();
        let length = file.metadata().unwrap().len();
        file.rewind().unwrap();
        let mut spilled = 0;
        let (read, complete) = read_jsonl_blocks(file, length, |block| {
            match block {
                JsonlBlock::Bytes(bytes) => assert!(bytes.len() <= READ_CHUNK),
                JsonlBlock::Record(file) => {
                    assert_eq!(file.metadata()?.len(), length);
                    spilled += 1;
                }
            }
            Ok(())
        })
        .unwrap();
        assert_eq!((read as u64, complete as u64), (length, length));
        assert_eq!(spilled, 1);
    }

    #[test]
    fn checkpoints_do_not_depend_on_read_boundaries() {
        for length in [0, 37, CHECKPOINT_CHUNK, CHECKPOINT_CHUNK * 3 + 37] {
            let bytes: Vec<_> = (0..length).map(|i| (i % 251) as u8).collect();
            let first = chunk_digest(&bytes[..length.min(CHECKPOINT_CHUNK)]);
            let last_at = length.saturating_sub(1) / CHECKPOINT_CHUNK * CHECKPOINT_CHUNK;
            let last = chunk_digest(&bytes[last_at..]);
            for size in [17, CHECKPOINT_CHUNK - 1, CHECKPOINT_CHUNK + 13] {
                let mut checkpoint = Checkpoint::new(None, Vec::new());
                for block in bytes.chunks(size) {
                    checkpoint.extend(block);
                    assert!(checkpoint.tail.len() < CHECKPOINT_CHUNK);
                }
                assert_eq!(checkpoint.finish(), (first, last));
            }
        }
    }

    #[test]
    fn streamed_jsonl_keeps_the_complete_extent() {
        for (source, expected) in [
            (
                b"{}\r\n\n{\"a\":1}".as_slice(),
                b"{}\r\n\n{\"a\":1}".as_slice(),
            ),
            (b"{}\n{\"a\":".as_slice(), b"{}\n".as_slice()),
            (b"{}\n\xff\n".as_slice(), b"{}\n\xff\n".as_slice()),
            (b"broken".as_slice(), b"".as_slice()),
        ] {
            let mut got = Vec::new();
            let (read, complete) = read_jsonl(source, source.len() as u64, |block| {
                got.extend_from_slice(block);
                Ok(())
            })
            .unwrap();
            assert_eq!(read, source.len());
            assert_eq!(complete, expected.len());
            assert_eq!(got, expected);
        }
        let mut got = Vec::new();
        let (read, complete) = read_jsonl(b"{}\nappended".as_slice(), 3, |block| {
            got.extend_from_slice(block);
            Ok(())
        })
        .unwrap();
        assert_eq!((read, complete), (3, 3));
        assert_eq!(got, b"{}\n");
    }

    #[test]
    fn large_jsonl_reads_are_bounded_by_block_and_record() {
        let mut file = tempfile::tempfile().unwrap();
        let record = format!(
            "{{\"type\":\"noop\",\"ignored\":\"{}\"}}\n",
            "x".repeat(64 << 10)
        );
        let count = 4096;
        for _ in 0..count {
            std::io::Write::write_all(&mut file, record.as_bytes()).unwrap();
        }
        file.seek(SeekFrom::Start(0)).unwrap();
        struct Streaming<R> {
            reader: R,
            read: usize,
            consumed: std::rc::Rc<std::cell::Cell<usize>>,
        }
        impl<R: Read> Read for Streaming<R> {
            fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
                let n = self.reader.read(bytes)?;
                self.read += n;
                assert!(self.read <= self.consumed.get() + 2 * READ_CHUNK);
                Ok(n)
            }
        }
        let consumed = std::rc::Rc::new(std::cell::Cell::new(0));
        let reader = Streaming {
            reader: file,
            read: 0,
            consumed: consumed.clone(),
        };
        let mut peak = 0;
        let mut records = 0;
        let length = record.len() * count;
        let extent = read_jsonl(reader, length as u64, |block| {
            consumed.set(consumed.get() + block.len());
            peak = peak.max(block.len());
            records += block.iter().filter(|&&b| b == b'\n').count();
            Ok(())
        })
        .unwrap();
        assert_eq!(extent, (length, length));
        assert_eq!(records, count);
        assert!(peak <= READ_CHUNK + record.len());
        assert!(peak < length / 8);
    }
}

#[cfg(test)]
mod spill_tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn large_codex_snapshot_preserves_parser_and_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout-stream.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            r#"{{"type":"session_meta","payload":{{"id":"stream","cwd":"/work/test"}}}}"#
        )
        .unwrap();
        writeln!(
            file,
            r#"{{"type":"turn_context","payload":{{"model":"model-one"}}}}"#
        )
        .unwrap();
        writeln!(file, r#"{{"type":"response_item","timestamp":"2026-01-01T00:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_text","text":"streamed prompt"}}]}}}}"#).unwrap();
        let ignored = format!(
            "{{\"type\":\"noop\",\"padding\":\"{}\"}}\n",
            "x".repeat(8 << 10)
        );
        for _ in 0..=READ_CHUNK / ignored.len() {
            file.write_all(ignored.as_bytes()).unwrap();
        }
        writeln!(file, r#"{{"type":"event_msg","timestamp":"2026-01-01T00:00:00Z","payload":{{"type":"user_message","message":"streamed prompt"}}}}"#).unwrap();
        writeln!(file, r#"{{"type":"response_item","timestamp":"2026-01-01T00:00:01Z","payload":{{"type":"message","role":"assistant","content":[{{"type":"output_text","text":"streamed reply"}}]}}}}"#).unwrap();
        drop(file);
        let parsed = parse("codex", &path, None, None).unwrap();
        assert_eq!(parsed.outcome, ReadOutcome::Complete);
        assert_eq!(parsed.messages.len(), 1);
        // 0.4.9's codex::parse_file_with_tally attaches output_text to the
        // preceding attested user message, even across ignored records.
        assert_eq!(&*parsed.messages[0].text, "streamed prompt");
        assert_eq!(&*parsed.messages[0].reply, "streamed reply");
        let bytes = std::fs::read(&path).unwrap();
        let cursor: Cursor =
            serde_json::from_slice(&lz4_flex::decompress_size_prepended(&parsed.cursor).unwrap())
                .unwrap();
        assert_eq!(
            cursor.first_chunk,
            chunk_digest(&bytes[..bytes.len().min(CHECKPOINT_CHUNK)])
        );
        assert_eq!(
            cursor.last_chunk,
            chunk_digest(&bytes[(bytes.len() - 1) / CHECKPOINT_CHUNK * CHECKPOINT_CHUNK..])
        );
        assert_eq!(cursor.offset, bytes.len() as u64);
        assert_eq!(parsed.bytes_read, bytes.len());
        assert_eq!(parsed.bytes_parsed, bytes.len());
    }
}

#[cfg(test)]
mod large_metadata_tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn malformed_records_in_blocks_and_spills_keep_readable_rows_and_checkpoints() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            r#"{{"type":"session","id":"malformed","cwd":"/work/test"}}"#
        )
        .unwrap();
        file.write_all(b"{broken\n{\"broken\":\"").unwrap();
        let padding = vec![b'x'; 64 << 10];
        for _ in 0..=READ_CHUNK / padding.len() {
            file.write_all(&padding).unwrap();
        }
        file.write_all(b"\" invalid}\n").unwrap();
        writeln!(file, r#"{{"type":"message","id":"u","parentId":null,"message":{{"role":"user","content":"readable prompt"}}}}"#).unwrap();
        drop(file);
        let (parsed, tallies) = crate::intake::scoped(|| parse("pi", &path, None, None).unwrap());
        assert!(crate::intake::errors(&tallies));
        assert_eq!(parsed.outcome, ReadOutcome::Complete);
        assert_eq!(parsed.messages.len(), 1);
        assert_eq!(&*parsed.messages[0].text, "readable prompt");
        let raw = std::fs::read(&path).unwrap();
        let cursor: Cursor =
            serde_json::from_slice(&lz4_flex::decompress_size_prepended(&parsed.cursor).unwrap())
                .unwrap();
        assert_eq!(
            cursor.first_chunk,
            chunk_digest(&raw[..raw.len().min(CHECKPOINT_CHUNK)])
        );
        assert_eq!(
            cursor.last_chunk,
            chunk_digest(&raw[(raw.len() - 1) / CHECKPOINT_CHUNK * CHECKPOINT_CHUNK..])
        );
        assert_eq!(cursor.records, 4);
    }

    #[test]
    fn oversized_blank_record_is_skipped_before_the_next_message() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            r#"{{"type":"session","id":"blank-record","cwd":"/work/test"}}"#
        )
        .unwrap();
        let blank = vec![b' '; 64 << 10];
        for _ in 0..=READ_CHUNK / blank.len() {
            file.write_all(&blank).unwrap();
        }
        file.write_all(b"\n").unwrap();
        writeln!(file, r#"{{"type":"message","id":"u","parentId":null,"message":{{"role":"user","content":"blank record prompt"}}}}"#).unwrap();
        drop(file);
        let parsed = parse("pi", &path, None, None).unwrap();
        assert_eq!(parsed.outcome, ReadOutcome::Complete);
        assert_eq!(parsed.messages.len(), 1);
        assert_eq!(&*parsed.messages[0].text, "blank record prompt");
    }

    #[test]
    fn huge_ignored_tool_metadata_keeps_rows_and_raw_checkpoints() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            r#"{{"type":"session","id":"large-details","cwd":"/work/test"}}"#
        )
        .unwrap();
        writeln!(file, r#"{{"type":"message","id":"u","parentId":null,"message":{{"role":"user","content":"prompt"}}}}"#).unwrap();
        writeln!(file, r#"{{"type":"message","id":"a","parentId":"u","message":{{"role":"assistant","content":[{{"type":"toolCall","id":"call","name":"bash","arguments":{{"command":"true"}}}}]}}}}"#).unwrap();
        write!(file, r#"{{"type":"message","id":"r","parentId":"a","message":{{"role":"toolResult","toolCallId":"call","content":"ok","details":{{"rows":["#).unwrap();
        let item = format!("{{\"padding\":\"{}\"}}", "x".repeat(2048));
        for i in 0..=READ_CHUNK / item.len() {
            if i > 0 {
                file.write_all(b",").unwrap();
            }
            file.write_all(item.as_bytes()).unwrap();
        }
        file.write_all(b"]}}}\n").unwrap();
        drop(file);
        let parsed = parse("pi", &path, None, None).unwrap();
        assert_eq!(parsed.outcome, ReadOutcome::Complete);
        assert_eq!(parsed.messages.len(), 1);
        assert_eq!(&*parsed.messages[0].text, "prompt");
        // 0.4.9's pi parser discards tool details and takes the text as the output.
        assert_eq!(parsed.events.len(), 1);
        assert_eq!(parsed.events[0].output, "ok");
        assert_eq!(parsed.events[0].output_chars, 2);
        assert!(parsed
            .nodes
            .iter()
            .all(|n| n.body["message"].get("details").is_none()));
        let raw = std::fs::read(&path).unwrap();
        let cursor: Cursor =
            serde_json::from_slice(&lz4_flex::decompress_size_prepended(&parsed.cursor).unwrap())
                .unwrap();
        assert_eq!(
            cursor.first_chunk,
            chunk_digest(&raw[..raw.len().min(CHECKPOINT_CHUNK)])
        );
        assert_eq!(
            cursor.last_chunk,
            chunk_digest(&raw[(raw.len() - 1) / CHECKPOINT_CHUNK * CHECKPOINT_CHUNK..])
        );
        assert_eq!(cursor.offset, raw.len() as u64);
    }
}
