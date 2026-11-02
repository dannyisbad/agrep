//! File operations for user-owned integration files and native backups.
use anyhow::{bail, Context, Result};
use std::{
    fs::{self, Metadata},
    io::Write,
    path::{Component, Path},
};

pub fn linked(meta: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        meta.file_type().is_symlink() || meta.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        meta.file_type().is_symlink()
    }
}

pub fn identity(meta: &Metadata) -> (u64, Option<std::time::SystemTime>, u64, i128) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        (
            meta.len(),
            meta.modified().ok(),
            meta.ino(),
            i128::from(meta.ctime()) * 1_000_000_000 + i128::from(meta.ctime_nsec()),
        )
    }
    #[cfg(not(unix))]
    {
        (meta.len(), meta.modified().ok(), 0, 0)
    }
}

/// Reject links at every existing component, including the leaf, and lexical traversal.
pub fn plain_path(path: &Path) -> Result<()> {
    if !path.is_absolute() {
        bail!("path must be absolute: {}", path.display())
    }
    let mut p = std::path::PathBuf::new();
    for part in path.components() {
        if matches!(part, Component::ParentDir) {
            bail!("path traversal: {}", path.display())
        }
        p.push(part);
        // macOS exposes its system temporary roots through these fixed aliases.
        // They are not user-controlled source/destination redirects.
        #[cfg(target_os = "macos")]
        if matches!(p.to_str(), Some("/var" | "/tmp"))
            && fs::read_link(&p).ok().map(|v| {
                if v.is_absolute() {
                    v
                } else {
                    std::path::Path::new("/").join(v)
                }
            }) == Some(std::path::PathBuf::from(format!("/private{}", p.display())))
        {
            continue;
        }
        match fs::symlink_metadata(&p) {
            Ok(m) if linked(&m) => bail!("symlink or reparse point: {}", p.display()),
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e).with_context(|| p.display().to_string()),
        }
    }
    Ok(())
}

/// Resolve a user's dotfile link while leaving strict restore validation separate.
fn user_file(path: &Path) -> Result<std::path::PathBuf> {
    match fs::metadata(path) {
        Ok(m) if !m.is_file() => bail!("not a regular file: {}", path.display()),
        Ok(_) => Ok(fs::canonicalize(path)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(path.to_path_buf()),
        Err(e) => Err(e.into()),
    }
}
pub fn read_optional(path: &Path) -> Result<Option<Vec<u8>>> {
    let path = user_file(path)?;
    match fs::read(&path) {
        Ok(b) => Ok(Some(b)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e).with_context(|| path.display().to_string()),
    }
}

pub fn write_atomic(path: &Path, bytes: &[u8], overwrite: bool) -> Result<()> {
    let resolved = user_file(path)?;
    let path = resolved.as_path();
    let old = fs::metadata(path).ok();
    let before = old.as_ref().map(identity);
    if old.as_ref().is_some_and(|m| m.permissions().readonly()) {
        bail!("read-only file: {}", path.display())
    }
    let parent = path.parent().context("path has no parent")?;
    fs::create_dir_all(parent)?;
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    tmp.write_all(bytes)?;
    if let Some(m) = old {
        tmp.as_file().set_permissions(m.permissions())?;
    }
    tmp.as_file().sync_all()?;
    if fs::metadata(path).ok().as_ref().map(identity) != before {
        bail!("file changed during write: {}", path.display());
    }
    if overwrite {
        tmp.persist(path).map_err(|e| e.error)?;
    } else {
        tmp.persist_noclobber(path).map_err(|e| e.error)?;
    }
    #[cfg(unix)]
    std::fs::File::open(parent)?.sync_all()?;
    Ok(())
}

pub fn remove(path: &Path) -> Result<()> {
    let path = user_file(path)?;
    match fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.into()),
    }
}
