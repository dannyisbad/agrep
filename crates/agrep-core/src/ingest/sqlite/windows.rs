//! The no-delete handles a Windows read-only open holds on a SQLite store and its sidecars.

use std::path::Path;

use super::{same_plain_snapshot, sqlite_sidecar, JOURNAL_PRESENT};

pub(super) struct WindowsSqliteGuard {
    _files: Vec<std::fs::File>,
}

impl WindowsSqliteGuard {
    pub(super) fn create(path: &Path) -> std::io::Result<Self> {
        let mut files = Vec::new();
        for (index, candidate) in [
            path.to_path_buf(),
            sqlite_sidecar(path, "-wal"),
            sqlite_sidecar(path, "-shm"),
        ]
        .into_iter()
        .enumerate()
        {
            if let Some(file) = guard_file(&candidate, index == 0)? {
                files.push(file);
            }
        }
        refuse_unprobed_journal(path)?;
        Ok(Self { _files: files })
    }
}

/// Open `candidate` as the plain regular file it was stamped as, without following a reparse
/// point; None when an optional sidecar is absent.
fn guard_file(candidate: &Path, required: bool) -> std::io::Result<Option<std::fs::File>> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    let before = match std::fs::symlink_metadata(candidate) {
        Ok(metadata) => metadata,
        Err(error) if !required && error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    if crate::ingest::registry::metadata_is_link(&before) || !before.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("{} is not a plain regular file", candidate.display()),
        ));
    }
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(candidate)?;
    let opened = file.metadata()?;
    let after = std::fs::symlink_metadata(candidate)?;
    if crate::ingest::registry::metadata_is_link(&opened)
        || crate::ingest::registry::metadata_is_link(&after)
        || !opened.is_file()
        || !same_plain_snapshot(&before, &opened)
        || !same_plain_snapshot(&opened, &after)
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("source changed while guarding {}", candidate.display()),
        ));
    }
    Ok(Some(file))
}

/// SQLite never rolls back an empty journal, so one is read past. This platform cannot probe
/// for a journal's writer, so any other journal is named instead of read.
fn refuse_unprobed_journal(path: &Path) -> std::io::Result<()> {
    match std::fs::symlink_metadata(sqlite_sidecar(path, "-journal")) {
        Ok(journal)
            if journal.len() == 0
                && journal.is_file()
                && !crate::ingest::registry::metadata_is_link(&journal) =>
        {
            Ok(())
        }
        Ok(_) => Err(std::io::Error::new(
            std::io::ErrorKind::WouldBlock,
            format!(
                "{JOURNAL_PRESENT} {}; if the agent that wrote it is not running, \
                 opening it once in that agent recovers it",
                path.display()
            ),
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}
