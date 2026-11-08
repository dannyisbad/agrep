//! Validate per-process caller publications and walk process ancestry.
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};

fn publication_root() -> PathBuf {
    if let Some(p) = std::env::var_os("AGREP_CALLER_PUBLICATION_DIR") {
        return p.into();
    }
    #[cfg(unix)]
    {
        PathBuf::from(format!("/tmp/agrep-0.5-caller-v1-{}", unsafe {
            libc::geteuid()
        }))
    }
    #[cfg(not(unix))]
    {
        std::env::temp_dir().join("agrep-0.5-caller-v1-user")
    }
}
pub(super) fn read_publication(root: &Path, pid: u32) -> Option<Vec<String>> {
    let meta = std::fs::symlink_metadata(root).ok()?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.mode() & 0o777 != 0o700 || meta.uid() != unsafe { libc::geteuid() } {
            return None;
        }
    }
    let path = root.join(format!("{pid}.json"));
    let meta = std::fs::symlink_metadata(&path).ok()?;
    if !meta.is_file() || meta.file_type().is_symlink() || meta.len() > 65536 {
        return None;
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let mut file = options.open(path).ok()?;
    let meta = file.metadata().ok()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != unsafe { libc::geteuid() } {
            return None;
        }
    }
    if !meta.is_file() || meta.len() > 65536 {
        return None;
    }
    let mut bytes = Vec::new();
    use std::io::Read;
    file.by_ref().take(65537).read_to_end(&mut bytes).ok()?;
    if bytes.len() > 65536 {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    let process = crate::caller_process::info(pid)?;
    if let Some(start) = v["start"].as_str() {
        if process.identity != start {
            return None;
        }
    } else if v["updated"].as_i64()? + 2000 < process.born_ms {
        return None;
    }
    if v["pid"].as_u64()? != u64::from(pid) {
        return None;
    }
    let values = v["sessions"].as_array()?;
    if values.is_empty() || values.len() > 64 {
        return None;
    }
    let mut sessions = Vec::new();
    for value in values {
        let session = value.as_str()?;
        if session.is_empty()
            || session.len() > 128
            || !session
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
        {
            return None;
        }
        if !sessions.iter().any(|s| s == session) {
            sessions.push(session.to_owned());
        }
    }
    Some(sessions)
}

pub(super) fn published() -> Option<Vec<String>> {
    let root = publication_root();
    if !root.exists() {
        return None;
    }
    let mut pid = std::process::id();
    let mut seen = BTreeSet::new();
    for _ in 0..24 {
        pid = crate::caller_process::info(pid)?.parent;
        if pid <= 1 || !seen.insert(pid) {
            return None;
        }
        if let Some(s) = read_publication(&root, pid) {
            return Some(s);
        }
    }
    None
}
