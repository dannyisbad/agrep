//! The private copy-on-write snapshot a POSIX read-only open resolves instead of the live store:
//! the database and its WAL cloned (or copied) into a private directory, verified unchanged
//! against their sources before and after SQLite opens them.

use std::path::{Path, PathBuf};

use super::journal::{rollback_journal_key, rollback_journal_state};
use super::{same_plain_file, same_plain_snapshot, sqlite_generation_token, sqlite_sidecar};

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

/// Copy `opened` whole into a new `destination` where no copy-on-write clone could be made.
fn copy_into_new_file(opened: &mut std::fs::File, destination: &Path) -> std::io::Result<()> {
    use std::io::Seek;
    let mut output = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)?;
    opened.seek(std::io::SeekFrom::Start(0))?;
    std::io::copy(opened, &mut output)?;
    output.sync_all()
}

/// The snapshot copy at `destination` is a plain regular file of the source's length.
fn verify_snapshot_copy(
    source: &Path,
    destination: &Path,
    metadata: &std::fs::Metadata,
) -> std::io::Result<()> {
    let destination_metadata = std::fs::symlink_metadata(destination)?;
    if crate::ingest::registry::metadata_is_link(&destination_metadata)
        || !destination_metadata.is_file()
        || destination_metadata.len() != metadata.len()
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("invalid snapshot of {}", source.display()),
        ));
    }
    Ok(())
}

struct ClonedSqliteFile {
    source: PathBuf,
    opened: std::fs::File,
    metadata: std::fs::Metadata,
}

impl ClonedSqliteFile {
    fn verify(&self) -> std::io::Result<()> {
        let source = std::fs::symlink_metadata(&self.source)?;
        let opened = self.opened.metadata()?;
        if crate::ingest::registry::metadata_is_link(&source)
            || !source.is_file()
            || !same_plain_file(&opened, &source)
            || !same_plain_snapshot(&self.metadata, &opened)
            || !same_plain_snapshot(&opened, &source)
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

pub(super) struct SqliteSnapshot {
    directory: PathBuf,
    pub(super) database: PathBuf,
    cloned: Vec<ClonedSqliteFile>,
    wal_missing: bool,
    source: PathBuf,
    pub(super) generation: String,
    /// The idle rollback journal the snapshot began beside, which must not change under it.
    journal: Option<String>,
}

impl SqliteSnapshot {
    fn private_directory() -> std::io::Result<PathBuf> {
        use std::sync::atomic::{AtomicU64, Ordering};

        static NEXT_ALIAS: AtomicU64 = AtomicU64::new(0);
        let root = std::env::temp_dir();
        for attempt in 0..32_u64 {
            let sequence = NEXT_ALIAS.fetch_add(1, Ordering::Relaxed);
            let nonce = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            let candidate = root.join(format!(
                ".agrep-05-input-{}-{nonce:x}-{:x}",
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
                Ok(()) => return Ok(candidate),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "could not allocate a private SQLite snapshot directory",
        ))
    }

    pub(super) fn create(source: &Path) -> std::io::Result<Self> {
        let journal = rollback_journal_state(source)?;
        let generation = sqlite_generation_token(source)?;
        let directory = Self::private_directory()?;
        let database = directory.join("snapshot.db");
        let mut snapshot = Self {
            directory,
            database,
            cloned: Vec::new(),
            wal_missing: false,
            source: source.to_path_buf(),
            generation,
            journal,
        };
        let database_alias = snapshot.database.clone();
        snapshot.clone_file(source, &database_alias, true)?;
        let wal_source = sqlite_sidecar(source, "-wal");
        let wal_alias = sqlite_sidecar(&snapshot.database, "-wal");
        snapshot.clone_file(&wal_source, &wal_alias, false)?;
        snapshot.verify()?;
        Ok(snapshot)
    }

    /// Clone `source` to `destination`; an absent source that is not `required` (the WAL) is
    /// recorded as missing instead.
    fn clone_file(
        &mut self,
        source: &Path,
        destination: &Path,
        required: bool,
    ) -> std::io::Result<()> {
        let (mut opened, metadata) = match open_snapshot_source(source) {
            Ok(opened) => opened,
            Err(error) if !required && error.kind() == std::io::ErrorKind::NotFound => {
                self.wal_missing = true;
                return Ok(());
            }
            Err(error) => return Err(error),
        };
        if !clone_cow(&opened, destination)? {
            copy_into_new_file(&mut opened, destination)?;
        }
        verify_snapshot_copy(source, destination, &metadata)?;
        let cloned = ClonedSqliteFile {
            source: source.to_path_buf(),
            opened,
            metadata,
        };
        cloned.verify()?;
        self.cloned.push(cloned);
        Ok(())
    }

    pub(super) fn verify(&self) -> std::io::Result<()> {
        for cloned in &self.cloned {
            cloned.verify()?;
        }
        if self.wal_missing {
            self.verify_wal_still_missing()?;
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

    fn verify_wal_still_missing(&self) -> std::io::Result<()> {
        match std::fs::symlink_metadata(sqlite_sidecar(&self.source, "-wal")) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Ok(_) => Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "SQLite WAL appeared while snapshotting",
            )),
            Err(error) => Err(error),
        }
    }
}

impl Drop for SqliteSnapshot {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}
