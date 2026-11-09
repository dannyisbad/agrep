//! The generation token that fences a SQLite store's reads: it moves whenever the database
//! or its committed WAL does.

use std::path::Path;

use super::sqlite_sidecar;

/// Metadata generation for a SQLite database and its committed WAL. The shared-memory file is
/// lock coordination rather than durable content and must not invalidate warm generations. A
/// store root that links to a database is measured at its target, where its WAL lives.
pub(crate) fn sqlite_generation_token(path: &std::path::Path) -> std::io::Result<String> {
    use std::fmt::Write;

    let path = crate::ingest::registry::source_path(path);
    let mut bytes = Vec::with_capacity(64);
    for (index, candidate) in [path.to_path_buf(), sqlite_sidecar(&path, "-wal")]
        .into_iter()
        .enumerate()
    {
        match std::fs::symlink_metadata(&candidate) {
            // Opening a WAL-mode database read-only materializes an empty -wal beside it. An
            // empty WAL holds no committed frames, so it is the same generation as no WAL at
            // all; counting it as drift makes every checkpointed store permanently unreadable.
            Ok(metadata)
                if index == 1
                    && metadata.is_file()
                    && metadata.len() == 0
                    && !crate::ingest::registry::metadata_is_link(&metadata) =>
            {
                bytes.push(0xff);
            }
            Ok(metadata) => append_file_key(&mut bytes, index, &candidate, &metadata)?,
            Err(error) if index == 1 && error.kind() == std::io::ErrorKind::NotFound => {
                bytes.push(0xff);
            }
            Err(error) => return Err(error),
        }
    }
    let mut token = String::with_capacity(2 + bytes.len() * 2);
    token.push_str("x:");
    for byte in bytes {
        write!(&mut token, "{byte:02x}").expect("writing to a String cannot fail");
    }
    Ok(token)
}

/// The key bytes of one present generation file (`index` 0 the database, 1 its WAL): its
/// extent, its modification time and its change token. A link or special file fails the read.
fn append_file_key(
    bytes: &mut Vec<u8>,
    index: usize,
    candidate: &Path,
    metadata: &std::fs::Metadata,
) -> std::io::Result<()> {
    use std::time::UNIX_EPOCH;

    let modified = metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    bytes.push(index as u8);
    bytes.extend_from_slice(&metadata.len().to_le_bytes());
    bytes.extend_from_slice(&modified.to_le_bytes());
    if crate::ingest::registry::metadata_is_link(metadata) || !metadata.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("{} is not a plain regular file", candidate.display()),
        ));
    }
    #[cfg(target_os = "linux")]
    sqlite_stable_change_token(candidate, metadata)?.append_key(bytes);
    #[cfg(not(target_os = "linux"))]
    crate::ingest::registry::metadata_change_token(candidate, metadata)?.append_key(bytes);
    Ok(())
}

#[cfg(target_os = "linux")]
fn sqlite_stable_change_token(
    path: &Path,
    metadata: &std::fs::Metadata,
) -> std::io::Result<crate::ingest::registry::ChangeToken> {
    use std::os::unix::fs::MetadataExt;

    let snapshot = crate::ingest::registry::regular_file_edge_snapshot(path, 4096)?
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "SQLite file vanished"))?;
    if snapshot.len != metadata.len() || snapshot.modified != metadata.modified()? {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "SQLite file changed while reading its stable identity",
        ));
    }
    let mut hash = 0xcbf29ce484222325_u64;
    let mut extend = |bytes: &[u8]| {
        for byte in bytes {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x100000001b3);
        }
    };
    extend(&metadata.dev().to_le_bytes());
    extend(&metadata.ino().to_le_bytes());
    extend(&metadata.len().to_le_bytes());
    extend(&metadata.mtime().to_le_bytes());
    extend(&metadata.mtime_nsec().to_le_bytes());
    extend(&snapshot.head);
    extend(&snapshot.tail);
    Ok(crate::ingest::registry::ChangeToken::Metadata(hash))
}
