//! Capturing one source file as a new catalog version when its content changed.
use super::{catalog::Archive, copy_database, hash, now, Source, Stats, CHUNK};
use crate::{
    ingest::registry::{path_eq, relative_path},
    safe_fs,
};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    time::Duration,
};

pub(super) fn open_source(path: &Path) -> Result<File> {
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("source is a symbolic link");
    }
    let mut options = OpenOptions::new();
    options.read(true);
    crate::ingest::registry::secure_read_options(&mut options);
    let file = options.open(path)?;
    if !file.metadata()?.is_file() {
        bail!("source is not a regular file")
    }
    Ok(file)
}
fn sqlite_snapshot(path: &Path, dir: &Path, blocking: bool) -> Result<tempfile::NamedTempFile> {
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("source is a symbolic link");
    }
    for suffix in ["-wal", "-shm", "-journal"] {
        if fs::symlink_metadata(format!("{}{suffix}", path.display()))
            .is_ok_and(|m| m.file_type().is_symlink())
        {
            bail!("source sidecar is a symbolic link");
        }
    }
    let source = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    source.busy_timeout(Duration::ZERO)?;
    let temp = tempfile::NamedTempFile::new_in(dir)?;
    let mut destination = Connection::open(temp.path())?;
    {
        let backup = rusqlite::backup::Backup::new(&source, &mut destination)?;
        copy_database(&backup, blocking)?;
    }
    destination.close().map_err(|(_, e)| e)?;
    Ok(temp)
}
/// The file's identity, and for a database its WAL's: unchanged means nothing to read.
fn fingerprint(file: &File, path: &Path, sqlite: bool) -> Result<String> {
    let mut fingerprint = format!("{:?}", safe_fs::identity(&file.metadata()?));
    if sqlite {
        let wal = PathBuf::from(format!("{}-wal", path.display()));
        if fs::symlink_metadata(&wal).is_ok_and(|m| m.file_type().is_symlink()) {
            bail!("source sidecar is a symbolic link");
        }
        fingerprint.push_str(&format!(
            "{:?}",
            fs::metadata(wal).ok().as_ref().map(safe_fs::identity)
        ));
    }
    Ok(fingerprint)
}
/// The latest captured version of a path.
struct Previous {
    id: i64,
    size: u64,
    sha: String,
}
/// A source's content as stored chunks.
struct Captured {
    chunks: Vec<String>,
    sha: String,
    size: u64,
    /// Whether the content starts with the previous version's: an append.
    prefix_matches: bool,
}
pub(super) fn capture_one(
    archive: &mut Archive,
    source: &Source,
    stats: &mut Stats,
    scheduled: bool,
    log: &mut dyn FnMut(&str),
) -> Result<()> {
    // A root that links to a file is versioned under its own path and read at its target.
    let read = crate::ingest::registry::source_path(&source.path);
    let mut file = open_source(&read)?;
    let before = safe_fs::identity(&file.metadata()?);
    let mut header = [0u8; 16];
    let n = file.read(&mut header)?;
    let sqlite = n == 16 && &header == b"SQLite format 3\0";
    let fingerprint = fingerprint(&file, &read, sqlite)?;
    let previous_fingerprint: Option<(i64, String)> = archive
        .db
        .query_row(
            "SELECT id,fingerprint FROM versions WHERE path=? ORDER BY id DESC LIMIT 1",
            [source.path.to_string_lossy().as_ref()],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    if let Some((id, _)) = previous_fingerprint.filter(|(_, f)| *f == fingerprint) {
        // An unchanged file whose stored copy is damaged is read again to re-store it.
        if !archive.version_damaged(id)? {
            stats.unchanged += 1;
            return Ok(());
        }
    }
    file.seek(SeekFrom::Start(0))?;
    let snapshot = if sqlite {
        Some(sqlite_snapshot(&read, &archive.dir, !scheduled)?)
    } else {
        None
    };
    if let Some(ref snapshot) = snapshot {
        file = File::open(snapshot.path())?;
    }
    let previous = archive
        .db
        .query_row(
            "SELECT id,size,sha FROM versions WHERE path=? ORDER BY id DESC LIMIT 1",
            [source.path.to_string_lossy().as_ref()],
            |r| {
                Ok(Previous {
                    id: r.get(0)?,
                    size: r.get(1)?,
                    sha: r.get(2)?,
                })
            },
        )
        .optional()?;
    let repaired = archive.repaired;
    let captured = read_chunks(archive, &mut file, previous.as_ref(), sqlite)?;
    if !sqlite
        && (safe_fs::identity(&file.metadata()?) != before
            || safe_fs::identity(&fs::metadata(&read)?) != before)
    {
        bail!("source changed during capture")
    }
    if archive.repaired > repaired {
        log(&format!(
            "re-stored {} damaged chunk(s) of {}",
            archive.repaired - repaired,
            file_name(source)
        ));
    }
    if captured.size == 0 {
        stats.unchanged += 1;
        return Ok(());
    }
    if let Some(previous) = previous.as_ref().filter(|p| p.sha == captured.sha) {
        archive.db.execute(
            "UPDATE versions SET fingerprint=? WHERE id=?",
            params![fingerprint, previous.id],
        )?;
        stats.unchanged += 1;
        return Ok(());
    }
    record_version(archive, source, &captured, sqlite, &fingerprint)?;
    log(&count(stats, source, &captured, previous.as_ref()));
    Ok(())
}
fn file_name(source: &Source) -> std::borrow::Cow<'_, str> {
    source
        .path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
}
/// Stores the file's content chunk by chunk, checking whether it extends `previous`.
fn read_chunks(
    archive: &mut Archive,
    file: &mut File,
    previous: Option<&Previous>,
    sqlite: bool,
) -> Result<Captured> {
    let old_chunks = if let Some(previous) = previous {
        archive.chunks(previous.id)?
    } else {
        Vec::new()
    };
    let mut chunks = Vec::new();
    let mut digest = Sha256::new();
    let mut size = 0;
    let mut prefix_matches = !sqlite && previous.is_some();
    loop {
        let chunk = next_chunk(file)?;
        if chunk.is_empty() {
            break;
        }
        if prefix_matches {
            if let Some(sha) = old_chunks.get(chunks.len()) {
                let identical = hash(&chunk) == *sha;
                let extends_partial = previous.is_some_and(|p| {
                    p.size % CHUNK as u64 != 0 && chunks.len() + 1 == old_chunks.len()
                }) && !archive.damaged.contains(sha);
                if !identical
                    && !(extends_partial && {
                        let old = archive.load(sha)?;
                        chunk.starts_with(&old)
                    })
                {
                    prefix_matches = false;
                }
            }
        }
        digest.update(&chunk);
        size += chunk.len() as u64;
        let (sha, _) = archive.store(&chunk)?;
        chunks.push(sha);
    }
    Ok(Captured {
        chunks,
        sha: crate::store::hex_digest(digest.finalize()),
        size,
        prefix_matches,
    })
}
/// Up to one chunk of the file; empty at its end.
fn next_chunk(file: &mut File) -> Result<Vec<u8>> {
    let mut chunk = vec![0; CHUNK];
    let mut n = 0;
    while n < CHUNK {
        let count = file.read(&mut chunk[n..])?;
        if count == 0 {
            break;
        }
        n += count;
    }
    chunk.truncate(n);
    Ok(chunk)
}
fn record_version(
    archive: &mut Archive,
    source: &Source,
    captured: &Captured,
    sqlite: bool,
    fingerprint: &str,
) -> Result<()> {
    let home = crate::ingest::home();
    let home_relative =
        relative_path(&source.path, &home).map(|p| p.to_string_lossy().into_owned());
    let tail = if path_eq(&source.path, &source.root) {
        source
            .path
            .file_name()
            .context("source has no filename")?
            .to_string_lossy()
            .into_owned()
    } else {
        relative_path(&source.path, &source.root)
            .context("source is outside archive root")?
            .to_string_lossy()
            .into_owned()
    };
    let tx = archive.db.transaction()?;
    tx.execute("INSERT INTO versions(agent,path,home_relative,tail,size,sha,sqlite,created,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",params![source.agent,source.path.to_string_lossy(),home_relative,tail,captured.size,captured.sha,sqlite,now(),fingerprint])?;
    let id = tx.last_insert_rowid();
    for (i, sha) in captured.chunks.iter().enumerate() {
        tx.execute("INSERT INTO chunks VALUES(?,?,?)", params![id, i, sha])?;
    }
    tx.commit()?;
    Ok(())
}
/// Counts a recorded version and returns its log line.
fn count(
    stats: &mut Stats,
    source: &Source,
    captured: &Captured,
    previous: Option<&Previous>,
) -> String {
    let size = captured.size;
    // 0.4.9 counted an append's new suffix and a full capture's whole file as new content.
    let grown = previous
        .filter(|p| captured.prefix_matches && size > p.size)
        .map(|p| p.size);
    if let Some(old) = grown {
        stats.appended += 1;
        stats.bytes_stored += size - old;
    } else {
        stats.full += 1;
        stats.bytes_stored += size;
    }
    format!(
        "{} {} ({size} bytes)",
        if grown.is_some() {
            "appended"
        } else {
            "captured"
        },
        file_name(source)
    )
}

#[cfg(test)]
mod source_open_tests {
    use super::*;
    #[cfg(not(unix))]
    use std::io::Write;
    #[cfg(unix)]
    fn assert_source_flags(file: &File) {
        use std::os::fd::AsRawFd;
        let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
        assert!(flags >= 0);
        assert_ne!(
            flags & libc::O_NONBLOCK,
            0,
            "a source swapped for a FIFO must not block open"
        );
        assert_eq!(flags & libc::O_ACCMODE, libc::O_RDONLY);
        let temp = tempfile::tempdir().unwrap();
        let fifo = temp.path().join("source.fifo");
        use std::os::unix::ffi::OsStrExt;
        let name = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(open_source(&fifo)
            .unwrap_err()
            .to_string()
            .contains("not a regular file"));
    }
    #[cfg(not(unix))]
    fn assert_source_flags(file: &File) {
        assert!(file.metadata().unwrap().is_file());
        let mut read_only = file;
        assert!(read_only.write_all(b"must remain read only").is_err());
    }
    #[test]
    fn source_open_is_nonblocking_and_read_only() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("source");
        fs::write(&path, b"fixture").unwrap();
        let mut file = open_source(&path).unwrap();
        assert_source_flags(&file);
        let mut text = String::new();
        file.read_to_string(&mut text).unwrap();
        assert_eq!(text, "fixture");
    }
}
