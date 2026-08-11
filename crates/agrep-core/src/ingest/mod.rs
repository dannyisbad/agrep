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

fn sqlite_open_io_error(path: &Path, error: std::io::Error) -> rusqlite::Error {
    rusqlite::Error::SqliteFailure(
        rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_CANTOPEN),
        Some(format!("cannot safely open {}: {error}", path.display())),
    )
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
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(0x20_000 | 0x800);
    }
    #[cfg(target_os = "macos")]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(0x100 | 0x4);
    }
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
        let journal = sqlite_sidecar(source, "-journal");
        match std::fs::symlink_metadata(&journal) {
            Ok(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::WouldBlock,
                    format!("rollback journal is active for {}", source.display()),
                ));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
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
        match std::fs::symlink_metadata(sqlite_sidecar(&self.source, "-journal")) {
            Ok(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::WouldBlock,
                    "SQLite rollback journal appeared while snapshotting",
                ));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
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
                    format!("rollback journal is active for {}", path.display()),
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

/// Append `t` to `buf` (space-joined), capping the result to `cap` characters (UTF-8 safe).
/// Returns the normalized source characters represented by this append, even after the cap.
pub fn append_capped(buf: &mut String, t: &str, cap: usize) -> usize {
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

/// Cap on indexed reply text; original lengths remain available for truncation diagnostics.
pub const REPLY_CAP: usize = 64_000;

/// Cap on event summaries. Original lengths remain in the event row to disclose truncation.
pub const EVENT_CAP: usize = 800;

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
    use super::sqlite_generation_token;
    #[cfg(target_os = "linux")]
    use super::SqliteSnapshot;
    use super::{
        append_capped, open_sqlite_ro, open_sqlite_ro_with_hook, read_lossy, sqlite_sidecar,
        sqlite_uri_path, terminal_safe,
    };

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

    #[test]
    fn capped_append_keeps_counting_source_characters() {
        let mut reply = String::new();
        let mut chars = append_capped(&mut reply, "  abcdef  ", 5);
        chars += append_capped(&mut reply, "汉字", 5);
        assert_eq!(reply, "abcde…");
        assert_eq!(chars, 9);
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
                    std::thread::sleep(std::time::Duration::from_micros(200));
                }
            })
        };

        let mut lengths = Vec::new();
        let mut sample = String::new();
        for _ in 0..20 {
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
}
