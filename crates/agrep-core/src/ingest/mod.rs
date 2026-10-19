//! Per-agent transcript adapters. Each yields normalized `Message`s (the user's words only).

pub mod antigravity;
pub mod claude;
pub mod cline;
pub mod codex;
pub mod crush;
pub mod cursor;
pub mod gemini;
pub mod kimi;
pub mod opencode;
pub(crate) mod parse_guard;
pub mod parse_timestamp;
pub mod pi;
pub mod registry;

use std::path::{Path, PathBuf};

/// Home directory: the discovery root every adapter resolves its store under. `AGREP_HOME`
/// is the shared hookless/ingest override; the golden harness uses it without touching the
/// real `USERPROFILE`/`HOME`. Otherwise `USERPROFILE` precedes `HOME`; reads stay fresh so
/// subprocess overrides are honored.
pub fn home() -> PathBuf {
    std::env::var_os("AGREP_HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Lowercased last segment of the home dir (the username). Paths under home start with
/// container segments (`Users/<name>/Desktop/...`); the username must never read as a
/// project name, whoever runs the ingest.
pub fn home_leaf() -> &'static str {
    use std::sync::OnceLock;
    static LEAF: OnceLock<String> = OnceLock::new();
    LEAF.get_or_init(|| {
        home()
            .file_name()
            .map(|s| s.to_string_lossy().to_ascii_lowercase())
            .unwrap_or_default()
    })
}

/// Bucket a working-dir path into a project name. Uses the basename, but disambiguates
/// generic leaf names (src/web/<you>/Desktop/...) by prepending the parent segment, so
/// `sampleapp/web` and `shopfront/.../src` don't all collapse into "web"/"src".
pub fn project_name(cwd: &str) -> String {
    // Cwds recovered from logged tool args can carry shell quoting (`...\sample-project"`).
    let parts: Vec<&str> = cwd
        .split(['/', '\\'])
        .map(|s| s.trim_matches(|c| c == '"' || c == '\'' || c == ' '))
        .filter(|s| !s.is_empty())
        .collect();
    match parts.last() {
        None => "unknown".to_string(),
        Some(&leaf) => {
            let lower = leaf.to_ascii_lowercase();
            // Any machine's home dir buckets as "~": home_leaf() knows only this
            // machine's username, so a foreign home cwd would leak a username as a
            // project. Shape check: leaf whose parent is Users/home at the path root.
            if parts.len() >= 2 && parts.len() - 2 <= 1 {
                let parent = parts[parts.len() - 2].to_ascii_lowercase();
                if parent == "users" || parent == "home" {
                    return "~".to_string();
                }
            }
            let generic = lower == home_leaf()
                || matches!(
                    lower.as_str(),
                    "src"
                        | "web"
                        | "mobile"
                        | "app"
                        | "lib"
                        | "client"
                        | "server"
                        | "desktop"
                        | "documents"
                        | "downloads"
                );
            if generic && parts.len() >= 2 {
                format!("{}/{}", parts[parts.len() - 2], leaf)
            } else {
                leaf.to_string()
            }
        }
    }
}

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

/// SQLite's CANTOPEN, extended with codes SQLite never uses: for a source that changed while its
/// snapshot was taken, one with a file the user may not read, and one whose journal needs recovery.
const SQLITE_CANTOPEN_MOVED: std::os::raw::c_int = rusqlite::ffi::SQLITE_CANTOPEN | (0x40 << 8);
const SQLITE_CANTOPEN_DENIED: std::os::raw::c_int = rusqlite::ffi::SQLITE_CANTOPEN | (0x41 << 8);
const SQLITE_CANTOPEN_RECOVERY: std::os::raw::c_int =
    rusqlite::ffi::SQLITE_CANTOPEN | (0x42 << 8);

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
    error.get_ref().is_some_and(|inner| inner.is::<HotJournal>())
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

#[cfg(target_os = "linux")]
pub(crate) fn sqlite_stable_change_token(
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

#[cfg(not(windows))]
fn open_snapshot_source(path: &Path) -> std::io::Result<(std::fs::File, std::fs::Metadata)> {
    let before = std::fs::symlink_metadata(path)?;
    if crate::ingest::registry::metadata_is_link(&before) || !before.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("{} is not a plain regular file", path.display()),
        ));
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    crate::ingest::registry::nofollow_read_options(&mut options);
    let file = options.open(path)?;
    let opened = file.metadata()?;
    let after = std::fs::symlink_metadata(path)?;
    if !opened.is_file()
        || !same_plain_snapshot(&before, &opened)
        || !same_plain_snapshot(&opened, &after)
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("source changed while opening {}", path.display()),
        ));
    }
    Ok((file, opened))
}

#[cfg(target_os = "macos")]
fn clone_cow(source: &std::fs::File, destination: &Path) -> std::io::Result<bool> {
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;

    unsafe extern "C" {
        fn fclonefileat(
            source_fd: i32,
            destination_dir: i32,
            destination: *const i8,
            flags: u32,
        ) -> i32;
    }
    let destination_bytes =
        std::ffi::CString::new(destination.as_os_str().as_bytes()).map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "SQLite snapshot path contains a NUL byte",
            )
        })?;
    let result = unsafe { fclonefileat(source.as_raw_fd(), -2, destination_bytes.as_ptr(), 0) };
    if result == 0 {
        return Ok(true);
    }
    let _ = std::fs::remove_file(destination);
    Ok(false)
}

#[cfg(target_os = "linux")]
fn clone_cow(source: &std::fs::File, destination: &Path) -> std::io::Result<bool> {
    use std::os::fd::AsRawFd;

    unsafe extern "C" {
        fn ioctl(fd: i32, request: usize, ...) -> i32;
    }
    let destination_file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)?;
    let result = unsafe {
        ioctl(
            destination_file.as_raw_fd(),
            0x4004_9409,
            source.as_raw_fd(),
        )
    };
    drop(destination_file);
    if result == 0 {
        return Ok(true);
    }
    let _ = std::fs::remove_file(destination);
    Ok(false)
}

#[cfg(all(not(windows), not(any(target_os = "macos", target_os = "linux"))))]
fn clone_cow(_source: &std::fs::File, _destination: &Path) -> std::io::Result<bool> {
    Ok(false)
}

#[cfg(not(windows))]
struct ClonedSqliteFile {
    source: PathBuf,
    opened: std::fs::File,
    metadata: std::fs::Metadata,
    mutable: bool,
}

#[cfg(not(windows))]
impl ClonedSqliteFile {
    fn verify(&self) -> std::io::Result<()> {
        let source = std::fs::symlink_metadata(&self.source)?;
        let opened = self.opened.metadata()?;
        if crate::ingest::registry::metadata_is_link(&source)
            || !source.is_file()
            || !same_plain_file(&opened, &source)
            || (!self.mutable
                && (!same_plain_snapshot(&self.metadata, &opened)
                    || !same_plain_snapshot(&opened, &source)))
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "source changed while snapshotting {}",
                    self.source.display()
                ),
            ));
        }
        Ok(())
    }
}

#[cfg(not(windows))]
#[derive(Clone, Copy, Eq, PartialEq)]
enum SnapshotFileMode {
    Frozen,
    LiveHardlink,
}

#[cfg(not(windows))]
struct SqliteSnapshot {
    directory: PathBuf,
    database: PathBuf,
    cloned: Vec<ClonedSqliteFile>,
    wal_missing: bool,
    shm_missing: bool,
    live_hardlinks: bool,
    source: PathBuf,
    generation: String,
    /// The idle rollback journal the snapshot began beside, which must not change under it.
    journal: Option<String>,
}

/// A rollback journal SQLite would roll back on its next write open: no writer holds the
/// database, and only the agent that wrote it can recover it.
#[cfg(not(windows))]
#[derive(Debug)]
struct HotJournal(String);

#[cfg(not(windows))]
impl std::fmt::Display for HotJournal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

#[cfg(not(windows))]
impl std::error::Error for HotJournal {}

/// Size and change time of `source`'s rollback journal, None when there is none.
#[cfg(not(windows))]
fn rollback_journal_key(source: &Path) -> std::io::Result<Option<String>> {
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
#[cfg(not(windows))]
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
#[cfg(not(windows))]
fn rollback_journal_state(source: &Path) -> std::io::Result<Option<String>> {
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

#[cfg(unix)]
fn owned_private_snapshot(metadata: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    unsafe extern "C" {
        fn geteuid() -> u32;
    }
    metadata.uid() == unsafe { geteuid() } && metadata.mode() & 0o077 == 0
}

#[cfg(not(unix))]
fn owned_private_snapshot(_metadata: &std::fs::Metadata) -> bool {
    true
}

#[cfg(not(windows))]
fn snapshot_directory_owner(name: &std::ffi::OsStr) -> Option<u32> {
    let name = name.to_str()?;
    let suffix = name.strip_prefix(".agrep-sqlite-")?;
    let mut fields = suffix.split('-');
    let pid = fields.next()?.parse().ok()?;
    matches!(fields.next(), Some(nonce) if !nonce.is_empty() && nonce.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .then_some(())?;
    matches!(fields.next(), Some(sequence) if !sequence.is_empty() && sequence.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .then_some(())?;
    fields.next().is_none().then_some(pid)
}

#[cfg(unix)]
fn process_is_alive(pid: u32) -> bool {
    if pid > i32::MAX as u32 {
        return true;
    }
    unsafe extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }
    let result = unsafe { kill(pid as i32, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(1)
}

#[cfg(not(unix))]
fn process_is_alive(_pid: u32) -> bool {
    true
}

#[cfg(not(windows))]
fn plain_snapshot_contents(path: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(path) else {
        return false;
    };
    let mut count = 0;
    for entry in entries {
        let Ok(entry) = entry else {
            return false;
        };
        count += 1;
        if count > 3
            || !matches!(
                entry.file_name().to_str(),
                Some("snapshot.db" | "snapshot.db-wal" | "snapshot.db-shm")
            )
        {
            return false;
        }
        let Ok(metadata) = std::fs::symlink_metadata(entry.path()) else {
            return false;
        };
        if crate::ingest::registry::metadata_is_link(&metadata) || !metadata.is_file() {
            return false;
        }
    }
    true
}

#[cfg(not(windows))]
fn cleanup_stale_sqlite_snapshots(
    root: &Path,
    minimum_age: std::time::Duration,
    limit: usize,
) -> std::io::Result<usize> {
    let mut removed = 0;
    for entry in std::fs::read_dir(root)?.filter_map(Result::ok) {
        let Some(pid) = snapshot_directory_owner(&entry.file_name()) else {
            continue;
        };
        if removed == limit || process_is_alive(pid) {
            continue;
        }
        let metadata = match std::fs::symlink_metadata(entry.path()) {
            Ok(metadata) => metadata,
            Err(_) => continue,
        };
        let old_enough = metadata
            .modified()
            .ok()
            .and_then(|modified| modified.elapsed().ok())
            .is_some_and(|age| age >= minimum_age);
        if crate::ingest::registry::metadata_is_link(&metadata)
            || !metadata.is_dir()
            || !owned_private_snapshot(&metadata)
            || !old_enough
            || !plain_snapshot_contents(&entry.path())
        {
            continue;
        }
        if std::fs::remove_dir_all(entry.path()).is_ok() {
            removed += 1;
        }
    }
    Ok(removed)
}

#[cfg(not(windows))]
impl SqliteSnapshot {
    fn private_directory(source: &Path) -> std::io::Result<PathBuf> {
        use std::sync::atomic::{AtomicU64, Ordering};

        static NEXT_ALIAS: AtomicU64 = AtomicU64::new(0);
        let mut roots = Vec::new();
        if let Some(parent) = source.parent() {
            roots.push(parent.to_path_buf());
        }
        let temp = std::env::temp_dir();
        if !roots.iter().any(|root| root == &temp) {
            roots.push(temp);
        }
        let mut last_error = None;
        for root in roots {
            let _ = cleanup_stale_sqlite_snapshots(
                &root,
                std::time::Duration::from_secs(6 * 60 * 60),
                8,
            );
            for attempt in 0..32_u64 {
                let sequence = NEXT_ALIAS.fetch_add(1, Ordering::Relaxed);
                let nonce = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_nanos();
                let candidate = root.join(format!(
                    ".agrep-sqlite-{}-{nonce:x}-{:x}",
                    std::process::id(),
                    sequence ^ attempt
                ));
                #[cfg(unix)]
                let created = {
                    use std::os::unix::fs::DirBuilderExt;
                    let mut builder = std::fs::DirBuilder::new();
                    builder.mode(0o700).create(&candidate)
                };
                #[cfg(not(unix))]
                let created = std::fs::create_dir(&candidate);
                match created {
                    Ok(()) => {
                        return Ok(candidate);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                    Err(error) => {
                        last_error = Some(error);
                        break;
                    }
                }
            }
        }
        Err(last_error.unwrap_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                "could not allocate a private SQLite snapshot directory",
            )
        }))
    }

    fn create(source: &Path) -> std::io::Result<Self> {
        Self::create_with(source, clone_cow)
    }

    fn create_with(
        source: &Path,
        cloner: impl Fn(&std::fs::File, &Path) -> std::io::Result<bool> + Copy,
    ) -> std::io::Result<Self> {
        let journal = rollback_journal_state(source)?;
        let generation = sqlite_generation_token(source)?;
        let directory = Self::private_directory(source)?;
        let database = directory.join("snapshot.db");
        let mut snapshot = Self {
            directory,
            database,
            cloned: Vec::new(),
            wal_missing: false,
            shm_missing: false,
            live_hardlinks: false,
            source: source.to_path_buf(),
            generation,
            journal,
        };
        let database_alias = snapshot.database.clone();
        let database_mode = snapshot
            .clone_file_with(source, &database_alias, true, false, cloner)?
            .expect("required database snapshot cannot be absent");
        let wal_source = sqlite_sidecar(source, "-wal");
        let wal_alias = sqlite_sidecar(&snapshot.database, "-wal");
        let wal_mode = snapshot.clone_file_with(&wal_source, &wal_alias, false, false, cloner)?;
        if database_mode == SnapshotFileMode::LiveHardlink
            || wal_mode == Some(SnapshotFileMode::LiveHardlink)
        {
            snapshot.reset_files()?;
            let forced = |_: &std::fs::File, _: &Path| Ok(false);
            let database_mode = snapshot
                .clone_file_with(source, &database_alias, true, false, forced)?
                .expect("required database snapshot cannot be absent");
            if database_mode != SnapshotFileMode::LiveHardlink {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::Unsupported,
                    "SQLite hardlink fallback unexpectedly copied the main database",
                ));
            }
            let wal_mode =
                snapshot.clone_file_with(&wal_source, &wal_alias, false, false, forced)?;
            let shm_source = sqlite_sidecar(source, "-shm");
            let shm_alias = sqlite_sidecar(&snapshot.database, "-shm");
            match wal_mode {
                Some(SnapshotFileMode::LiveHardlink) => {
                    let shm_mode = snapshot
                        .clone_file_with(&shm_source, &shm_alias, true, true, forced)?
                        .expect("required SQLite SHM snapshot cannot be absent");
                    if shm_mode != SnapshotFileMode::LiveHardlink {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::Unsupported,
                            "SQLite hardlink fallback unexpectedly copied shared memory",
                        ));
                    }
                }
                Some(SnapshotFileMode::Frozen) => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::Unsupported,
                        "SQLite hardlink fallback unexpectedly copied the WAL",
                    ));
                }
                None => match std::fs::symlink_metadata(&shm_source) {
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        snapshot.shm_missing = true;
                    }
                    Ok(_) => {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::InvalidData,
                            "SQLite SHM exists without its WAL",
                        ));
                    }
                    Err(error) => return Err(error),
                },
            }
            snapshot.live_hardlinks = true;
        }
        snapshot.verify()?;
        Ok(snapshot)
    }

    fn reset_files(&mut self) -> std::io::Result<()> {
        self.cloned.clear();
        for suffix in ["", "-wal", "-shm"] {
            let path = if suffix.is_empty() {
                self.database.clone()
            } else {
                sqlite_sidecar(&self.database, suffix)
            };
            match std::fs::remove_file(path) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
        self.wal_missing = false;
        self.shm_missing = false;
        Ok(())
    }

    fn clone_file_with(
        &mut self,
        source: &Path,
        destination: &Path,
        required: bool,
        mutable: bool,
        cloner: impl FnOnce(&std::fs::File, &Path) -> std::io::Result<bool>,
    ) -> std::io::Result<Option<SnapshotFileMode>> {
        let (mut opened, metadata) = match open_snapshot_source(source) {
            Ok(opened) => opened,
            Err(error) if !required && error.kind() == std::io::ErrorKind::NotFound => {
                self.wal_missing = true;
                return Ok(None);
            }
            Err(error) => return Err(error),
        };
        #[cfg(target_os = "linux")]
        let mut metadata = metadata;
        #[cfg(target_os = "linux")]
        let mut mode = SnapshotFileMode::Frozen;
        #[cfg(not(target_os = "linux"))]
        let mode = SnapshotFileMode::Frozen;
        if !cloner(&opened, destination)? {
            use std::io::Seek;

            const COPY_FALLBACK_MAX: u64 = 16 * 1024 * 1024;
            #[cfg(target_os = "linux")]
            let hardlinked = match std::fs::hard_link(source, destination) {
                Ok(()) => {
                    metadata = opened.metadata()?;
                    mode = SnapshotFileMode::LiveHardlink;
                    true
                }
                Err(_) => false,
            };
            #[cfg(not(target_os = "linux"))]
            let hardlinked = false;
            if !hardlinked {
                if metadata.len() > COPY_FALLBACK_MAX {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::Unsupported,
                        format!("{} cannot be cloned safely", source.display()),
                    ));
                }
                let mut output = std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(destination)?;
                opened.seek(std::io::SeekFrom::Start(0))?;
                std::io::copy(&mut opened, &mut output)?;
                output.sync_all()?;
            }
        }
        let destination_metadata = std::fs::symlink_metadata(destination)?;
        if crate::ingest::registry::metadata_is_link(&destination_metadata)
            || !destination_metadata.is_file()
            || destination_metadata.len() != metadata.len()
            || (mode == SnapshotFileMode::LiveHardlink
                && !same_plain_file(&metadata, &destination_metadata))
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("invalid snapshot of {}", source.display()),
            ));
        }
        let cloned = ClonedSqliteFile {
            source: source.to_path_buf(),
            opened,
            metadata,
            mutable,
        };
        cloned.verify()?;
        self.cloned.push(cloned);
        Ok(Some(mode))
    }

    fn verify(&self) -> std::io::Result<()> {
        for cloned in &self.cloned {
            cloned.verify()?;
        }
        if self.wal_missing {
            match std::fs::symlink_metadata(sqlite_sidecar(&self.source, "-wal")) {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Ok(_) => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "SQLite WAL appeared while snapshotting",
                    ));
                }
                Err(error) => return Err(error),
            }
        }
        if self.live_hardlinks && self.shm_missing {
            match std::fs::symlink_metadata(sqlite_sidecar(&self.source, "-shm")) {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Ok(_) => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "SQLite SHM appeared without a snapshot WAL",
                    ));
                }
                Err(error) => return Err(error),
            }
        }
        if rollback_journal_key(&self.source)? != self.journal {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "SQLite rollback journal appeared or changed while snapshotting",
            ));
        }
        let after = sqlite_generation_token(&self.source)?;
        if after != self.generation {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "SQLite generation changed while snapshotting",
            ));
        }
        Ok(())
    }
}

#[cfg(not(windows))]
impl Drop for SqliteSnapshot {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}

#[cfg(windows)]
struct WindowsSqliteGuard {
    files: Vec<std::fs::File>,
}

#[cfg(windows)]
impl WindowsSqliteGuard {
    fn create(path: &Path) -> std::io::Result<Self> {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };

        let mut files = Vec::new();
        for (index, candidate) in [
            path.to_path_buf(),
            sqlite_sidecar(path, "-wal"),
            sqlite_sidecar(path, "-shm"),
        ]
        .into_iter()
        .enumerate()
        {
            let before = match std::fs::symlink_metadata(&candidate) {
                Ok(metadata) => metadata,
                Err(error) if index != 0 && error.kind() == std::io::ErrorKind::NotFound => {
                    continue;
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
                .open(&candidate)?;
            let opened = file.metadata()?;
            let after = std::fs::symlink_metadata(&candidate)?;
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
            files.push(file);
        }
        match std::fs::symlink_metadata(sqlite_sidecar(path, "-journal")) {
            Ok(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::WouldBlock,
                    format!(
                        "{JOURNAL_PRESENT} {}; if the agent that wrote it is not running, \
                         opening it once in that agent recovers it",
                        path.display()
                    ),
                ));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        Ok(Self { files })
    }
}

pub(crate) struct ReadOnlyConnection {
    connection: rusqlite::Connection,
    #[cfg(not(windows))]
    _snapshot: SqliteSnapshot,
    #[cfg(windows)]
    _guard: WindowsSqliteGuard,
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

fn open_sqlite_ro_with_hook(
    path: &Path,
    before_open: impl FnOnce(),
) -> Result<ReadOnlyConnection, rusqlite::Error> {
    #[cfg(not(windows))]
    let snapshot =
        SqliteSnapshot::create(path).map_err(|error| sqlite_open_io_error(path, error))?;
    #[cfg(not(windows))]
    let generation = snapshot.generation.clone();
    #[cfg(not(windows))]
    let database = snapshot.database.clone();
    #[cfg(windows)]
    let guard =
        WindowsSqliteGuard::create(path).map_err(|error| sqlite_open_io_error(path, error))?;
    #[cfg(windows)]
    let generation =
        sqlite_generation_token(path).map_err(|error| sqlite_open_io_error(path, error))?;
    #[cfg(windows)]
    let database = path.to_path_buf();
    #[cfg(not(windows))]
    snapshot
        .verify()
        .map_err(|error| sqlite_open_io_error(path, error))?;
    before_open();
    let uri = format!("file:{}?mode=ro", sqlite_uri_path(&database)?);
    let connection = rusqlite::Connection::open_with_flags(
        &uri,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
    )?;
    connection.query_row("PRAGMA schema_version", [], |_| Ok(()))?;
    #[cfg(not(windows))]
    snapshot
        .verify()
        .map_err(|error| sqlite_open_io_error(path, error))?;
    let after = sqlite_generation_token(path).map_err(|error| sqlite_open_io_error(path, error))?;
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

/// SQLite resolves only a private COW snapshot on POSIX; Windows holds no-delete source handles.
/// A path swap therefore cannot turn SQLite's own open into a special-file wait.
pub(crate) fn open_sqlite_ro(path: &Path) -> Result<ReadOnlyConnection, rusqlite::Error> {
    open_sqlite_ro_with_hook(path, || {})
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

/// Whether a failure observed on `path` by a pass that began at `scan_start` may have caught a
/// write in flight: a file (or SQLite sidecar) modified or changed within a timestamp tick of the
/// observation. Two such catches of one torn state share an identity, so neither proves it stays.
pub fn failure_observation_racy(path: &Path, scan_start: std::time::SystemTime) -> bool {
    let (start_ns, now_ns) = match crate::fault_injection::pinned_now_ns() {
        Some(pinned) => (pinned, pinned),
        None => match (
            crate::ingest_cache::epoch_ns(scan_start),
            crate::ingest_cache::epoch_ns(std::time::SystemTime::now()),
        ) {
            (Some(start), Some(now)) => (start, now),
            _ => return true,
        },
    };
    // A SQLite store's ctime moves under agrep's own hard-link snapshot, as its identity knows.
    let store = crate::ingest::registry::sqlite_database(path);
    let files: Vec<PathBuf> = if store {
        ["", "-journal", "-wal"]
            .into_iter()
            .map(|suffix| sqlite_sidecar(path, suffix))
            .collect()
    } else {
        vec![path.to_path_buf()]
    };
    files.iter().any(|file| match std::fs::symlink_metadata(file) {
        Ok(metadata) => {
            let (modified, changed) = metadata_stamps_ns(&metadata);
            racy_failure_stamps(modified, changed.filter(|_| !store), start_ns, now_ns)
        }
        Err(_) => false,
    })
}

/// The stamp rule of [`failure_observation_racy`], granule-aware as the cache's racy entries.
pub(crate) fn racy_failure_stamps(
    modified_ns: Option<i64>,
    changed_ns: Option<i64>,
    start_ns: i64,
    now_ns: i64,
) -> bool {
    [modified_ns, changed_ns]
        .into_iter()
        .flatten()
        .any(|stamp| crate::ingest_cache::racy_stamp(stamp, start_ns, now_ns))
}

#[cfg(unix)]
fn metadata_stamps_ns(metadata: &std::fs::Metadata) -> (Option<i64>, Option<i64>) {
    use std::os::unix::fs::MetadataExt;
    let stamp = |seconds: i64, nanos: i64| seconds.checked_mul(1_000_000_000)?.checked_add(nanos);
    (
        stamp(metadata.mtime(), metadata.mtime_nsec()),
        stamp(metadata.ctime(), metadata.ctime_nsec()),
    )
}

#[cfg(windows)]
fn metadata_stamps_ns(metadata: &std::fs::Metadata) -> (Option<i64>, Option<i64>) {
    (
        metadata
            .modified()
            .ok()
            .and_then(crate::ingest_cache::epoch_ns),
        None,
    )
}

/// What a failure on `path` is failing on, so a later pass that sees the same identity sees the
/// same failure. A SQLite store (database, `-journal`, `-wal`) is named as the census names it,
/// without ctime, which agrep's own hard-link snapshot moves; any other file by its identity and
/// its content, so a torn file whole again in between never reads as the same failure.
pub fn failure_identity(path: &Path) -> String {
    if crate::ingest::registry::sqlite_database(path) {
        return ["", "-journal", "-wal"]
            .into_iter()
            .map(|suffix| file_failure_identity(&sqlite_sidecar(path, suffix), false))
            .collect::<Vec<_>>()
            .join("|");
    }
    file_failure_identity(path, true)
}

/// Files at most this long are digested whole; longer ones by their head and tail.
const FAILURE_DIGEST_MAX_BYTES: u64 = 16 * 1024 * 1024;

fn file_failure_identity(path: &Path, ctime: bool) -> String {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) => return format!("!{:?}", error.kind()),
    };
    let mut digest = 0xcbf29ce484222325_u64;
    let mut extend = |bytes: &[u8]| {
        for byte in bytes {
            digest ^= u64::from(*byte);
            digest = digest.wrapping_mul(0x100000001b3);
        }
    };
    let content = if ctime && metadata.len() <= FAILURE_DIGEST_MAX_BYTES {
        crate::ingest::registry::read_bounded_regular_file_snapshot(path, FAILURE_DIGEST_MAX_BYTES)
            .map(|snapshot| {
                if let Some(snapshot) = snapshot {
                    extend(&snapshot.bytes);
                }
            })
    } else {
        crate::ingest::registry::regular_file_edge_snapshot(path, 4096).map(|snapshot| {
            if let Some(snapshot) = snapshot {
                extend(&snapshot.head);
                extend(&snapshot.tail);
            }
        })
    };
    let content = match content {
        Ok(()) => format!("{digest:x}"),
        Err(error) => format!("!{:?}", error.kind()),
    };
    format!("{}:{content}", metadata_failure_identity(&metadata, ctime))
}

#[cfg(unix)]
fn metadata_failure_identity(metadata: &std::fs::Metadata, ctime: bool) -> String {
    use std::os::unix::fs::MetadataExt;
    let changed = if ctime {
        format!(":{}.{}", metadata.ctime(), metadata.ctime_nsec())
    } else {
        String::new()
    };
    format!(
        "{}:{}:{}:{}.{}{changed}",
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.mtime_nsec()
    )
}

#[cfg(windows)]
fn metadata_failure_identity(metadata: &std::fs::Metadata, _ctime: bool) -> String {
    use std::os::windows::fs::MetadataExt;
    format!(
        "{}:{}:{}:{}",
        metadata.file_size(),
        metadata.creation_time(),
        metadata.last_write_time(),
        metadata.file_attributes()
    )
}

/// How a failed read names a rollback journal whose writer this platform cannot probe: the
/// refusal states it instead of promising a retry that may never differ.
pub const JOURNAL_PRESENT: &str = "a rollback journal is present beside";

/// Why a source's read failed (a store's open, an empty file), keyed by its path, for the
/// read-failure record the cache writes after the parse returns only an outcome.
static READ_FAILURES: std::sync::Mutex<Vec<(PathBuf, String)>> = std::sync::Mutex::new(Vec::new());

pub(crate) fn note_read_failure(path: &Path, reason: String) {
    if let Ok(mut failures) = READ_FAILURES.lock() {
        failures.retain(|(noted, _)| noted != path);
        failures.push((path.to_path_buf(), reason));
    }
}

pub(crate) fn take_read_failure(path: &Path) -> Option<String> {
    let mut failures = READ_FAILURES.lock().ok()?;
    let at = failures.iter().position(|(noted, _)| noted == path)?;
    Some(failures.swap_remove(at).1)
}

/// Whether a read-only open failed because the database changed under its snapshot: a later
/// pass opens another generation, so the failure never repeats as the same one.
pub(crate) fn sqlite_open_moved(error: &rusqlite::Error) -> bool {
    matches!(error, rusqlite::Error::SqliteFailure(failure, _)
        if failure.extended_code == SQLITE_CANTOPEN_MOVED)
}

/// What a per-file read error says about the source: a denial, an absence or a special file is
/// skipped as before; one that changed under the read moved; any other error may clear.
pub(crate) fn read_failure_outcome(error: &std::io::Error) -> crate::ingest_cache::ReadOutcome {
    use crate::ingest_cache::ReadOutcome;
    match error.kind() {
        std::io::ErrorKind::PermissionDenied
        | std::io::ErrorKind::NotFound
        | std::io::ErrorKind::InvalidInput => ReadOutcome::Skipped,
        std::io::ErrorKind::InvalidData => ReadOutcome::Moved,
        _ => ReadOutcome::Unavailable,
    }
}

/// A read whose source generation moved before or while it ran.
#[derive(Debug)]
pub(crate) struct SourceMoved(pub String);

impl std::fmt::Display for SourceMoved {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SourceMoved {}

/// Metadata generation for a SQLite database and its committed WAL. The shared-memory file is
/// lock coordination rather than durable content and must not invalidate warm generations.
pub(crate) fn sqlite_generation_token(path: &std::path::Path) -> std::io::Result<String> {
    use std::fmt::Write;
    use std::time::UNIX_EPOCH;

    let mut bytes = Vec::with_capacity(64);
    for (index, candidate) in [path.to_path_buf(), sqlite_sidecar(path, "-wal")]
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
            Ok(metadata) => {
                let modified = metadata
                    .modified()
                    .ok()
                    .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
                    .map(|duration| duration.as_nanos())
                    .unwrap_or(0);
                bytes.push(index as u8);
                bytes.extend_from_slice(&metadata.len().to_le_bytes());
                bytes.extend_from_slice(&modified.to_le_bytes());
                if crate::ingest::registry::metadata_is_link(&metadata) || !metadata.is_file() {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        format!("{} is not a plain regular file", candidate.display()),
                    ));
                }
                #[cfg(target_os = "linux")]
                sqlite_stable_change_token(&candidate, &metadata)?.append_key(&mut bytes);
                #[cfg(not(target_os = "linux"))]
                crate::ingest::registry::metadata_change_token(&candidate, &metadata)?
                    .append_key(&mut bytes);
            }
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

pub(crate) fn sqlite_sidecar(path: &std::path::Path, suffix: &str) -> PathBuf {
    let mut sidecar = path.as_os_str().to_os_string();
    sidecar.push(suffix);
    PathBuf::from(sidecar)
}

/// Read a store file tolerating invalid UTF-8: corrupt bytes become U+FFFD, so one bad
/// byte costs at most its own record instead of the whole transcript (a strict
/// read_to_string fails the entire file). Io errors still bubble; at a tallied call
/// site the error arm counts the file as one seen record + one error, keeping the
/// intake identity whole for files the parser never got to iterate.
///
/// Transcripts are live: the session being written right now is the one a user most wants
/// searchable, so this serves the extent the file had at open and leaves the tail to the
/// next pass. A trailing partial record simply fails its own line and is re-read then.
pub fn read_lossy(path: &std::path::Path) -> std::io::Result<String> {
    let bytes = crate::ingest::registry::read_growing_regular_file(path, u64::MAX)
        .and_then(|body| {
            body.ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::NotFound, "source does not exist")
            })
        })
        .map_err(|error| {
            std::io::Error::new(error.kind(), format!("{}: {error}", path.display()))
        })?;
    Ok(match String::from_utf8(bytes) {
        // Valid UTF-8 is the overwhelming case: take ownership of the read buffer instead of copying it.
        Ok(text) => text,
        Err(error) => String::from_utf8_lossy(error.as_bytes()).into_owned(),
    })
}

/// Append reply block `t` to the message's reply exactly as 0.4.2 did (blocks joined with one
/// space, `REPLY_CAP` characters kept, `…` when a block was cut), recording where the block
/// joined and, past the cap, the block's tail in `reply_shape`. Adds the source characters
/// this block represents (its length plus one join) to `reply_chars` and returns them. The
/// held length is tracked, so a block past the cap costs its own length, not a rescan.
pub fn append_reply(raw: &mut crate::model::RawMessage, t: &str) -> usize {
    let t = t.trim();
    if t.is_empty() {
        return 0;
    }
    let block_chars = t.chars().count();
    let shape = &mut raw.reply_shape;
    let held = shape.head_chars as usize;
    let source_chars = block_chars + usize::from(held > 0);
    raw.reply_chars += source_chars;
    if held >= REPLY_CAP {
        // the tail block replaces the previous one past the cap; its join entry accumulates
        shape.joins = (shape.breaks.len() + 1) as u32;
        keep_end(shape, t, block_chars);
        return source_chars;
    }
    if held > 0 {
        raw.reply.push(' ');
        shape.breaks.push(held as u32);
        shape.joins = shape.breaks.len() as u32;
    }
    raw.reply.push_str(t);
    let joined = held + usize::from(held > 0) + block_chars;
    if joined <= REPLY_CAP {
        shape.head_chars = joined as u32;
        return source_chars;
    }
    let end = raw
        .reply
        .char_indices()
        .nth(REPLY_CAP)
        .map_or(raw.reply.len(), |(index, _)| index);
    raw.reply.truncate(end);
    raw.reply.push('…');
    shape.head_chars = (REPLY_CAP + 1) as u32;
    keep_end(shape, t, block_chars);
    source_chars
}

fn keep_end(shape: &mut crate::model::ReplyShape, t: &str, block_chars: usize) {
    shape.end_cut = block_chars > REPLY_TAIL_CAP;
    let start = if shape.end_cut {
        t.char_indices()
            .nth(block_chars - REPLY_TAIL_CAP)
            .map_or(0, |(index, _)| index)
    } else {
        0
    };
    shape.end.clear();
    shape.end.push_str(&t[start..]);
}

/// Cap on indexed reply text; original lengths remain available for truncation diagnostics.
pub const REPLY_CAP: usize = 64_000;

/// Characters of a capped reply's latest block kept beside the text, so its ending survives.
pub const REPLY_TAIL_CAP: usize = 8_000;

/// Cap on event summaries. Original lengths remain in the event row to disclose truncation.
pub const EVENT_CAP: usize = 800;

/// The `meta` token the calls recorded after a turn's last reply block carry.
pub const AFTER_REPLY_META: &str = "after_reply=1";

/// Which of a turn's calls no reply text followed. Adapters mark each human prompt, each reply
/// block they append, and the end of the file; the calls recorded after the turn's last reply
/// block (every call of a turn that wrote none) gain `after_reply=1` in `meta`, so a turn cut
/// off mid-work can be told from one that answered after its tools. Compaction recaps are not
/// prompts: the reply they carry continues the turn before them.
pub struct ReplyOrder {
    text_at: usize,
    /// Calls the user ran (pi's `!cmd`), never the agent's work: left out of the fact.
    excluded: Vec<usize>,
}

impl Default for ReplyOrder {
    fn default() -> Self {
        Self::new()
    }
}

impl ReplyOrder {
    pub fn new() -> Self {
        Self {
            text_at: 0,
            excluded: Vec::new(),
        }
    }

    /// A human prompt opens a turn; the previous turn's trailing calls are settled.
    pub fn prompt(&mut self, events: &mut [crate::model::Event]) {
        self.settle(events);
        self.text_at = events.len();
    }

    /// A reply block was appended after `recorded` events: every call so far was answered, and
    /// the block's join (when the block made one) records whether work came between it and
    /// the block before it.
    pub fn reply(&mut self, recorded: usize) {
        self.text_at = recorded;
    }

    /// Whether the agent made a work call (a tool or a delegation) between its previous reply
    /// block and the block `append_reply` just added (the events before index `recorded`), kept
    /// for that join: the shape's last break, the tail block past the cap (whose entry covers
    /// every block the tail replaced), or for a first block the lead join.
    pub fn joined(
        &self,
        shape: &mut crate::model::ReplyShape,
        events: &[crate::model::Event],
        recorded: usize,
    ) {
        let worked = events[..recorded.min(events.len())]
            .iter()
            .enumerate()
            .skip(self.text_at)
            .any(|(index, event)| is_work_event(event) && !self.excluded.contains(&index));
        Self::joined_known(shape, worked);
    }

    /// The same fact from an adapter whose store orders prose and calls itself: `worked` is
    /// whether a work call came between the previous block and the one just appended.
    pub fn joined_known(shape: &mut crate::model::ReplyShape, worked: bool) {
        let flag = if worked {
            crate::model::WORKED_YES
        } else {
            crate::model::WORKED_NO
        };
        let joins = shape.joins as usize;
        if joins == 0 {
            shape.lead = flag;
        } else if shape.worked.len() < joins {
            shape.worked.push(flag);
        } else {
            let tail = shape.worked.len() - 1;
            shape.worked[tail] = shape.worked[tail].max(flag);
        }
    }

    /// The events so far were discarded (a replayed parent transcript): nothing to settle.
    pub fn reset(&mut self, events: &[crate::model::Event]) {
        self.text_at = events.len();
        self.excluded.clear();
    }

    /// The event at `index` was run by the user, not the agent: it never trails a turn.
    pub fn exclude(&mut self, index: usize) {
        self.excluded.push(index);
    }

    /// The file ended: the last turn's trailing calls are settled.
    pub fn finish(self, events: &mut [crate::model::Event]) {
        self.settle(events);
    }

    fn settle(&self, events: &mut [crate::model::Event]) {
        for (index, event) in events.iter_mut().enumerate().skip(self.text_at) {
            if self.excluded.contains(&index) {
                continue;
            }
            if !event.meta.is_empty() {
                event.meta.push(' ');
            }
            event.meta.push_str(AFTER_REPLY_META);
        }
    }
}

/// Is this a call that writes or reads the agent's task list? Claude TodoWrite/TaskUpdate,
/// opencode todowrite, Cursor todo_write, omp todo, Codex update_plan, Kimi SetTodoList,
/// Gemini write_todos.
pub fn is_task_tool(name: &str) -> bool {
    name == "TaskUpdate" || name == "update_plan" || name.to_ascii_lowercase().contains("todo")
}

/// A call that is not the agent's work: a todo/plan/task-list write or a `yield`, as summary
/// reads `after_reply` (its `_bookkeeping_call`).
pub fn is_bookkeeping_tool(name: &str) -> bool {
    is_task_tool(name)
        || name == "TaskCreate"
        || name == "TaskList"
        || name.eq_ignore_ascii_case("yield")
}

/// A recorded call that is the agent's work: a tool or a delegation, never bookkeeping.
pub fn is_work_event(event: &crate::model::Event) -> bool {
    (event.kind == "tool" || event.kind == "subagent_start") && !is_bookkeeping_tool(&event.name)
}

/// The task-list facts a call carried, kept whole where its display input is capped: the
/// todo/plan elements' names, statuses and ids, Cursor's merge flag, omp's todo op with its
/// targets, a Claude TaskUpdate's fields. Compact JSON with sorted keys, so summary reads it as
/// it reads an uncapped input; empty for other calls, for inputs carrying none of those facts,
/// and past `REPLY_CAP` characters.
pub fn task_state(name: &str, input: Option<&serde_json::Value>) -> String {
    use serde_json::Value;
    const ELEMENT_KEYS: &[&str] = &[
        "content",
        "title",
        "task",
        "step",
        "text",
        "name",
        "description",
        "status",
        "state",
        "id",
    ];
    const FIELD_KEYS: &[&str] = &[
        "op", "task", "phase", "subject", "status", "taskId", "id", "task_id",
    ];
    fn scalar(value: &Value) -> bool {
        matches!(value, Value::String(_) | Value::Number(_))
    }
    fn elements(items: &[Value]) -> Vec<Value> {
        items
            .iter()
            .filter_map(|item| match item {
                Value::String(_) => Some(item.clone()),
                Value::Object(fields) => Some(Value::Object(
                    fields
                        .iter()
                        .filter(|(key, value)| {
                            ELEMENT_KEYS.contains(&key.as_str()) && scalar(value)
                        })
                        .map(|(key, value)| (key.clone(), value.clone()))
                        .collect(),
                )),
                _ => None,
            })
            .collect()
    }
    if !is_task_tool(name) {
        return String::new();
    }
    let state = match input {
        Some(Value::Array(items)) => Value::Array(elements(items)),
        Some(Value::Object(fields)) => {
            let mut kept = serde_json::Map::new();
            for (key, value) in fields {
                let value = match (key.as_str(), value) {
                    ("todos" | "plan", Value::Array(items)) => Value::Array(elements(items)),
                    ("items", Value::Array(items)) => Value::Array(
                        items
                            .iter()
                            .filter(|item| item.is_string())
                            .cloned()
                            .collect(),
                    ),
                    ("list", Value::Array(phases)) => Value::Array(
                        phases
                            .iter()
                            .filter_map(|phase| {
                                let fields = phase.as_object()?;
                                let mut kept = serde_json::Map::new();
                                if let Some(name) = fields.get("phase").filter(|v| v.is_string()) {
                                    kept.insert("phase".to_string(), name.clone());
                                }
                                if let Some(Value::Array(items)) = fields.get("items") {
                                    kept.insert(
                                        "items".to_string(),
                                        Value::Array(
                                            items
                                                .iter()
                                                .filter(|i| i.is_string())
                                                .cloned()
                                                .collect(),
                                        ),
                                    );
                                }
                                Some(Value::Object(kept))
                            })
                            .collect(),
                    ),
                    ("merge", Value::Bool(_)) => value.clone(),
                    (key, value) if FIELD_KEYS.contains(&key) && scalar(value) => value.clone(),
                    _ => continue,
                };
                kept.insert(key.clone(), value);
            }
            Value::Object(kept)
        }
        _ => return String::new(),
    };
    let empty = match &state {
        Value::Array(items) => items.is_empty(),
        Value::Object(fields) => fields.is_empty(),
        _ => true,
    };
    if empty {
        return String::new();
    }
    let encoded = state.to_string();
    if encoded.chars().count() > REPLY_CAP {
        return String::new();
    }
    encoded
}

/// The whole list a Claude TaskList result printed, kept past the display cap so the replay
/// can reset to it; empty when the listing itself exceeds `REPLY_CAP` characters.
pub fn task_listing(output: &str) -> String {
    if output.trim().is_empty() || output.chars().count() > REPLY_CAP {
        return String::new();
    }
    serde_json::json!({ "listing": output }).to_string()
}

fn is_terminal_format(char: char) -> bool {
    matches!(char as u32,
        0x00ad | 0x0600..=0x0605 | 0x061c | 0x06dd | 0x070f |
        0x0890..=0x0891 | 0x08e2 | 0x180e | 0x200b..=0x200f |
        0x2028..=0x202e | 0x2060..=0x2064 | 0x2066..=0x206f | 0xfeff |
        0xfff9..=0xfffb | 0x110bd | 0x110cd | 0x13430..=0x1343f |
        0x1bca0..=0x1bca3 | 0x1d173..=0x1d17a | 0xe0001 |
        0xe0020..=0xe007f)
}

/// Escape terminal controls in store-derived diagnostics.
pub fn terminal_safe(value: impl std::fmt::Display) -> String {
    let mut out = String::new();
    for char in value.to_string().chars() {
        if char.is_control() || is_terminal_format(char) {
            use std::fmt::Write;
            write!(&mut out, "\\u{:04x}", char as u32).expect("writing to a String cannot fail");
        } else {
            out.push(char);
        }
    }
    out
}

/// Report a source that could not join this generation without letting store-derived paths or
/// errors write terminal controls.
pub fn warn_source_skip(agent: &str, path: &std::path::Path, reason: impl std::fmt::Display) {
    eprintln!(
        "  ! {agent}: skipping {}: {}",
        terminal_safe(path.display()),
        terminal_safe(reason)
    );
}

/// Truncate to `cap` characters (UTF-8 safe), appending an ellipsis when cut.
pub fn cap_str(s: &str, cap: usize) -> String {
    let mut it = s.char_indices();
    match it.nth(cap) {
        None => s.to_string(),
        Some((i, _)) => {
            let mut t = s[..i].to_string();
            t.push('…');
            t
        }
    }
}

pub fn cap_str_with_chars(s: &str, cap: usize) -> (String, usize) {
    (cap_str(s, cap), s.chars().count())
}

/// Cap an event output while retaining both source units used by display surfaces.
///
/// The byte count must be sampled from the uncapped source. In particular, UTF-8
/// byte length cannot be recovered from the capped excerpt (which also gains an
/// ellipsis when truncated).
pub fn cap_event_output(s: &str) -> (String, usize, usize) {
    (cap_str(s, EVENT_CAP), s.chars().count(), s.len())
}

/// Compact, human-meaningful summary of a tool-call input object: prefer the fields a
/// person would scan for (command line, file path, pattern, prompt), else compact JSON.
pub fn summarize_tool_input(input: &serde_json::Value) -> String {
    summarize_tool_input_with_chars(input).0
}

pub fn summarize_tool_input_with_chars(input: &serde_json::Value) -> (String, usize) {
    const KEYS: &[&str] = &[
        "command",
        "file_path",
        "notebook_path",
        "path",
        "pattern",
        "query",
        "url",
        "prompt",
        "description",
        "cmd",
    ];
    if let Some(obj) = input.as_object() {
        let mut parts: Vec<String> = Vec::new();
        for k in KEYS {
            match obj.get(*k) {
                Some(serde_json::Value::String(s)) if !s.trim().is_empty() => {
                    parts.push(s.trim().to_string());
                }
                // command arrays like ["bash","-lc","cargo build"]
                Some(serde_json::Value::Array(a)) => {
                    let joined: Vec<&str> = a.iter().filter_map(|x| x.as_str()).collect();
                    if !joined.is_empty() {
                        parts.push(joined.join(" "));
                    }
                }
                _ => {}
            }
        }
        if !parts.is_empty() {
            let text = parts.join(" · ");
            return cap_str_with_chars(&text, EVENT_CAP);
        }
    }
    if input.is_null() {
        return (String::new(), 0);
    }
    cap_str_with_chars(&input.to_string(), EVENT_CAP)
}

/// Is this content a command/system wrapper rather than something the user typed?
pub fn is_wrapper(text: &str) -> bool {
    let t = text.trim_start();
    t.starts_with("<command-name>")
        || t.starts_with("<command-message>")
        || t.starts_with("<command-args>")
        || t.starts_with("<local-command")
        || t.starts_with("<bash-input>")
        || t.starts_with("<bash-stdout>")
        || t.starts_with("<user-prompt-submit-hook>")
        || t.starts_with("Caveat:")
        || (t.contains("<command-name>") && t.contains("</command-name>"))
        || t.starts_with("<system-reminder>")
        // multi-agent orchestration chatter that isn't the user typing:
        || t.starts_with("<teammate-message")
        || t.starts_with("<task-notification")
        || t.starts_with("[SYSTEM NOTIFICATION")
        || t.starts_with("<system-notification")
        || (t.starts_with('{')
            && (t.contains("\"idle_notification\"")
                || t.contains("\"task_completed\"")
                || t.contains("\"shutdown_request\"")
                || t.contains("\"type\":\"idle\"")))
}

#[cfg(test)]
mod tests {
    #[cfg(not(windows))]
    use super::cleanup_stale_sqlite_snapshots;
    #[cfg(unix)]
    use super::open_sqlite_ro_with_hook;
    use super::sqlite_generation_token;
    #[cfg(target_os = "linux")]
    use super::SqliteSnapshot;
    use super::{open_sqlite_ro, read_lossy, sqlite_sidecar, sqlite_uri_path, terminal_safe};

    fn temp_dir(tag: &str) -> std::path::PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("agrep-{tag}-{}-{nonce}", std::process::id()));
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    #[cfg(unix)]
    fn fifo(path: &std::path::Path) {
        let status = std::process::Command::new("mkfifo")
            .arg(path)
            .status()
            .unwrap();
        assert!(status.success());
    }

    #[test]
    fn sqlite_uri_path_quotes_query_and_fragment_bytes() {
        let encoded = sqlite_uri_path(std::path::Path::new("/tmp/a?b#c%d e.db")).unwrap();
        assert_eq!(encoded, "/tmp/a%3Fb%23c%25d%20e.db");
    }

    fn raw() -> crate::model::RawMessage {
        crate::model::RawMessage {
            agent: "claude",
            project: String::new(),
            session: String::new(),
            ts: 0,
            turn: 0,
            text: String::new(),
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            reply_shape: Default::default(),
            side: false,
            parent: String::new(),
        }
    }

    /// 0.4.2's append, kept as the oracle for the text and the count.
    fn legacy_append(buf: &mut String, t: &str, cap: usize) -> usize {
        let t = t.trim();
        if t.is_empty() {
            return 0;
        }
        let source_chars = t.chars().count() + usize::from(!buf.is_empty());
        if buf.chars().count() >= cap {
            return source_chars;
        }
        if !buf.is_empty() {
            buf.push(' ');
        }
        buf.push_str(t);
        if buf.chars().count() > cap {
            let kept: String = buf.chars().take(cap).collect();
            *buf = kept;
            buf.push('…');
        }
        source_chars
    }

    #[test]
    fn reply_text_and_count_stay_what_0_4_2_published_while_the_shape_rides_beside() {
        let cap = super::REPLY_CAP;
        let long = "z".repeat(cap + 500);
        let exact = "e".repeat(cap);
        let almost = "x".repeat(cap - 1);
        let cases: Vec<Vec<&str>> = vec![
            vec![
                "Ported it.\n\n**Next steps:**\n- a\n- b",
                "Updated the callers.",
            ],
            vec!["one", "  ", "two", "three\n\nfour"],
            vec!["lone block"],
            vec![
                "a diff:\n```diff\n-old\n \n+new\n```",
                "Want me to run them?",
            ],
            vec!["step 1 ok\n \nstep 2 ok", "\n \n", "done"],
            vec!["head\n\nwith paragraphs", long.as_str(), "ending"],
            vec![exact.as_str(), "after the exact cap"],
            vec![almost.as_str(), "crosses at the join"],
            vec!["汉字", "字汉", long.as_str()],
        ];
        for blocks in cases {
            let mut raw = raw();
            let mut oracle = String::new();
            let mut oracle_chars = 0;
            for block in &blocks {
                let chars = super::append_reply(&mut raw, block);
                let expected = legacy_append(&mut oracle, block, cap);
                oracle_chars += expected;
                assert_eq!(chars, expected);
            }
            assert_eq!(raw.reply, oracle, "{:?}", &blocks[0][..8]);
            assert_eq!(raw.reply_chars, oracle_chars);
            let shape = &raw.reply_shape;
            assert_eq!(shape.head_chars as usize, raw.reply.chars().count());
            // every break is a joining space, and the block after it is the block appended
            let text: Vec<char> = raw.reply.chars().collect();
            let kept: Vec<&str> = blocks
                .iter()
                .map(|b| b.trim())
                .filter(|b| !b.is_empty())
                .collect();
            let mut stops: Vec<usize> = shape.breaks.iter().map(|b| *b as usize).collect();
            stops.push(text.len());
            for (index, &offset) in shape.breaks.iter().enumerate() {
                assert_eq!(text[offset as usize], ' ');
                let mut segment: String =
                    text[offset as usize + 1..stops[index + 1]].iter().collect();
                // the last segment of a capped head ends where the cap cut the block
                if raw.reply_chars > cap && stops[index + 1] == text.len() {
                    segment.pop();
                    assert!(kept[index + 1].starts_with(&segment), "{index}");
                } else {
                    assert_eq!(segment, kept[index + 1]);
                }
            }
            let capped = raw.reply_chars > cap;
            assert_eq!(!shape.end.is_empty(), capped);
            if capped {
                let last = kept.last().unwrap();
                assert!(last.ends_with(shape.end.as_str()));
                assert_eq!(shape.end_cut, last.chars().count() > super::REPLY_TAIL_CAP);
                assert!(shape.end.chars().count() <= super::REPLY_TAIL_CAP);
            }
        }
    }

    #[test]
    fn each_join_records_whether_work_came_between_its_blocks() {
        use crate::model::{WORKED_NO, WORKED_UNKNOWN, WORKED_YES};
        fn tool(name: &str) -> crate::model::Event {
            crate::model::Event {
                agent: "claude",
                session: "s".to_string(),
                ts: 0,
                kind: "tool",
                name: name.to_string(),
                input: String::new(),
                output: String::new(),
                input_chars: 0,
                output_chars: 0,
                output_bytes: 0,
                ok: None,
                call_id: name.to_string(),
                child_session: String::new(),
                meta: String::new(),
                state: String::new(),
            }
        }
        // blocks and calls in order: text, call, text, text, call(bookkeeping), text
        let mut message = raw();
        let mut order = super::ReplyOrder::default();
        let mut events = Vec::new();
        super::append_reply(&mut message, "plan");
        order.joined(&mut message.reply_shape, &events, events.len());
        order.reply(events.len());
        assert!(message.reply_shape.worked.is_empty());
        assert_eq!(message.reply_shape.lead, WORKED_NO);
        events.push(tool("Bash"));
        super::append_reply(&mut message, "ran it");
        order.joined(&mut message.reply_shape, &events, events.len());
        order.reply(events.len());
        super::append_reply(&mut message, "and more");
        order.joined(&mut message.reply_shape, &events, events.len());
        order.reply(events.len());
        events.push(tool("TaskCreate"));
        super::append_reply(&mut message, "noted");
        order.joined(&mut message.reply_shape, &events, events.len());
        order.reply(events.len());
        assert_eq!(
            message.reply_shape.worked,
            [WORKED_YES, WORKED_NO, WORKED_NO]
        );
        assert_eq!(message.reply_shape.joins, 3);

        // past the cap the tail's join covers every block it replaced: a call before a
        // replaced block still counts once the final tail lands
        let mut message = raw();
        let mut order = super::ReplyOrder::default();
        let mut events = Vec::new();
        let long = "z".repeat(super::REPLY_CAP + 1);
        super::append_reply(&mut message, &long);
        order.joined(&mut message.reply_shape, &events, events.len());
        order.reply(events.len());
        events.push(tool("Edit"));
        super::append_reply(&mut message, "replaced tail");
        order.joined(&mut message.reply_shape, &events, events.len());
        order.reply(events.len());
        super::append_reply(&mut message, "final tail");
        order.joined(&mut message.reply_shape, &events, events.len());
        order.reply(events.len());
        assert_eq!(message.reply_shape.end, "final tail");
        assert_eq!(
            (message.reply_shape.joins, &message.reply_shape.worked[..]),
            (1, &[WORKED_YES][..])
        );

        // an adapter whose store orders prose and calls itself records the fact per block;
        // a join never recorded stays unknown
        let mut message = raw();
        for (block, worked) in [
            ("a", true),
            ("b", false),
            (long.as_str(), true),
            ("tail", false),
        ] {
            super::append_reply(&mut message, block);
            super::ReplyOrder::joined_known(&mut message.reply_shape, worked);
        }
        assert_eq!(message.reply_shape.breaks.len(), 2);
        assert_eq!(
            (message.reply_shape.lead, &message.reply_shape.worked[..]),
            (WORKED_YES, &[WORKED_NO, WORKED_YES, WORKED_NO][..])
        );
        let mut message = raw();
        super::append_reply(&mut message, "a");
        super::append_reply(&mut message, "b");
        assert_eq!(message.reply_shape.lead, WORKED_UNKNOWN);
        assert!(message.reply_shape.worked.is_empty());

        // a delegation is work; a recap row's first block records the work since the previous
        // row's last block as its lead join (no prompt reset the order between them)
        let mut events = Vec::new();
        let mut order = super::ReplyOrder::default();
        let mut first = raw();
        super::append_reply(&mut first, "plan");
        order.joined(&mut first.reply_shape, &events, events.len());
        order.reply(events.len());
        let mut delegated = tool("Agent");
        delegated.kind = "subagent_start";
        events.push(delegated);
        let mut recap = raw();
        super::append_reply(&mut recap, "done");
        order.joined(&mut recap.reply_shape, &events, events.len());
        order.reply(events.len());
        assert_eq!(
            (first.reply_shape.lead, recap.reply_shape.lead),
            (WORKED_NO, WORKED_YES)
        );
        let mut prompted = raw();
        order.prompt(&mut events);
        super::append_reply(&mut prompted, "next");
        order.joined(&mut prompted.reply_shape, &events, events.len());
        assert_eq!(prompted.reply_shape.lead, WORKED_NO);
    }

    #[test]
    fn blocks_past_the_cap_cost_their_own_length_only() {
        let block = "b".repeat(400);
        let mut timings = Vec::new();
        for blocks in [3_000usize, 20_000, 60_000] {
            let started = std::time::Instant::now();
            let mut raw = raw();
            for _ in 0..blocks {
                super::append_reply(&mut raw, &block);
            }
            let new = started.elapsed();
            let started = std::time::Instant::now();
            let mut oracle = String::new();
            let mut chars = 0;
            for _ in 0..blocks {
                chars += legacy_append(&mut oracle, &block, super::REPLY_CAP);
            }
            let old = started.elapsed();
            assert_eq!(raw.reply, oracle);
            assert_eq!(raw.reply_chars, chars);
            timings.push((blocks, new, old));
        }
        eprintln!("append_reply vs 0.4.2: {timings:?}");
        for (blocks, new, old) in timings {
            assert!(
                new <= old * 2 + std::time::Duration::from_millis(50),
                "{blocks}: {new:?} vs {old:?}"
            );
        }
    }

    #[test]
    fn reply_order_leaves_user_run_commands_out() {
        let event = |name: &str| crate::model::Event {
            agent: "pi",
            session: "s".to_string(),
            ts: 0,
            kind: "tool",
            name: name.to_string(),
            input: String::new(),
            output: String::new(),
            input_chars: 0,
            output_chars: 0,
            output_bytes: 0,
            ok: None,
            call_id: name.to_string(),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        };
        let mut events = Vec::new();
        let mut order = super::ReplyOrder::new();
        order.prompt(&mut events);
        events.push(event("t1"));
        order.reply(events.len());
        events.push(event("user-bash"));
        order.exclude(events.len() - 1);
        events.push(event("t2"));
        order.finish(&mut events);
        let metas: Vec<&str> = events.iter().map(|e| e.meta.as_str()).collect();
        assert_eq!(metas, ["", "", "after_reply=1"]);
    }

    #[test]
    fn task_state_keeps_list_facts_whole_and_nothing_else() {
        let todos = serde_json::json!({
            "todos": [
                {"content": "Port the loader", "status": "pending", "activeForm": "Porting",
                 "priority": "high", "id": 7},
                "bare item",
                {"notes": "no name"},
                42
            ]
        });
        assert_eq!(
            super::task_state("TodoWrite", Some(&todos)),
            r#"{"todos":[{"content":"Port the loader","id":7,"status":"pending"},"bare item",{}]}"#
        );
        let cursor =
            serde_json::json!({"merge": true, "todos": [{"id": "a", "status": "completed"}]});
        assert_eq!(
            super::task_state("todo_write", Some(&cursor)),
            r#"{"merge":true,"todos":[{"id":"a","status":"completed"}]}"#
        );
        let omp = serde_json::json!({
            "op": "init", "list": [{"phase": "Port", "items": ["a", "b", 3], "extra": 1}],
            "items": ["c"], "phase": "Port", "task": "a", "explain": "why"
        });
        assert_eq!(
            super::task_state("todo", Some(&omp)),
            r#"{"items":["c"],"list":[{"items":["a","b"],"phase":"Port"}],"op":"init","phase":"Port","task":"a"}"#
        );
        let update = serde_json::json!({"taskId": "3", "status": "completed",
                                        "description": "long prose", "subject": "Ship it"});
        assert_eq!(
            super::task_state("TaskUpdate", Some(&update)),
            r#"{"status":"completed","subject":"Ship it","taskId":"3"}"#
        );
        let plan =
            serde_json::json!({"plan": [{"step": "a", "status": "pending"}], "explanation": "x"});
        assert_eq!(
            super::task_state("update_plan", Some(&plan)),
            r#"{"plan":[{"status":"pending","step":"a"}]}"#
        );
        assert_eq!(super::task_state("Bash", Some(&todos)), "");
        assert_eq!(
            super::task_state("TodoRead", Some(&serde_json::json!({}))),
            ""
        );
        assert_eq!(
            super::task_state("TodoWrite", Some(&serde_json::json!({"other": 1}))),
            ""
        );
        assert_eq!(super::task_state("TodoWrite", None), "");
        assert_eq!(
            super::task_state("SetTodoList", Some(&serde_json::json!(["x", "y"]))),
            r#"["x","y"]"#
        );
        let huge = serde_json::json!({"todos": [{"content": "z".repeat(super::REPLY_CAP)}]});
        assert_eq!(super::task_state("TodoWrite", Some(&huge)), "");
        assert_eq!(
            super::task_listing("#1 [pending] Port\n#2 [completed] Ship"),
            "{\"listing\":\"#1 [pending] Port\\n#2 [completed] Ship\"}"
        );
        assert_eq!(super::task_listing("  "), "");
        assert_eq!(super::task_listing(&"z".repeat(super::REPLY_CAP + 1)), "");
    }

    #[test]
    fn reply_order_marks_the_calls_no_reply_followed() {
        fn event(name: &str, meta: &str) -> crate::model::Event {
            crate::model::Event {
                agent: "codex",
                session: "s".to_string(),
                ts: 0,
                kind: "tool",
                name: name.to_string(),
                input: String::new(),
                output: String::new(),
                input_chars: 0,
                output_chars: 0,
                output_bytes: 0,
                ok: None,
                call_id: name.to_string(),
                child_session: String::new(),
                meta: meta.to_string(),
                state: String::new(),
            }
        }
        let mut events = Vec::new();
        let mut order = super::ReplyOrder::new();
        // turn 1: text, tool, text (answered); turn 2: text, tool, tool (cut off)
        order.prompt(&mut events);
        order.reply(events.len());
        events.push(event("t1", "name=x"));
        order.reply(events.len());
        order.prompt(&mut events);
        order.reply(events.len());
        events.push(event("t2", ""));
        events.push(event("t3", "to=y"));
        order.finish(&mut events);
        let metas: Vec<&str> = events.iter().map(|e| e.meta.as_str()).collect();
        assert_eq!(metas, ["name=x", "after_reply=1", "to=y after_reply=1"]);
        // a turn that wrote no reply at all: every call trails
        let mut events = vec![];
        let mut order = super::ReplyOrder::new();
        order.prompt(&mut events);
        events.push(event("t4", ""));
        order.prompt(&mut events);
        events.push(event("t5", ""));
        order.reply(events.len());
        order.finish(&mut events);
        assert_eq!(events[0].meta, "after_reply=1");
        assert_eq!(events[1].meta, "");
        // a reset after discarded events settles nothing stale
        let mut events = vec![event("stale", "")];
        let mut order = super::ReplyOrder::new();
        events.clear();
        order.reset(&events);
        order.finish(&mut events);
        assert!(events.is_empty());
    }

    #[test]
    fn terminal_diagnostics_escape_controls_and_bidi() {
        let rendered = terminal_safe("before\u{1b}]52;c;payload\u{7}\r\u{202e}after");
        assert_eq!(
            rendered,
            "before\\u001b]52;c;payload\\u0007\\u000d\\u202eafter"
        );
    }

    /// A live agent session is appended to continuously; the transcript reader must serve
    /// the prefix it opened rather than refuse every source that grew under it.
    #[test]
    fn lossy_reader_serves_a_live_appended_transcript() {
        let dir = temp_dir("lossy-live-append");
        let path = dir.join("rollout.jsonl");
        let line = format!("{{\"role\":\"user\",\"pad\":\"{}\"}}\n", "x".repeat(4000));
        let seeded = line.repeat(500);
        std::fs::write(&path, &seeded).unwrap();

        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        // Each read follows at least one more landed append, so growth under the
        // reader is guaranteed rather than left to thread scheduling.
        let (appended, landed) = std::sync::mpsc::channel::<()>();
        let writer = {
            let (path, line, stop) = (path.clone(), line.clone(), stop.clone());
            std::thread::spawn(move || {
                use std::io::Write;
                let mut file = std::fs::OpenOptions::new()
                    .append(true)
                    .open(&path)
                    .unwrap();
                while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                    file.write_all(line.as_bytes()).unwrap();
                    if appended.send(()).is_err() {
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_micros(200));
                }
            })
        };

        let mut lengths = Vec::new();
        let mut sample = String::new();
        for _ in 0..20 {
            landed
                .recv_timeout(std::time::Duration::from_secs(5))
                .expect("the appender must make progress");
            let text = read_lossy(&path).expect("a growing source stays readable");
            assert!(text.len() >= seeded.len());
            lengths.push(text.len());
            sample = text;
        }
        stop.store(true, std::sync::atomic::Ordering::Relaxed);
        writer.join().unwrap();
        assert_eq!(lengths.len(), 20);
        assert!(lengths.windows(2).all(|pair| pair[0] <= pair[1]));
        // Without real growth the reads never raced anything and prove nothing.
        assert!(lengths.last().unwrap() > &seeded.len());

        // Once the writer stops, the reader converges on the whole file, and every
        // extent served along the way was a true prefix of it.
        let settled = read_lossy(&path).unwrap();
        assert_eq!(settled.len() as u64, path.metadata().unwrap().len());
        assert!(settled.starts_with(&sample));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn source_read_errors_name_the_path() {
        let path = std::env::temp_dir().join(format!(
            "agrep-missing-source-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let error = read_lossy(&path).unwrap_err();
        assert!(error.to_string().contains(&path.display().to_string()));
    }

    #[cfg(unix)]
    #[test]
    fn lossy_reader_rejects_fifo_without_waiting() {
        let dir = temp_dir("lossy-fifo");
        let path = dir.join("transcript.jsonl");
        fifo(&path);
        let error = read_lossy(&path).unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
        assert!(error.to_string().contains(&path.display().to_string()));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(unix)]
    #[test]
    fn sqlite_path_swap_to_fifo_cannot_reach_sqlite_open() {
        let dir = temp_dir("sqlite-fifo-swap");
        let path = dir.join("state.vscdb");
        let held = dir.join("held.db");
        let writer = rusqlite::Connection::open(&path).unwrap();
        writer.execute("CREATE TABLE rows(value TEXT)", []).unwrap();
        drop(writer);

        let error = open_sqlite_ro_with_hook(&path, || {
            std::fs::rename(&path, &held).unwrap();
            fifo(&path);
        })
        .err()
        .expect("source replacement must invalidate the snapshot");
        assert!(error.to_string().contains("cannot safely open"));
        std::fs::remove_file(&path).unwrap();
        std::fs::rename(&held, &path).unwrap();
        let _ = std::fs::remove_dir_all(dir);
    }

    /// A FIFO, or a link to one, where a store's rollback journal belongs fails that store's
    /// open at once; it never blocks the pass waiting for a writer to the FIFO.
    #[cfg(unix)]
    #[test]
    fn a_fifo_journal_fails_the_open_without_blocking() {
        for linked in [false, true] {
            let dir = temp_dir("sqlite-fifo-journal");
            let path = dir.join("state.vscdb");
            rusqlite::Connection::open(&path)
                .unwrap()
                .execute("CREATE TABLE rows(value TEXT)", [])
                .unwrap();
            let journal = sqlite_sidecar(&path, "-journal");
            if linked {
                let target = dir.join("elsewhere");
                fifo(&target);
                std::os::unix::fs::symlink(&target, &journal).unwrap();
            } else {
                fifo(&journal);
            }
            let (done, outcome) = std::sync::mpsc::channel();
            let opened = path.clone();
            std::thread::spawn(move || {
                let _ = done.send(open_sqlite_ro(&opened).is_err());
            });
            let failed = outcome
                .recv_timeout(std::time::Duration::from_secs(10))
                .expect("the open blocked on the journal");
            assert!(failed, "linked {linked}");
            let _ = std::fs::remove_dir_all(dir);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn sqlite_snapshot_does_not_change_source_freshness() {
        let dir = temp_dir("sqlite-source-freshness");
        let path = dir.join("state.vscdb");
        let writer = rusqlite::Connection::open(&path).unwrap();
        writer.execute("CREATE TABLE rows(value TEXT)", []).unwrap();
        drop(writer);
        let metadata = std::fs::symlink_metadata(&path).unwrap();
        let before = crate::ingest::registry::metadata_change_token(&path, &metadata).unwrap();
        let generation = sqlite_generation_token(&path).unwrap();
        drop(open_sqlite_ro(&path).unwrap());
        let metadata = std::fs::symlink_metadata(&path).unwrap();
        let after = crate::ingest::registry::metadata_change_token(&path, &metadata).unwrap();
        assert_eq!(before, after);
        assert_eq!(generation, sqlite_generation_token(&path).unwrap());
        let _ = std::fs::remove_dir_all(dir);
    }

    /// A read-only open of a WAL-mode database creates the -wal itself. That empty file carries
    /// no committed frames, so it must read as the same generation as no WAL at all - otherwise
    /// every checkpointed store answers "generation changed while opening" forever.
    #[test]
    fn generation_reads_an_empty_wal_as_no_wal() {
        let dir = temp_dir("sqlite-empty-wal");
        let path = dir.join("state.vscdb");
        std::fs::write(&path, b"sqlite fixture").unwrap();
        let wal = sqlite_sidecar(&path, "-wal");
        let absent = sqlite_generation_token(&path).unwrap();
        std::fs::write(&wal, b"").unwrap();
        assert_eq!(absent, sqlite_generation_token(&path).unwrap());
        std::fs::write(&wal, b"committed frame").unwrap();
        assert_ne!(absent, sqlite_generation_token(&path).unwrap());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(unix)]
    #[test]
    fn generation_detects_same_size_restored_mtime_edit() {
        let dir = temp_dir("sqlite-restored-mtime");
        let path = dir.join("state.vscdb");
        let stamp = dir.join("stamp");
        std::fs::write(&path, b"original").unwrap();
        std::fs::write(&stamp, b"stamp").unwrap();
        assert!(std::process::Command::new("touch")
            .args(["-r", path.to_str().unwrap(), stamp.to_str().unwrap()])
            .status()
            .unwrap()
            .success());
        let before = sqlite_generation_token(&path).unwrap();
        std::fs::write(&path, b"mutation").unwrap();
        assert!(std::process::Command::new("touch")
            .args(["-r", stamp.to_str().unwrap(), path.to_str().unwrap()])
            .status()
            .unwrap()
            .success());
        assert_ne!(before, sqlite_generation_token(&path).unwrap());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_no_reflink_fallback_is_fast_and_freshness_neutral() {
        use std::os::unix::fs::MetadataExt;

        let dir = temp_dir("sqlite-linux-hardlink");
        let path = dir.join("state.vscdb");
        std::fs::write(&path, b"sqlite fixture").unwrap();
        let before_metadata = std::fs::symlink_metadata(&path).unwrap();
        let generic_before =
            crate::ingest::registry::metadata_change_token(&path, &before_metadata).unwrap();
        crate::ingest::registry::wait_for_next_ctime_tick(&path);
        let generation = sqlite_generation_token(&path).unwrap();
        let snapshot = SqliteSnapshot::create_with(&path, |_, _| Ok(false)).unwrap();
        assert_eq!(
            std::fs::metadata(&path).unwrap().ino(),
            std::fs::metadata(&snapshot.database).unwrap().ino()
        );
        snapshot.verify().unwrap();
        assert_eq!(generation, sqlite_generation_token(&path).unwrap());
        drop(snapshot);
        assert_eq!(generation, sqlite_generation_token(&path).unwrap());
        let after_metadata = std::fs::symlink_metadata(&path).unwrap();
        let generic_after =
            crate::ingest::registry::metadata_change_token(&path, &after_metadata).unwrap();
        assert_ne!(generic_before, generic_after);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_hardlink_fallback_shares_wal_locking_with_a_writer() {
        use std::os::unix::fs::MetadataExt;

        let dir = temp_dir("sqlite-linux-live-wal");
        let path = dir.join("state.vscdb");
        let writer = rusqlite::Connection::open(&path).unwrap();
        writer.pragma_update(None, "journal_mode", "wal").unwrap();
        writer.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
        writer
            .execute("CREATE TABLE rows(value INTEGER)", [])
            .unwrap();
        writer.execute("INSERT INTO rows VALUES (0)", []).unwrap();
        assert!(sqlite_sidecar(&path, "-shm").exists());
        let snapshot = SqliteSnapshot::create_with(&path, |_, _| Ok(false)).unwrap();
        assert!(snapshot.live_hardlinks);
        for suffix in ["", "-wal", "-shm"] {
            let source = if suffix.is_empty() {
                path.clone()
            } else {
                sqlite_sidecar(&path, suffix)
            };
            let alias = if suffix.is_empty() {
                snapshot.database.clone()
            } else {
                sqlite_sidecar(&snapshot.database, suffix)
            };
            assert_eq!(
                std::fs::metadata(source).unwrap().ino(),
                std::fs::metadata(alias).unwrap().ino()
            );
        }
        let reader = rusqlite::Connection::open_with_flags(
            &snapshot.database,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        let writes = std::thread::spawn(move || {
            for value in 1..=100 {
                writer
                    .execute("INSERT INTO rows VALUES (?1)", [value])
                    .unwrap();
            }
        });
        for _ in 0..100 {
            let count: i64 = reader
                .query_row("SELECT count(*) FROM rows", [], |row| row.get(0))
                .unwrap();
            assert!(count >= 1);
        }
        writes.join().unwrap();
        let count: i64 = reader
            .query_row("SELECT count(*) FROM rows", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 101);
        assert!(snapshot.verify().is_err());
        drop(reader);
        drop(snapshot);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[cfg(unix)]
    #[test]
    fn stale_snapshot_cleanup_is_exact_and_does_not_follow_links() {
        let dir = temp_dir("sqlite-stale-cleanup");
        let mut exited = std::process::Command::new("true").spawn().unwrap();
        let dead_pid = exited.id();
        assert!(exited.wait().unwrap().success());
        let stale = dir.join(format!(".agrep-sqlite-{dead_pid}-abcd-0"));
        let unrelated = dir.join(".agrep-sqlite-not-ours");
        let target = dir.join("target");
        let linked = dir.join(format!(".agrep-sqlite-{dead_pid}-abcd-1"));
        let live = dir.join(format!(".agrep-sqlite-{}-abcd-2", std::process::id()));
        std::fs::create_dir(&stale).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&stale, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(stale.join("snapshot.db"), b"clone").unwrap();
        std::fs::create_dir(&unrelated).unwrap();
        std::fs::create_dir(&target).unwrap();
        std::fs::create_dir(&live).unwrap();
        std::fs::set_permissions(&live, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(live.join("snapshot.db"), b"live").unwrap();
        std::os::unix::fs::symlink(&target, &linked).unwrap();
        assert_eq!(
            cleanup_stale_sqlite_snapshots(&dir, std::time::Duration::ZERO, 8).unwrap(),
            1
        );
        assert!(!stale.exists());
        assert!(unrelated.exists());
        assert!(linked.exists());
        assert!(target.exists());
        assert!(live.exists());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn read_only_sqlite_rejects_rollback_journal() {
        let dir = temp_dir("sqlite-rollback-journal");
        let path = dir.join("state.vscdb");
        let writer = rusqlite::Connection::open(&path).unwrap();
        writer.execute("CREATE TABLE rows(value TEXT)", []).unwrap();
        drop(writer);
        std::fs::write(sqlite_sidecar(&path, "-journal"), b"hot").unwrap();
        assert!(open_sqlite_ro(&path).is_err());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn read_only_sqlite_sees_committed_wal_without_blocking_writer() {
        let dir = temp_dir("sqlite-live-wal");
        #[cfg(windows)]
        let path = dir.join("history #% space.db");
        #[cfg(not(windows))]
        let path = dir.join("history #?% space.db");
        let writer = rusqlite::Connection::open(&path).unwrap();
        writer.pragma_update(None, "journal_mode", "wal").unwrap();
        writer.pragma_update(None, "wal_autocheckpoint", 0).unwrap();
        writer.execute("CREATE TABLE rows(value TEXT)", []).unwrap();
        writer
            .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))
            .unwrap();
        writer
            .execute("INSERT INTO rows VALUES ('wal-one')", [])
            .unwrap();
        assert!(
            std::fs::metadata(sqlite_sidecar(&path, "-wal"))
                .unwrap()
                .len()
                > 0
        );

        let reader = open_sqlite_ro(&path).unwrap();
        let first: String = reader
            .query_row("SELECT value FROM rows", [], |row| row.get(0))
            .unwrap();
        assert_eq!(first, "wal-one");
        writer
            .execute("INSERT INTO rows VALUES ('wal-two')", [])
            .unwrap();
        drop(reader);

        let reader = open_sqlite_ro(&path).unwrap();
        let count: i64 = reader
            .query_row("SELECT count(*) FROM rows", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 2);
        drop(reader);
        drop(writer);
        let _ = std::fs::remove_dir_all(dir);
    }

    /// A SQLite store's failure identity ignores what agrep's own snapshot does to it: a hard
    /// link (the fallback where no reflink exists) moves the store's ctime, never its identity.
    #[cfg(unix)]
    #[test]
    fn a_hard_link_snapshot_leaves_a_stores_failure_identity_unchanged() {
        let dir = temp_dir("failure-identity-link");
        let path = dir.join("store.db");
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("CREATE TABLE t(x);")
            .unwrap();
        let before = super::failure_identity(&path);
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::hard_link(&path, dir.join("snapshot.db")).unwrap();
        assert_eq!(super::failure_identity(&path), before);
        let _ = std::fs::remove_dir_all(dir);
    }

    /// A file's failure identity covers its content: torn, whole and torn again at one length
    /// and one modification time, the whole copy reads as another failure.
    #[test]
    fn a_files_failure_identity_covers_its_content() {
        let dir = temp_dir("failure-identity-content");
        let path = dir.join("session.json");
        let digest = |body: &[u8]| {
            std::fs::write(&path, body).unwrap();
            let identity = super::failure_identity(&path);
            identity.rsplit(':').next().unwrap().to_owned()
        };
        let torn = digest(b"{\"torn\": tru");
        let whole = digest(b"{\"whole\": 1}");
        assert_ne!(torn, whole);
        assert_eq!(digest(b"{\"torn\": tru"), torn);
        let _ = std::fs::remove_dir_all(dir);
    }

    /// A failure whose file was stamped within a timestamp tick of the observation is racy: a
    /// whole-second stamp (a 1 s filesystem) stays racy for its two-second granule, then is not.
    #[cfg(not(windows))]
    #[test]
    fn a_failure_stamped_within_a_tick_of_its_observation_is_racy() {
        const SECOND: i64 = 1_000_000_000;
        let stamp = 100 * SECOND;
        let fine = 100 * SECOND - 50_000_123;
        assert!(super::racy_failure_stamps(Some(stamp), Some(fine), stamp + SECOND / 2, stamp + SECOND));
        assert!(super::racy_failure_stamps(Some(fine), Some(stamp), stamp + SECOND, stamp + SECOND));
        assert!(!super::racy_failure_stamps(
            Some(stamp),
            Some(fine),
            stamp + 10 * SECOND,
            stamp + 10 * SECOND
        ));
        assert!(!super::racy_failure_stamps(None, None, stamp, stamp));
    }
}
