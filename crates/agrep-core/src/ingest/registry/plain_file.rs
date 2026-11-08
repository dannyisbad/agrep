//! Plain regular files: telling links and absences apart, the open flags that never follow a
//! link or block on a special file, and the metadata comparisons that prove a file unchanged.

use std::fs;
use std::io::ErrorKind;
use std::path::Path;

pub fn metadata_is_link(metadata: &fs::Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
        metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    false
}

/// Whether a failed `symlink_metadata(path)` proves nothing exists at `path`: ENOENT, or a
/// parent that is not a directory (or a name too long) where the deepest ancestor that stats is
/// not a link. A link there is mispointed and, like a loop (ELOOP), hides data that may be
/// intact; EACCES and EIO likewise stay unavailable and retain their rows.
pub fn stat_proves_absent(path: &Path, error: &std::io::Error) -> bool {
    match error.kind() {
        ErrorKind::NotFound => true,
        ErrorKind::NotADirectory | ErrorKind::InvalidFilename => path
            .ancestors()
            .skip(1)
            .find_map(|ancestor| fs::symlink_metadata(ancestor).ok())
            .is_some_and(|metadata| !metadata_is_link(&metadata)),
        _ => false,
    }
}

pub fn plain_metadata(path: &Path) -> std::io::Result<Option<fs::Metadata>> {
    let metadata = fs::symlink_metadata(path)?;
    Ok((!metadata_is_link(&metadata)).then_some(metadata))
}

pub fn plain_entry_metadata(entry: &fs::DirEntry) -> std::io::Result<Option<fs::Metadata>> {
    let metadata = entry.metadata()?;
    Ok((!metadata_is_link(&metadata)).then_some(metadata))
}

/// Read-open flags that refuse a final-component symlink (ELOOP) and never block on a FIFO.
/// The O_* values differ by architecture: x86_64's O_NOFOLLOW is O_LARGEFILE on aarch64 Linux.
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub fn nofollow_read_options(options: &mut fs::OpenOptions) {
    use std::os::unix::fs::OpenOptionsExt;

    options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub(crate) fn secure_read_options(options: &mut fs::OpenOptions) {
    nofollow_read_options(options);
}

#[cfg(windows)]
pub(crate) fn secure_read_options(options: &mut fs::OpenOptions) {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    options
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
pub(crate) fn secure_read_options(_options: &mut fs::OpenOptions) {}

#[cfg(unix)]
fn same_plain_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    left.dev() == right.dev() && left.ino() == right.ino()
}

#[cfg(windows)]
fn same_plain_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    left.creation_time() == right.creation_time()
        && left.file_size() == right.file_size()
        && left.file_attributes() == right.file_attributes()
}

#[cfg(not(any(unix, windows)))]
fn same_plain_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.len() == right.len() && left.modified().ok() == right.modified().ok()
}

#[cfg(unix)]
pub(super) fn same_plain_snapshot(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;

    same_plain_file(left, right)
        && left.len() == right.len()
        && left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime() == right.ctime()
        && left.ctime_nsec() == right.ctime_nsec()
}

#[cfg(not(unix))]
pub(super) fn same_plain_snapshot(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    same_plain_file(left, right)
        && left.len() == right.len()
        && left.modified().ok() == right.modified().ok()
}

/// Identity without extent: what stays true of a file a writer is still appending to.
#[cfg(unix)]
pub(super) fn same_growing_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    same_plain_file(left, right)
}

#[cfg(windows)]
pub(super) fn same_growing_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;

    left.creation_time() == right.creation_time()
        && left.file_attributes() == right.file_attributes()
}

#[cfg(not(any(unix, windows)))]
pub(super) fn same_growing_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.len() <= right.len()
}
