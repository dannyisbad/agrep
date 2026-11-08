use super::Cursor;
use anyhow::Result;
use sha2::Digest;
use sha2::Sha256;

pub(super) const CHECKPOINT_CHUNK: usize = 64 * 1024;

pub(super) fn chunk_digest(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

pub(super) struct Checkpoint {
    first: Option<[u8; 32]>,
    last: [u8; 32],
    pub(super) tail: Vec<u8>,
}
impl Checkpoint {
    pub(super) fn new(cursor: Option<&Cursor>, tail: Vec<u8>) -> Self {
        Self {
            first: cursor
                .filter(|c| c.offset >= CHECKPOINT_CHUNK as u64)
                .map(|c| c.first_chunk),
            last: cursor.map_or_else(|| chunk_digest(&[]), |c| c.last_chunk),
            tail,
        }
    }
    pub(super) fn extend(&mut self, mut bytes: &[u8]) {
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
    pub(super) fn finish(mut self) -> ([u8; 32], [u8; 32]) {
        if !self.tail.is_empty() {
            self.last = chunk_digest(&self.tail);
        }
        (self.first.unwrap_or(self.last), self.last)
    }
}

pub(super) fn read_at(
    file: &std::fs::File,
    mut bytes: &mut [u8],
    mut offset: u64,
) -> std::io::Result<()> {
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
pub(super) fn verify_chunks(
    file: &std::fs::File,
    cursor: &Cursor,
) -> Result<(bool, Vec<u8>, usize)> {
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
