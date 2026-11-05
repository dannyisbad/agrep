use std::{
    borrow::Cow,
    cell::RefCell,
    fs::File,
    io::{self, BufRead, BufReader, Seek, Write},
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
        let mut reader = match file {
            Ok(file) => BufReader::with_capacity(64 << 10, file),
            Err(error) => {
                *self.error.borrow_mut() = Some(error);
                return Box::new(std::iter::empty());
            }
        };
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
                Err(error) => {
                    *self.error.borrow_mut() = Some(error);
                    None
                }
            }
        }))
    }
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
