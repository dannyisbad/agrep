//! Path identity and classification: comparing paths as the platform resolves them, and what a
//! symlink met inside a store points at.

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

#[cfg(not(windows))]
pub fn path_eq(left: &Path, right: &Path) -> bool {
    left == right
}

#[cfg(not(windows))]
pub fn relative_path(path: &Path, root: &Path) -> Option<PathBuf> {
    path.strip_prefix(root).ok().map(Path::to_path_buf)
}

/// What a symlink met while walking a store is, normally agrep's own: its target is indexed
/// directly (an alias) or was since removed.
pub enum StoreLink {
    /// a regular transcript inside the store, reached by this walked path
    Alias(PathBuf),
    /// a transcript path inside the store with nothing there any more
    Dangling,
    /// anything else: outside the store, not a transcript, a loop, a directory
    Foreign,
}

fn lexical_path(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

pub fn classify_store_link(link: &Path, root: &Path, accepts: &dyn Fn(&Path) -> bool) -> StoreLink {
    if !accepts(link) {
        return StoreLink::Foreign;
    }
    let Ok(raw) = fs::read_link(link) else {
        return StoreLink::Foreign;
    };
    let lexical = if raw.is_absolute() {
        raw
    } else {
        link.parent()
            .map_or_else(|| raw.clone(), |parent| parent.join(&raw))
    };
    if !accepts(&lexical) {
        return StoreLink::Foreign;
    }
    let canonical_root = fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    match fs::canonicalize(&lexical) {
        Ok(target) => resolved_link(&target, root, &canonical_root),
        Err(error) if error.kind() == ErrorKind::NotFound => {
            missing_link(&lexical, root, &canonical_root)
        }
        Err(_) => StoreLink::Foreign,
    }
}

/// A link whose target resolves: an alias when it is a regular file inside the store.
fn resolved_link(target: &Path, root: &Path, canonical_root: &Path) -> StoreLink {
    if !fs::symlink_metadata(target).is_ok_and(|metadata| metadata.is_file()) {
        return StoreLink::Foreign;
    }
    match target.strip_prefix(canonical_root) {
        Ok(relative) => StoreLink::Alias(root.join(relative)),
        Err(_) => StoreLink::Foreign,
    }
}

/// A link whose target is gone: dangling when it pointed inside the store.
fn missing_link(lexical: &Path, root: &Path, canonical_root: &Path) -> StoreLink {
    let target = lexical_path(lexical);
    let inside = target.strip_prefix(lexical_path(root)).is_ok()
        || target.strip_prefix(canonical_root).is_ok();
    if inside {
        StoreLink::Dangling
    } else {
        StoreLink::Foreign
    }
}
