//! Read-only SQLite opens of live agent stores, and the classification of their failures.
//!
//! POSIX opens a private copy-on-write snapshot of the database and its WAL (`snapshot`);
//! Windows holds no-delete handles on the sources instead (`windows`). Both fence the open
//! with the store's generation token (`generation`).

use std::path::{Path, PathBuf};

mod generation;
#[cfg(not(windows))]
mod journal;
#[cfg(not(windows))]
mod snapshot;
#[cfg(windows)]
mod windows;

pub(crate) use generation::sqlite_generation_token;

/// SQLite's CANTOPEN, extended with codes SQLite never uses: for a source that changed while its
/// snapshot was taken, one with a file the user may not read, and one whose journal needs recovery.
const SQLITE_CANTOPEN_MOVED: std::os::raw::c_int = rusqlite::ffi::SQLITE_CANTOPEN | (0x40 << 8);
const SQLITE_CANTOPEN_DENIED: std::os::raw::c_int = rusqlite::ffi::SQLITE_CANTOPEN | (0x41 << 8);
const SQLITE_CANTOPEN_RECOVERY: std::os::raw::c_int = rusqlite::ffi::SQLITE_CANTOPEN | (0x42 << 8);

/// How a failed read names a rollback journal whose writer this platform cannot probe: the
/// refusal states it instead of promising a retry that may never differ.
pub const JOURNAL_PRESENT: &str = "a rollback journal is present beside";

/// Encode a native UTF-8 path as the path component of a SQLite file URI.
fn sqlite_uri_path(path: &std::path::Path) -> Result<String, rusqlite::Error> {
    let Some(raw) = path.to_str() else {
        return Err(rusqlite::Error::InvalidPath(path.to_path_buf()));
    };
    let raw = raw.replace('\\', "/");
    let mut encoded = String::with_capacity(raw.len());
    for byte in raw.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~' | b'/' | b':') {
            encoded.push(char::from(byte));
        } else {
            use std::fmt::Write;
            write!(&mut encoded, "%{byte:02X}").expect("writing to a String cannot fail");
        }
    }
    Ok(encoded)
}

fn sqlite_open_io_error(path: &Path, error: std::io::Error) -> rusqlite::Error {
    let code = match error.kind() {
        std::io::ErrorKind::InvalidData => SQLITE_CANTOPEN_MOVED,
        std::io::ErrorKind::PermissionDenied => SQLITE_CANTOPEN_DENIED,
        _ if hot_journal(&error) => SQLITE_CANTOPEN_RECOVERY,
        _ => rusqlite::ffi::SQLITE_CANTOPEN,
    };
    rusqlite::Error::SqliteFailure(
        rusqlite::ffi::Error::new(code),
        Some(format!("cannot safely open {}: {error}", path.display())),
    )
}

#[cfg(not(windows))]
fn hot_journal(error: &std::io::Error) -> bool {
    error
        .get_ref()
        .is_some_and(|inner| inner.is::<journal::HotJournal>())
}

#[cfg(windows)]
fn hot_journal(_error: &std::io::Error) -> bool {
    false
}

#[cfg(unix)]
fn same_plain_file(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(not(unix))]
fn same_plain_file(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    left.len() == right.len() && left.modified().ok() == right.modified().ok()
}

fn same_plain_snapshot(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    let same = same_plain_file(left, right)
        && left.len() == right.len()
        && left.modified().ok() == right.modified().ok();
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        same && left.ctime() == right.ctime() && left.ctime_nsec() == right.ctime_nsec()
    }
    #[cfg(not(unix))]
    {
        same
    }
}

fn sqlite_sidecar(path: &std::path::Path, suffix: &str) -> PathBuf {
    let mut sidecar = path.as_os_str().to_os_string();
    sidecar.push(suffix);
    PathBuf::from(sidecar)
}

pub(crate) struct ReadOnlyConnection {
    connection: rusqlite::Connection,
    #[cfg(not(windows))]
    _snapshot: snapshot::SqliteSnapshot,
    #[cfg(windows)]
    _guard: windows::WindowsSqliteGuard,
    generation: String,
}

impl ReadOnlyConnection {
    pub(crate) fn source_generation(&self) -> &str {
        &self.generation
    }
}

impl std::ops::Deref for ReadOnlyConnection {
    type Target = rusqlite::Connection;

    fn deref(&self) -> &Self::Target {
        &self.connection
    }
}

/// SQLite resolves only a private COW snapshot on POSIX; Windows holds no-delete source handles.
/// A path swap therefore cannot turn SQLite's own open into a special-file wait. A store root
/// that links to a database is opened at its target (see `registry::source_path`).
pub(crate) fn open_sqlite_ro(path: &Path) -> Result<ReadOnlyConnection, rusqlite::Error> {
    let source = crate::ingest::registry::source_path(path);
    #[cfg(not(windows))]
    let snapshot = snapshot::SqliteSnapshot::create(&source)
        .map_err(|error| sqlite_open_io_error(path, error))?;
    #[cfg(not(windows))]
    let generation = snapshot.generation.clone();
    #[cfg(not(windows))]
    let database = snapshot.database.clone();
    #[cfg(windows)]
    let guard = windows::WindowsSqliteGuard::create(&source)
        .map_err(|error| sqlite_open_io_error(path, error))?;
    #[cfg(windows)]
    let generation =
        sqlite_generation_token(&source).map_err(|error| sqlite_open_io_error(path, error))?;
    #[cfg(windows)]
    let database = source.to_path_buf();
    #[cfg(not(windows))]
    snapshot
        .verify()
        .map_err(|error| sqlite_open_io_error(path, error))?;
    let connection = open_read_only_uri(&database)?;
    #[cfg(not(windows))]
    snapshot
        .verify()
        .map_err(|error| sqlite_open_io_error(path, error))?;
    let after =
        sqlite_generation_token(&source).map_err(|error| sqlite_open_io_error(path, error))?;
    if generation != after {
        return Err(sqlite_open_io_error(
            path,
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "SQLite generation changed while opening",
            ),
        ));
    }
    Ok(ReadOnlyConnection {
        connection,
        #[cfg(not(windows))]
        _snapshot: snapshot,
        #[cfg(windows)]
        _guard: guard,
        generation,
    })
}

/// Open `database` read-only through a file URI and read its schema version, so a file SQLite
/// rejects fails here rather than at the first query.
fn open_read_only_uri(database: &Path) -> Result<rusqlite::Connection, rusqlite::Error> {
    let uri = format!("file:{}?mode=ro", sqlite_uri_path(database)?);
    let connection = rusqlite::Connection::open_with_flags(
        &uri,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
    )?;
    connection.query_row("PRAGMA schema_version", [], |_| Ok(()))?;
    Ok(connection)
}

/// The primary result code, including the input errors SQLite reports with a token offset.
pub(crate) fn sqlite_code(error: &rusqlite::Error) -> Option<rusqlite::ErrorCode> {
    match error {
        rusqlite::Error::SqliteFailure(failure, _) => Some(failure.code),
        rusqlite::Error::SqlInputError { error, .. } => Some(error.code),
        _ => None,
    }
}

/// The source issue kind of a read-only open failure no retry heals: a file SQLite rejects as
/// no database, or one the user may not read. None for failures a retry may clear.
pub(crate) fn sqlite_open_defect(path: &Path, error: &rusqlite::Error) -> Option<&'static str> {
    if let rusqlite::Error::SqliteFailure(failure, _) = error {
        match failure.extended_code {
            SQLITE_CANTOPEN_DENIED => return Some("permission-denied"),
            SQLITE_CANTOPEN_RECOVERY => return Some("needs-recovery"),
            _ => {}
        }
    }
    if sqlite_code(error) == Some(rusqlite::ErrorCode::NotADatabase) {
        return Some("unsupported-file-type");
    }
    if sqlite_corrupt(error) {
        return Some("corrupt-database");
    }
    // The open flattens I/O errors into CANTOPEN; a zero-byte secure probe recovers the
    // permission case without letting a swapped-in special file block the census.
    match crate::ingest::registry::regular_file_edge_snapshot(path, 0) {
        Err(probe) if probe.kind() == std::io::ErrorKind::PermissionDenied => {
            Some("permission-denied")
        }
        _ => None,
    }
}

/// Whether SQLite found the pages it read malformed: the same bytes fail the same way again.
pub(crate) fn sqlite_corrupt(error: &rusqlite::Error) -> bool {
    sqlite_code(error) == Some(rusqlite::ErrorCode::DatabaseCorrupt)
}

/// Whether a read-only open failed because the database changed under its snapshot: a later
/// pass opens another generation, so the failure never repeats as the same one.
pub(crate) fn sqlite_open_moved(error: &rusqlite::Error) -> bool {
    matches!(error, rusqlite::Error::SqliteFailure(failure, _)
        if failure.extended_code == SQLITE_CANTOPEN_MOVED)
}
