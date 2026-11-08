//! Bounded incremental reads of journal files: JSON lines from a byte offset, or one
//! whole JSON document per file.
use super::{cap, journal, now, text, Source, Watcher, JSON_CAP, LINE_CAP, WINDOW_MS};
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::{
    fs::{self, File},
    io::{BufRead, BufReader, Read, Seek, SeekFrom},
    path::Path,
};

pub(super) struct BoundedLine {
    pub body: Vec<u8>,
    pub bytes: u64,
    pub complete: bool,
    pub oversized: bool,
}
pub(super) fn bounded_line(
    reader: &mut impl BufRead,
    discarding: bool,
) -> std::io::Result<BoundedLine> {
    let mut line = BoundedLine {
        body: Vec::new(),
        bytes: 0,
        complete: false,
        oversized: discarding,
    };
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Ok(line);
        }
        let length = available
            .iter()
            .position(|&b| b == b'\n')
            .map_or(available.len(), |n| n + 1);
        line.complete = available[length - 1] == b'\n';
        line.bytes += length as u64;
        if !line.oversized && line.body.len().saturating_add(length) <= LINE_CAP {
            line.body.extend_from_slice(&available[..length]);
        } else {
            line.oversized = true;
            line.body.clear();
        }
        reader.consume(length);
        if line.complete {
            return Ok(line);
        }
    }
}

/// Identity describes replacement, not growth.
fn identity(meta: &fs::Metadata) -> String {
    #[cfg(not(unix))]
    {
        use std::os::windows::fs::MetadataExt;
        format!("{}", meta.creation_time())
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        format!("{}:{}", meta.dev(), meta.ino())
    }
}

impl Source {
    /// A new or replaced journal: metadata comes from its first lines, and reading starts
    /// at the first line beginning in its last 64 KiB.
    fn rewind(
        &mut self,
        agent: &str,
        path: &Path,
        file: &File,
        meta: &fs::Metadata,
        identity: String,
    ) -> Result<()> {
        *self = Source {
            identity,
            ..Source::default()
        };
        self.meta = journal::Meta::from_path(agent, path);
        if path.extension().is_none_or(|e| e != "jsonl") {
            return Ok(());
        }
        let mut head = BufReader::new(file).take(meta.len());
        for _ in 0..12 {
            let line = bounded_line(&mut head, false)?;
            if line.bytes == 0 {
                break;
            }
            if line.oversized {
                self.skipped_records += 1;
            } else if let Ok(row) = serde_json::from_slice(&line.body) {
                self.meta.header(agent, &row);
            }
            if !line.complete {
                break;
            }
        }
        if meta.len() > 65_536 {
            self.offset = meta.len() - 65_536;
            let mut reader = BufReader::new(file);
            reader.seek(SeekFrom::Start(self.offset))?;
            let mut reader = reader.take(meta.len().saturating_sub(self.offset));
            let line = bounded_line(&mut reader, true)?;
            self.offset += line.bytes;
            self.discarding_line = !line.complete;
        }
        Ok(())
    }
    fn skip_oversized_json(&mut self, len: u64) {
        if !self.oversized_json {
            self.skipped_records += 1;
        }
        self.oversized_json = true;
        self.offset = len;
    }
}

impl Watcher {
    pub(super) fn journal(
        &mut self,
        agent: &str,
        path: &Path,
        source: &mut Source,
        sink: &mut impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        let mut options = fs::OpenOptions::new();
        options.read(true);
        crate::ingest::registry::secure_read_options(&mut options);
        let file = options.open(path)?;
        let meta = file.metadata()?;
        anyhow::ensure!(meta.is_file(), "source is not a regular file");
        let identity = identity(&meta);
        if source.identity.is_empty()
            && now()
                - meta
                    .modified()?
                    .duration_since(std::time::UNIX_EPOCH)?
                    .as_millis() as i64
                > WINDOW_MS
        {
            return Ok(());
        }
        if source.identity != identity || source.offset > meta.len() {
            source.rewind(agent, path, &file, &meta, identity)?;
        }
        if path.extension().is_some_and(|e| e == "json") {
            return self.json_file(agent, &file, meta.len(), source, sink);
        }
        self.lines(agent, &file, meta.len(), source, sink)
    }
    /// A whole-file JSON record, read again whenever its size changes.
    fn json_file(
        &mut self,
        agent: &str,
        file: &File,
        len: u64,
        source: &mut Source,
        sink: &mut impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        if len > JSON_CAP as u64 {
            source.skip_oversized_json(len);
            return Ok(());
        }
        if source.offset == len {
            return Ok(());
        }
        let mut reader = BufReader::new(file);
        reader.seek(SeekFrom::Start(0))?;
        let mut bytes = Vec::new();
        reader.take(JSON_CAP as u64 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > JSON_CAP {
            source.skip_oversized_json(len);
            return Ok(());
        }
        source.oversized_json = false;
        let row: Value = serde_json::from_slice(&bytes)?;
        let sender = text(&row["sender"]);
        if sender.contains("/task-") {
            self.emit(json!({"agent":agent,"session":source.meta.session,"type":"subagent_result","ts":now(),"name":row["renderDetails"]["messageTitle"].as_str().unwrap_or(sender),"output":cap(text(&row["content"]),400)}),sink)?;
        }
        source.offset = len;
        Ok(())
    }
    /// Complete lines from the source's offset; a partial last line waits for its newline.
    fn lines(
        &mut self,
        agent: &str,
        file: &File,
        len: u64,
        source: &mut Source,
        sink: &mut impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        let mut reader = BufReader::new(file);
        reader.seek(SeekFrom::Start(source.offset))?;
        let mut reader = reader.take(len.saturating_sub(source.offset));
        loop {
            let line = bounded_line(&mut reader, source.discarding_line)?;
            if line.bytes == 0 {
                break;
            }
            if line.oversized {
                if !source.discarding_line {
                    source.skipped_records += 1;
                }
                source.discarding_line = !line.complete;
                source.offset += line.bytes;
                if !line.complete {
                    break;
                }
                continue;
            }
            if !line.complete {
                break;
            }
            if !line.body.iter().all(u8::is_ascii_whitespace) {
                self.record(agent, &line.body, source, sink)?;
            }
            source.offset += line.bytes;
        }
        Ok(())
    }
    /// Parser state advances only once every event of the record was emitted.
    fn record(
        &mut self,
        agent: &str,
        body: &[u8],
        source: &mut Source,
        sink: &mut impl FnMut(&Value) -> Result<()>,
    ) -> Result<()> {
        let row: Value = serde_json::from_slice(body)
            .with_context(|| format!("invalid JSON at byte {}", source.offset))?;
        let mut parsed_meta = source.meta.clone();
        for e in journal::parse(agent, &mut parsed_meta, &row) {
            self.emit(e, sink)?;
        }
        source.meta = parsed_meta;
        Ok(())
    }
}
