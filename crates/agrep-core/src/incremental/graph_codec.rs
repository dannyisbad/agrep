#[cfg(test)]
use super::checkpoint::CHECKPOINT_CHUNK;
#[cfg(test)]
use super::parse::parse;
use anyhow::Context;
use rusqlite::OptionalExtension;
#[cfg(test)]
use std::io::Seek;

use anyhow::Result;
use rusqlite::{params, Connection};
use std::collections::BTreeMap;
#[cfg(test)]
use std::fs::OpenOptions;
#[cfg(test)]
use std::io::SeekFrom;
#[cfg(test)]
use std::path::Path;

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
pub(super) struct GraphNode {
    pub(super) parent: Option<String>,
    pub(super) ordinal: usize,
    pub(super) record: serde_json::Value,
}

pub(super) type Graph = BTreeMap<String, GraphNode>;
pub(super) fn bucket(id: &str) -> i64 {
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
pub(super) fn pack_graph(graph: &Graph) -> Result<Vec<u8>> {
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
pub(super) fn graph(c: &Connection, source: i64, bucket: i64) -> Result<Graph> {
    let bytes: Option<Vec<u8>> = c
        .query_row(
            "SELECT body FROM parser_graph WHERE source=?1 AND bucket=?2",
            params![source, bucket],
            |r| r.get(0),
        )
        .optional()?;
    bytes.map_or_else(|| Ok(Graph::new()), |b| unpack_graph(&b))
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
