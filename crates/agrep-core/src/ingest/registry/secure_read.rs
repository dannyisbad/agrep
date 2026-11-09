//! Reads of source files that never follow a link or block on a special file, and that fail
//! when the file is replaced (or, for strict snapshots, changed at all) under the read.

use std::fs;
use std::io::{ErrorKind, Read, Seek, SeekFrom};
use std::path::Path;

use super::change_token::open_file_change_token;
use super::plain_file::{same_growing_file, same_plain_snapshot};
use super::{metadata_is_link, secure_read_options, ChangeToken};

pub(crate) fn open_regular_snapshot(
    path: &Path,
    before: &fs::Metadata,
) -> std::io::Result<fs::File> {
    if metadata_is_link(before) || !before.is_file() {
        return Err(std::io::Error::new(
            ErrorKind::InvalidInput,
            "source is not a plain regular file",
        ));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let file = options.open(path)?;
    let opened = file.metadata()?;
    let after = fs::symlink_metadata(path)?;
    if metadata_is_link(&opened)
        || !opened.is_file()
        || !same_plain_snapshot(before, &opened)
        || !same_plain_snapshot(&opened, &after)
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while opening",
        ));
    }
    Ok(file)
}

#[cfg(not(windows))]
fn verify_regular_snapshot(
    path: &Path,
    file: &fs::File,
    before: &fs::Metadata,
) -> std::io::Result<()> {
    let opened = file.metadata()?;
    let after = fs::symlink_metadata(path)?;
    if !same_plain_snapshot(before, &opened) || !same_plain_snapshot(&opened, &after) {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn verify_regular_snapshot(
    path: &Path,
    file: &fs::File,
    before: &fs::Metadata,
) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;

    use super::windows_handle::windows_handle_state;

    let opened = file.metadata()?;
    if !same_plain_snapshot(before, &opened) {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let path_after = options.open(path)?;
    let opened_state = windows_handle_state(file.as_raw_handle())?;
    let path_state = windows_handle_state(path_after.as_raw_handle())?;
    if opened_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || path_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || opened_state != path_state
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

#[cfg(not(windows))]
fn verify_growing_snapshot(path: &Path, file: &fs::File, read: u64) -> std::io::Result<()> {
    let opened = file.metadata()?;
    let after = fs::symlink_metadata(path)?;
    if !opened.is_file()
        || !after.is_file()
        || metadata_is_link(&after)
        || !same_growing_file(&opened, &after)
        || opened.len() < read
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn verify_growing_snapshot(path: &Path, file: &fs::File, read: u64) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;

    use super::windows_handle::windows_handle_state;

    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let path_after = options.open(path)?;
    let opened_state = windows_handle_state(file.as_raw_handle())?;
    let path_state = windows_handle_state(path_after.as_raw_handle())?;
    if opened_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || path_state.attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || opened_state.identity != path_state.identity
        || opened_state.attributes != path_state.attributes
        || opened_state.size < read
    {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while reading",
        ));
    }
    Ok(())
}

/// `symlink_metadata(path)`, None when nothing is there.
fn present_metadata(path: &Path) -> std::io::Result<Option<fs::Metadata>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => Ok(Some(metadata)),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

/// Read the bytes a source held when it was opened, tolerating appends past them.
///
/// An agent transcript is an append-only log that grows under every reader, so the strict
/// snapshot readers below - which require len, mtime and ctime to be untouched - refuse the
/// busiest session on the box on every pass, forever. The leading `len` bytes are a real
/// generation of the file; the appended tail is the next pass's work. Replacement and
/// truncation still fail. A truncate that regrows past `len` inside one read is invisible to
/// metadata and is corrected on the next pass, whose stamp differs.
fn read_growing_regular_file_from_meta(
    path: &Path,
    max_bytes: u64,
    before: fs::Metadata,
) -> std::io::Result<Vec<u8>> {
    let (mut file, opened) = open_growing_file(path, &before)?;
    let len = opened.len();
    if len > max_bytes {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            format!("source exceeds {max_bytes} bytes"),
        ));
    }
    let mut body = Vec::with_capacity(len as usize);
    file.by_ref().take(len).read_to_end(&mut body)?;
    if body.len() as u64 != len {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source shrank while reading",
        ));
    }
    verify_growing_snapshot(path, &file, len)?;
    Ok(body)
}

/// Open the plain regular file `before` describes, failing if it was replaced in between;
/// beside it the metadata of the opened handle.
fn open_growing_file(
    path: &Path,
    before: &fs::Metadata,
) -> std::io::Result<(fs::File, fs::Metadata)> {
    if metadata_is_link(before) || !before.is_file() {
        return Err(std::io::Error::new(
            ErrorKind::InvalidInput,
            "source is not a plain regular file",
        ));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true);
    secure_read_options(&mut options);
    let file = options.open(path)?;
    let opened = file.metadata()?;
    if metadata_is_link(&opened) || !opened.is_file() || !same_growing_file(before, &opened) {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source changed while opening",
        ));
    }
    Ok((file, opened))
}

pub fn read_growing_regular_file(path: &Path, max_bytes: u64) -> std::io::Result<Option<Vec<u8>>> {
    let Some(before) = present_metadata(path)? else {
        return Ok(None);
    };
    read_growing_regular_file_from_meta(path, max_bytes, before).map(Some)
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BoundedRegularFileSnapshot {
    pub bytes: Vec<u8>,
    pub modified: std::time::SystemTime,
}

fn read_bounded_regular_file_snapshot_from_meta(
    path: &Path,
    max_bytes: u64,
    before: fs::Metadata,
) -> std::io::Result<BoundedRegularFileSnapshot> {
    if before.len() > max_bytes {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            format!("source exceeds {max_bytes} bytes"),
        ));
    }
    let mut file = open_regular_snapshot(path, &before)?;
    let opened = file.metadata()?;
    let mut body = Vec::with_capacity(opened.len() as usize);
    file.by_ref()
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut body)?;
    if body.len() as u64 != before.len() || body.len() as u64 > max_bytes {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "source length changed while reading",
        ));
    }
    verify_regular_snapshot(path, &file, &before)?;
    Ok(BoundedRegularFileSnapshot {
        bytes: body,
        modified: opened.modified()?,
    })
}

pub fn read_bounded_regular_file(path: &Path, max_bytes: u64) -> std::io::Result<Option<Vec<u8>>> {
    let Some(before) = present_metadata(path)? else {
        return Ok(None);
    };
    read_bounded_regular_file_snapshot_from_meta(path, max_bytes, before)
        .map(|snapshot| Some(snapshot.bytes))
}

/// Stream a source that is rewritten rather than appended: `read` gets the opened file, and
/// the whole read fails if the file changed at all under it. None when nothing is there.
pub(crate) fn read_regular_snapshot<T>(
    path: &Path,
    read: impl FnOnce(&fs::File) -> T,
) -> std::io::Result<Option<T>> {
    let Some(before) = present_metadata(path)? else {
        return Ok(None);
    };
    let file = open_regular_snapshot(path, &before)?;
    let value = read(&file);
    verify_regular_snapshot(path, &file, &before)?;
    Ok(Some(value))
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RegularFileEdgeSnapshot {
    pub len: u64,
    pub modified: std::time::SystemTime,
    pub change_token: ChangeToken,
    pub head: Vec<u8>,
    pub tail: Vec<u8>,
}

fn regular_file_edge_snapshot_from_meta(
    path: &Path,
    edge_bytes: usize,
    before: fs::Metadata,
) -> std::io::Result<RegularFileEdgeSnapshot> {
    let mut file = open_regular_snapshot(path, &before)?;
    let opened = file.metadata()?;
    let len = opened.len();
    let head_len = len.min(edge_bytes as u64) as usize;
    let mut head = vec![0; head_len];
    file.read_exact(&mut head)?;
    let mut tail = Vec::new();
    if len > edge_bytes as u64 && edge_bytes != 0 {
        let tail_len = len.min(edge_bytes as u64) as usize;
        let offset = i64::try_from(tail_len).map_err(|_| {
            std::io::Error::new(ErrorKind::InvalidInput, "edge size exceeds seek range")
        })?;
        file.seek(SeekFrom::End(-offset))?;
        tail.resize(tail_len, 0);
        file.read_exact(&mut tail)?;
    }
    let change_token = open_file_change_token(path, &file, &opened)?;
    verify_regular_snapshot(path, &file, &before)?;
    Ok(RegularFileEdgeSnapshot {
        len,
        modified: opened.modified()?,
        change_token,
        head,
        tail,
    })
}

pub fn regular_file_edge_snapshot(
    path: &Path,
    edge_bytes: usize,
) -> std::io::Result<Option<RegularFileEdgeSnapshot>> {
    let Some(before) = present_metadata(path)? else {
        return Ok(None);
    };
    regular_file_edge_snapshot_from_meta(path, edge_bytes, before).map(Some)
}
