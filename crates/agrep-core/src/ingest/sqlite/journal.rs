//! The rollback journal beside a POSIX SQLite store: whether a snapshot may read past it.

use std::path::Path;

use super::sqlite_sidecar;

/// A rollback journal SQLite would roll back on its next write open: no writer holds the
/// database, and only the agent that wrote it can recover it.
#[derive(Debug)]
pub(super) struct HotJournal(String);

impl std::fmt::Display for HotJournal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for HotJournal {}

/// Size and change time of `source`'s rollback journal, None when there is none.
pub(super) fn rollback_journal_key(source: &Path) -> std::io::Result<Option<String>> {
    use std::os::unix::fs::MetadataExt;
    match std::fs::symlink_metadata(sqlite_sidecar(source, "-journal")) {
        Ok(metadata) => Ok(Some(format!(
            "{}:{}:{}.{}",
            metadata.ino(),
            metadata.len(),
            metadata.ctime(),
            metadata.ctime_nsec()
        ))),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

/// Whether another process holds SQLite's PENDING or RESERVED lock on `source`: a writer is live.
fn sqlite_write_lock_held(source: &Path) -> std::io::Result<bool> {
    use std::os::fd::AsRawFd;
    const PENDING_BYTE: libc::off_t = 0x4000_0000;
    let before = std::fs::symlink_metadata(source)?;
    let file = crate::ingest::registry::open_regular_snapshot(source, &before)?;
    // SAFETY: a zeroed flock is a valid query; F_GETLK only writes into it.
    let mut probe: libc::flock = unsafe { std::mem::zeroed() };
    // Linux declares the lock types c_int, the BSDs c_short; the field is c_short on both.
    probe.l_type = libc::F_WRLCK as libc::c_short;
    probe.l_whence = libc::SEEK_SET as libc::c_short;
    probe.l_start = PENDING_BYTE;
    probe.l_len = 2;
    // SAFETY: the descriptor is open for the call and `probe` outlives it.
    if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETLK, &mut probe) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(probe.l_type != libc::F_UNLCK as libc::c_short)
}

/// The rollback journal beside `source`: none, or one no writer holds that SQLite would not roll
/// back (empty, or with its header zeroed), is read past. A live writer's is retryable; a hot
/// one no reader may roll back needs recovery by the agent that wrote it.
pub(super) fn rollback_journal_state(source: &Path) -> std::io::Result<Option<String>> {
    let Some(key) = rollback_journal_key(source)? else {
        return Ok(None);
    };
    let journal = sqlite_sidecar(source, "-journal");
    if sqlite_write_lock_held(source)? {
        return Err(std::io::Error::new(
            std::io::ErrorKind::WouldBlock,
            format!("a writer holds {} mid-transaction", source.display()),
        ));
    }
    // Opened as the census opens sources: never through a link, never blocking on a FIFO, and
    // only as the regular file it was stamped as. Anything else fails this store's read.
    let header = crate::ingest::registry::regular_file_edge_snapshot(&journal, 1)?
        .map(|snapshot| snapshot.head)
        .unwrap_or_default();
    if header.first().is_none_or(|byte| *byte == 0) {
        return Ok(Some(key));
    }
    Err(std::io::Error::other(HotJournal(format!(
        "rollback journal {} needs recovery by the agent that wrote it",
        journal.display()
    ))))
}
