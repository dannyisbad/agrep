use std::{
    borrow::Cow,
    cell::RefCell,
    fs::{self, File},
    io::{self, BufRead, BufReader, Read, Seek, Write},
    path::Path,
};

pub(crate) trait Text {
    fn lines(&self) -> Box<dyn Iterator<Item = Cow<'_, str>> + '_>;
}

impl Text for str {
    fn lines(&self) -> Box<dyn Iterator<Item = Cow<'_, str>> + '_> {
        Box::new(str::lines(self).map(Cow::Borrowed))
    }
}

impl Text for String {
    fn lines(&self) -> Box<dyn Iterator<Item = Cow<'_, str>> + '_> {
        Text::lines(self.as_str())
    }
}

/// A private snapshot for parsers which inspect the same JSONL more than once.
/// Spooling preserves one generation without keeping the raw transcript in RAM.
pub(crate) struct FileText {
    file: File,
    error: RefCell<Option<io::Error>>,
}

impl FileText {
    pub(crate) fn new(file: File) -> Self {
        Self {
            file,
            error: RefCell::new(None),
        }
    }

    pub(crate) fn check(&self) -> io::Result<()> {
        match self.error.borrow_mut().take() {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }
}

impl Text for FileText {
    fn lines(&self) -> Box<dyn Iterator<Item = Cow<'_, str>> + '_> {
        let file = self.file.try_clone().and_then(|mut file| {
            file.rewind()?;
            Ok(file)
        });
        match file {
            Ok(file) => buffered_lines(BufReader::with_capacity(64 << 10, file), &self.error),
            Err(error) => {
                *self.error.borrow_mut() = Some(error);
                Box::new(std::iter::empty())
            }
        }
    }
}

/// A zstd-compressed JSONL source, decoded afresh on every pass so that no pass holds the
/// whole decompressed transcript in memory or on disk.
pub(crate) struct ZstdText<'a> {
    file: &'a File,
    error: RefCell<Option<io::Error>>,
}

impl<'a> ZstdText<'a> {
    pub(crate) fn new(file: &'a File) -> Self {
        Self {
            file,
            error: RefCell::new(None),
        }
    }

    /// The first decoding or read error of the latest pass; a pass stops at it.
    pub(crate) fn check(&self) -> io::Result<()> {
        match self.error.borrow_mut().take() {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }
}

impl Text for ZstdText<'_> {
    fn lines(&self) -> Box<dyn Iterator<Item = Cow<'_, str>> + '_> {
        let mut file = self.file;
        match file
            .rewind()
            .and_then(|()| zstd::stream::read::Decoder::new(file))
        {
            Ok(decoder) => buffered_lines(BufReader::with_capacity(64 << 10, decoder), &self.error),
            Err(error) => {
                *self.error.borrow_mut() = Some(error);
                Box::new(std::iter::empty())
            }
        }
    }
}

/// Above the largest compression ratio of the owner's 1237 Codex rollouts (12.2x, median
/// 4.8x), for a zstd source whose frames cannot be read without decoding it.
const UNREAD_ZSTD_RATIO: u64 = 16;

/// The bytes a source decodes to, read from a zstd source's frame headers rather than by
/// decoding it, and never less than the stored size.
pub(crate) fn decoded_len(path: &Path, stored: u64) -> u64 {
    if path.extension().is_none_or(|extension| extension != "zst") {
        return stored;
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    crate::ingest::registry::secure_read_options(&mut options);
    options
        .open(path)
        .ok()
        .and_then(|file| zstd_content_len(file, stored).ok().flatten())
        .unwrap_or(stored.saturating_mul(UNREAD_ZSTD_RATIO))
        .max(stored)
}

/// Each frame's declared content size, or for a frame that declares none the most its blocks
/// can hold; None when `stored` bytes are not a sequence of zstd frames. Block headers are
/// read in passing through a large buffer, since one read per header costs more than the bytes.
fn zstd_content_len(file: impl Read + Seek, stored: u64) -> io::Result<Option<u64>> {
    const BLOCK_MAX: u64 = 128 << 10;
    let mut file = BufReader::with_capacity(256 << 10, file);
    let mut position = 0;
    let mut read = |at: u64, bytes: &mut [u8]| {
        file.seek_relative(i64::try_from(at - position).map_err(io::Error::other)?)?;
        file.read_exact(bytes)?;
        position = at + bytes.len() as u64;
        io::Result::Ok(())
    };
    let (mut at, mut total) = (0u64, 0u64);
    while at < stored {
        let mut word = [0; 4];
        read(at, &mut word)?;
        let magic = u32::from_le_bytes(word);
        if magic & 0xFFFF_FFF0 == 0x184D_2A50 {
            read(at + 4, &mut word)?;
            at += 8 + u64::from(u32::from_le_bytes(word));
            continue;
        }
        if magic != 0xFD2F_B528 {
            return Ok(None);
        }
        let mut descriptor = [0; 1];
        read(at + 4, &mut descriptor)?;
        let descriptor = descriptor[0];
        let single_segment = descriptor & 0x20 != 0;
        let skipped = usize::from(!single_segment) + [0, 1, 2, 4][usize::from(descriptor & 3)];
        let size_len = [usize::from(single_segment), 2, 4, 8][usize::from(descriptor >> 6)];
        let mut header = [0; 13];
        read(at + 5, &mut header[..skipped + size_len])?;
        let mut size = [0; 8];
        size[..size_len].copy_from_slice(&header[skipped..skipped + size_len]);
        let declared =
            (size_len > 0).then(|| u64::from_le_bytes(size) + if size_len == 2 { 256 } else { 0 });
        at += 5 + (skipped + size_len) as u64;
        let mut blocks = 0u64;
        loop {
            let mut block = [0; 4];
            read(at, &mut block[..3])?;
            let block = u32::from_le_bytes(block);
            let kind = (block >> 1) & 3;
            if kind == 3 {
                return Ok(None);
            }
            at += 3 + if kind == 1 { 1 } else { u64::from(block >> 3) };
            blocks += 1;
            if block & 1 == 1 {
                break;
            }
        }
        at += if descriptor & 0x04 != 0 { 4 } else { 0 };
        total = total.saturating_add(declared.unwrap_or(blocks.saturating_mul(BLOCK_MAX)));
    }
    Ok((at == stored).then_some(total))
}

/// Lines as `str::lines` splits them, decoded like `String::from_utf8_lossy`; a read error
/// ends the iteration and is kept in `error`.
fn buffered_lines<'a>(
    mut reader: impl BufRead + 'a,
    error: &'a RefCell<Option<io::Error>>,
) -> Box<dyn Iterator<Item = Cow<'a, str>> + 'a> {
    Box::new(std::iter::from_fn(move || {
        let mut bytes = Vec::new();
        match reader.read_until(b'\n', &mut bytes) {
            Ok(0) => None,
            Ok(_) => {
                if bytes.last() == Some(&b'\n') {
                    bytes.pop();
                    if bytes.last() == Some(&b'\r') {
                        bytes.pop();
                    }
                }
                Some(Cow::Owned(String::from_utf8(bytes).unwrap_or_else(
                    |error| String::from_utf8_lossy(error.as_bytes()).into_owned(),
                )))
            }
            Err(read_error) => {
                *error.borrow_mut() = Some(read_error);
                None
            }
        }
    }))
}

/// Decode like String::from_utf8_lossy without retaining an entire record.
pub(crate) struct LossyReader<R> {
    reader: R,
    tail: Vec<u8>,
    decoded: std::io::Cursor<Vec<u8>>,
    only_whitespace: bool,
}
impl<R: std::io::Read> LossyReader<R> {
    pub(crate) fn new(reader: R) -> Self {
        Self {
            reader,
            tail: Vec::new(),
            decoded: std::io::Cursor::new(Vec::new()),
            only_whitespace: true,
        }
    }
    pub(crate) fn only_whitespace(&self) -> bool {
        self.only_whitespace
    }
}
impl<R: std::io::Read> std::io::Read for LossyReader<R> {
    fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
        if out.is_empty() {
            return Ok(0);
        }
        loop {
            let n = self.decoded.read(out)?;
            if n > 0 {
                return Ok(n);
            }
            let mut bytes = std::mem::take(&mut self.tail);
            let prefix = bytes.len();
            bytes.resize(prefix + (64 << 10), 0);
            let n = self.reader.read(&mut bytes[prefix..])?;
            bytes.truncate(prefix + n);
            if bytes.is_empty() {
                return Ok(0);
            }
            let mut at = 0;
            while let Err(error) = std::str::from_utf8(&bytes[at..]) {
                at += error.valid_up_to();
                match error.error_len() {
                    Some(length) => at += length,
                    None if n > 0 => {
                        self.tail = bytes.split_off(at);
                        break;
                    }
                    None => break,
                }
            }
            let decoded = String::from_utf8_lossy(&bytes);
            self.only_whitespace &= decoded.trim().is_empty();
            self.decoded = std::io::Cursor::new(decoded.into_owned().into_bytes());
        }
    }
}

pub(crate) struct RawRecords {
    file: std::sync::Arc<File>,
}
#[derive(Clone)]
pub(crate) struct RawRecord {
    file: std::sync::Arc<File>,
    start: u64,
    length: u64,
}
impl RawRecords {
    pub(crate) fn new() -> io::Result<Self> {
        Ok(Self {
            file: std::sync::Arc::new(tempfile::tempfile()?),
        })
    }
    pub(crate) fn push(&mut self, reader: impl std::io::Read) -> io::Result<RawRecord> {
        use std::io::Write;
        let mut file = &*self.file;
        let start = file.seek(std::io::SeekFrom::End(0))?;
        let length = io::copy(&mut LossyReader::new(reader), &mut file)?;
        file.write_all(b"\n")?;
        Ok(RawRecord {
            file: self.file.clone(),
            start,
            length,
        })
    }
}
impl RawRecord {
    pub(crate) fn len(&self) -> usize {
        self.length as usize
    }
    pub(crate) fn sample(&self) -> io::Result<String> {
        use std::io::Read;
        let mut file = self.file.try_clone()?;
        file.seek(std::io::SeekFrom::Start(self.start))?;
        let mut bytes = Vec::new();
        file.take(self.length.min(84)).read_to_end(&mut bytes)?;
        Ok(crate::intake::clip(&String::from_utf8_lossy(&bytes), 80).to_owned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn zstd_sources_count_their_decoded_size_from_frame_headers() {
        let text =
            b"{\"type\":\"response_item\",\"payload\":{\"text\":\"hello\"}}\n".repeat(20_000);
        let declared = zstd::bulk::compress(&text, 3).unwrap();
        let streamed = zstd::stream::encode_all(&text[..], 3).unwrap();
        let size = |bytes: &[u8]| zstd_content_len(io::Cursor::new(bytes), bytes.len() as u64);
        assert_eq!(size(&declared).unwrap(), Some(text.len() as u64));
        let bound = size(&streamed).unwrap().unwrap();
        // Streaming leaves the size undeclared: every block counts as full.
        assert!(bound > text.len() as u64 && bound <= text.len() as u64 + (128 << 10));
        let mut frames = declared.clone();
        frames.extend(0x184D_2A50u32.to_le_bytes());
        frames.extend(3u32.to_le_bytes());
        frames.extend(b"pad");
        frames.extend(&declared);
        assert_eq!(size(&frames).unwrap(), Some(2 * text.len() as u64));
        assert_eq!(size(&declared[..declared.len() - 1]).unwrap_or(None), None);
        assert_eq!(size(&text[..64]).unwrap(), None);

        let dir = tempfile::tempdir().unwrap();
        let plain = dir.path().join("rollout.jsonl");
        let compressed = dir.path().join("rollout.jsonl.zst");
        let unreadable = dir.path().join("broken.jsonl.zst");
        std::fs::write(&compressed, &declared).unwrap();
        std::fs::write(&unreadable, &text[..64]).unwrap();
        assert_eq!(decoded_len(&plain, 7), 7);
        assert_eq!(
            decoded_len(&compressed, declared.len() as u64),
            text.len() as u64
        );
        assert_eq!(decoded_len(&unreadable, 64), 64 * UNREAD_ZSTD_RATIO);
    }

    #[test]
    fn streamed_lossy_utf8_matches_whole_input_at_every_boundary() {
        struct Short<'a> {
            bytes: &'a [u8],
            chunk: usize,
        }
        impl std::io::Read for Short<'_> {
            fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
                let n = self.chunk.min(out.len()).min(self.bytes.len());
                out[..n].copy_from_slice(&self.bytes[..n]);
                self.bytes = &self.bytes[n..];
                Ok(n)
            }
        }
        use std::io::Read;
        let bytes = b"a\xc3\xa9\xf0\x9f\x8c\x8d\xff\xe0\x80x\xed\xa0\x80\xf0\x9f";
        for chunk in 1..=bytes.len() {
            let mut actual = String::new();
            LossyReader::new(Short { bytes, chunk })
                .read_to_string(&mut actual)
                .unwrap();
            assert_eq!(actual, String::from_utf8_lossy(bytes), "chunk {chunk}");
        }
    }

    #[test]
    fn file_lines_match_text_lines_and_can_be_replayed() {
        for bytes in [
            b"one\r\n\nlast\r".as_slice(),
            b"\n".as_slice(),
            b"".as_slice(),
            b"a\xff\nb".as_slice(),
        ] {
            let mut file = tempfile::tempfile().unwrap();
            file.write_all(bytes).unwrap();
            let text = FileText::new(file);
            let expected: Vec<_> = String::from_utf8_lossy(bytes)
                .lines()
                .map(str::to_owned)
                .collect();
            for _ in 0..2 {
                assert_eq!(text.lines().collect::<Vec<_>>(), expected);
                text.check().unwrap();
            }
        }
    }
}

const INLINE_STRING: usize = 1 << 20;
pub(crate) const STRING_MARKER: &str = "\0agrep_raw:";

/// Leave oversized plain strings in the private record file until projection keeps them.
pub(crate) struct LargeStrings<R> {
    reader: R,
    position: u64,
    output: std::io::Cursor<Vec<u8>>,
}
impl<R: BufRead> LargeStrings<R> {
    pub(crate) fn new(reader: R) -> Self {
        Self {
            reader,
            position: 0,
            output: std::io::Cursor::new(Vec::new()),
        }
    }
    fn unsupported() -> io::Error {
        io::Error::new(
            io::ErrorKind::Unsupported,
            "string requires the ordinary JSON reader",
        )
    }
    fn plain(bytes: &[u8]) -> bool {
        bytes.iter().all(|&byte| (0x20..0x80).contains(&byte))
    }
    fn string(&mut self, mut output: Vec<u8>) -> io::Result<Vec<u8>> {
        self.reader.consume(1);
        self.position += 1;
        let start = self.position;
        output.push(b'"');
        let mut escaped = false;
        let mut large = false;
        loop {
            let bytes = self.reader.fill_buf()?;
            if bytes.is_empty() {
                return Err(io::ErrorKind::UnexpectedEof.into());
            }
            let end = memchr::memchr2(b'"', b'\\', bytes);
            let n = end.unwrap_or(bytes.len());
            let delimiter = end.map(|at| bytes[at]);
            if !large && output.len() + n > INLINE_STRING {
                if escaped || !Self::plain(&output[1..]) {
                    return Err(Self::unsupported());
                }
                large = true;
                output.clear();
            }
            if large {
                if !Self::plain(&bytes[..n]) {
                    return Err(Self::unsupported());
                }
            } else {
                output.extend_from_slice(&bytes[..n]);
            }
            self.reader.consume(n);
            self.position += n as u64;
            match delimiter {
                Some(b'"') => {
                    let length = self.position - start;
                    self.reader.consume(1);
                    self.position += 1;
                    if large {
                        write!(output, "\"\\u0000agrep_raw:{start}:{length}\"")?;
                    } else {
                        output.push(b'"');
                        // Every possible original marker takes the unchanged-reader fallback.
                        if output.starts_with(br#""\u0000"#) {
                            return Err(Self::unsupported());
                        }
                    }
                    return Ok(output);
                }
                Some(b'\\') => {
                    if large {
                        return Err(Self::unsupported());
                    }
                    escaped = true;
                    self.reader.consume(1);
                    self.position += 1;
                    let mut byte = [0];
                    std::io::Read::read_exact(&mut self.reader, &mut byte)?;
                    self.position += 1;
                    output.push(b'\\');
                    output.push(byte[0]);
                }
                None => {}
                _ => unreachable!(),
            }
        }
    }
    fn refill(&mut self) -> io::Result<()> {
        let mut output = std::mem::take(self.output.get_mut());
        output.clear();
        let bytes = self.reader.fill_buf()?;
        let n = memchr::memchr(b'"', bytes).unwrap_or(bytes.len());
        if n > 0 {
            output.extend_from_slice(&bytes[..n]);
            self.reader.consume(n);
            self.position += n as u64;
        } else if !bytes.is_empty() {
            output = self.string(output)?;
        }
        self.output = std::io::Cursor::new(output);
        Ok(())
    }
}
impl<R: BufRead> std::io::Read for LargeStrings<R> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        if self.output.position() == self.output.get_ref().len() as u64 {
            self.refill()?;
        }
        self.output.read(bytes)
    }
}

#[cfg(test)]
mod string_tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn oversized_ascii_string_uses_bounded_storage() {
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(b"{\"unused\":\"").unwrap();
        let chunk = vec![b'x'; 64 << 10];
        for _ in 0..512 {
            file.write_all(&chunk).unwrap();
        }
        file.write_all(b"\",\"keep\":1}").unwrap();
        file.rewind().unwrap();
        let mut reader = LargeStrings::new(BufReader::with_capacity(64 << 10, file));
        let mut output = Vec::new();
        let mut bytes = [0; 4096];
        loop {
            let n = reader.read(&mut bytes).unwrap();
            assert!(reader.output.get_ref().capacity() <= 2 * INLINE_STRING);
            if n == 0 {
                break;
            }
            output.extend_from_slice(&bytes[..n]);
        }
        assert!(output.len() < 100);
        let value: serde_json::Value = serde_json::from_slice(&output).unwrap();
        assert_eq!(value["keep"], 1);
        assert!(value["unused"].as_str().unwrap().starts_with(STRING_MARKER));
    }
}
