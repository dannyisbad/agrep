#[cfg(test)]
use super::checkpoint::{chunk_digest, Checkpoint, CHECKPOINT_CHUNK};
#[cfg(test)]
use super::parse::parse;
use std::io::BufRead;
use std::io::Seek;

#[cfg(test)]
use super::Cursor;
use crate::ingest;
#[cfg(windows)]
use crate::ingest::registry;
#[cfg(test)]
use crate::intake_batch::ReadOutcome;
use anyhow::Result;
#[cfg(test)]
use std::io::SeekFrom;
use std::io::{BufReader, Read, Write};
use std::path::Path;

pub fn supports(agent: &str, path: &Path) -> bool {
    matches!(agent, "pi" | "claude" | "codex") && path.extension().is_some_and(|s| s == "jsonl")
}
pub(super) fn identity(path: &Path, m: &std::fs::Metadata, file: &std::fs::File) -> Result<String> {
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
pub(super) const READ_CHUNK: usize = 16 * 1024 * 1024;

pub(super) enum JsonlBlock<'a> {
    Bytes(&'a [u8]),
    Record(&'a mut std::fs::File),
}
impl JsonlBlock<'_> {
    pub(super) fn chunks(&mut self, mut consume: impl FnMut(&[u8]) -> Result<()>) -> Result<()> {
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
pub(super) fn read_jsonl_blocks(
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

pub(super) fn malformed_record(
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

#[cfg(test)]
mod resume_tests {
    use super::*;

    fn cursor(parsed: &super::super::Parsed) -> Cursor {
        serde_json::from_slice(&lz4_flex::decompress_size_prepended(&parsed.cursor).unwrap())
            .unwrap()
    }

    /// A pi record whose tool call has no id, so its event id carries the record's line.
    fn pi_record(n: usize) -> String {
        let parent = n
            .checked_sub(1)
            .map_or(serde_json::Value::Null, |p| format!("e{p}").into());
        serde_json::json!({"type":"message","id":format!("e{n}"),"parentId":parent,"message":{"role":"assistant","content":[{"type":"text","text":format!("reply {n}")},{"type":"toolCall","name":"bash","arguments":{"command":format!("echo {n}")}}]}}).to_string()
    }

    #[test]
    fn a_record_parsed_before_its_newline_resumes_like_a_fresh_read() {
        let header = r#"{"type":"session","id":"open-line","cwd":"/work/test"}"#;
        for newline in ["\n", "\r\n"] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("session.jsonl");
            let written = format!("{header}{newline}{}{newline}{}", pi_record(0), pi_record(1));
            std::fs::write(&path, &written).unwrap();
            let first = parse("pi", &path, None, None).unwrap();
            assert_eq!(cursor(&first).offset, written.len() as u64);
            let appended = format!("{newline}{}{newline}", pi_record(2));
            let mut file = std::fs::OpenOptions::new()
                .append(true)
                .open(&path)
                .unwrap();
            file.write_all(appended.as_bytes()).unwrap();
            drop(file);
            let resumed = parse("pi", &path, Some(&first.cursor), None).unwrap();
            let fresh = parse("pi", &path, None, None).unwrap();
            assert!(resumed.append, "{newline:?}");
            // A resumed pass re-emits the calls still waiting for a result.
            let calls = |parsed: &super::super::Parsed| -> Vec<String> {
                parsed.events.iter().map(|e| e.call_id.clone()).collect()
            };
            assert_eq!(calls(&resumed), calls(&fresh), "{newline:?}");
            let ordinals = |parsed: &super::super::Parsed| -> Vec<usize> {
                parsed.nodes.iter().map(|node| node.ordinal).collect()
            };
            assert_eq!(ordinals(&resumed), ordinals(&fresh)[2..], "{newline:?}");
            let (resumed, fresh) = (cursor(&resumed), cursor(&fresh));
            assert_eq!(
                (
                    resumed.offset,
                    resumed.records,
                    resumed.first_chunk,
                    resumed.last_chunk
                ),
                (
                    fresh.offset,
                    fresh.records,
                    fresh.first_chunk,
                    fresh.last_chunk
                ),
                "{newline:?}"
            );
        }
    }

    #[test]
    fn an_append_that_continues_a_parsed_line_reparses_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let header = r#"{"type":"session","id":"continued","cwd":"/work/test"}"#;
        std::fs::write(&path, format!("{header}\n{}", pi_record(0))).unwrap();
        let first = parse("pi", &path, None, None).unwrap();
        assert_eq!(first.events.len(), 1);
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        file.write_all(format!("{}\n{}\n", pi_record(1), pi_record(2)).as_bytes())
            .unwrap();
        drop(file);
        let resumed = parse("pi", &path, Some(&first.cursor), None).unwrap();
        let fresh = parse("pi", &path, None, None).unwrap();
        assert!(!resumed.append && resumed.reset_nodes);
        let calls = |parsed: &super::super::Parsed| -> Vec<String> {
            parsed.events.iter().map(|e| e.call_id.clone()).collect()
        };
        assert_eq!(calls(&resumed), calls(&fresh));
        assert_eq!(cursor(&resumed).records, cursor(&fresh).records);
    }

    #[test]
    fn resume_state_maps_are_written_in_key_order() {
        let calls: Vec<String> = (0..40)
            .map(|n| format!("call{:02}", (n * 17) % 40))
            .collect();
        let pi = std::iter::once(r#"{"type":"session","id":"order","cwd":"/work/test"}"#.into())
            .chain(calls.iter().enumerate().map(|(n, id)| {
                let parent = n
                    .checked_sub(1)
                    .map_or(serde_json::Value::Null, |p| format!("a{p}").into());
                serde_json::json!({"type":"message","id":format!("a{n}"),"parentId":parent,"message":{"role":"assistant","content":[{"type":"toolCall","id":id,"name":"bash","arguments":{}}]}}).to_string()
            }))
            .collect::<Vec<String>>();
        let claude = calls
            .iter()
            .enumerate()
            .map(|(n, id)| {
                serde_json::json!({"type":"assistant","uuid":format!("a{n}"),"sessionId":"order","cwd":format!("/work/project{}/src", n % 5),"message":{"role":"assistant","content":[{"type":"tool_use","id":id,"name":"Read","input":{"file_path":format!("/work/project{}/src/main.rs", n % 7)}}]}}).to_string()
            })
            .collect::<Vec<String>>();
        let codex = std::iter::once(
            r#"{"type":"session_meta","payload":{"id":"order","cwd":"/work/test"}}"#.into(),
        )
        .chain(calls.iter().map(|id| {
            serde_json::json!({"type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{}","call_id":id}}).to_string()
        }))
        .collect::<Vec<String>>();
        for (agent, lines, maps) in [
            ("pi", pi, &["pending"][..]),
            ("claude", claude, &["pending", "project_votes"][..]),
            ("codex", codex, &["pending"][..]),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("session.jsonl");
            std::fs::write(&path, lines.join("\n") + "\n").unwrap();
            let parsed = parse(agent, &path, None, None).unwrap();
            let state: serde_json::Value = serde_json::from_str(&cursor(&parsed).state).unwrap();
            for map in maps {
                let keys: Vec<&String> = state[*map].as_object().unwrap().keys().collect();
                let mut sorted = keys.clone();
                sorted.sort();
                assert!(keys.len() > 1, "{agent} {map}");
                assert_eq!(keys, sorted, "{agent} {map}");
            }
        }
    }
}
