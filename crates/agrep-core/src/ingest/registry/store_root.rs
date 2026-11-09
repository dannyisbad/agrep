//! Store roots that are themselves symbolic links. Dotfiles setups move `~/.codex/sessions`,
//! `~/.claude/projects` or a database such as opencode's elsewhere and link them back, so a
//! root is followed once; links met inside a store are still never followed.

use std::borrow::Cow;
use std::cell::Cell;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use super::{metadata_is_link, path_eq, ADAPTERS};

/// What a store root stands for: a directory walked for sources, or one source file (a
/// database or a registry). A root link is followed only to its own kind.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RootKind {
    Directory,
    File,
}

impl RootKind {
    /// The kind of a root whose walker counts it as a source (`content`) when it is a file: a
    /// root that would itself be read is one file, any other a directory.
    pub fn of(content: bool) -> Self {
        if content {
            Self::File
        } else {
            Self::Directory
        }
    }
}

/// `fs::symlink_metadata(root)` for a directory store root, except that a root which is a link
/// to a directory reports that directory. A link that cannot be followed is an error, never
/// NotFound, so the store's absence is disclosed instead of passing as an empty store.
pub fn root_metadata(root: &Path) -> io::Result<fs::Metadata> {
    followed_root(root, || RootKind::Directory).map(|(_, metadata)| metadata)
}

/// [`root_metadata`] for a single-file store root, with the path to read it at: the root
/// itself, or the file its link leads to. No-follow opens and SQLite's `-wal` sidecar name the
/// last component only, so a linked database is opened at its target.
pub fn root_file(root: &Path) -> io::Result<(Cow<'_, Path>, fs::Metadata)> {
    followed_root(root, || RootKind::File)
}

/// [`root_metadata`] or [`root_file`], whichever `kind` names; it is asked only of a link.
pub fn followed_root(
    root: &Path,
    kind: impl FnOnce() -> RootKind,
) -> io::Result<(Cow<'_, Path>, fs::Metadata)> {
    let metadata = fs::symlink_metadata(root)?;
    if !metadata_is_link(&metadata) {
        return Ok((Cow::Borrowed(root), metadata));
    }
    let unreachable = |error| refused(format!("its target cannot be reached ({error})"));
    let target = fs::canonicalize(root).map_err(unreachable)?;
    let followed = fs::metadata(&target).map_err(unreachable)?;
    let kind = kind();
    let wrong = match kind {
        RootKind::Directory => (!followed.is_dir()).then_some("a directory"),
        RootKind::File => (!followed.is_file()).then_some("a regular file"),
    };
    if let Some(wanted) = wrong {
        return Err(refused(format!(
            "it leads to {}, which is not {wanted}",
            target.display()
        )));
    }
    if let Some(other) = overlapping_root(root, kind, &target) {
        return Err(refused(format!(
            "it leads into {}, which agrep already reads",
            other.display()
        )));
    }
    Ok((Cow::Owned(target), followed))
}

/// The path to open for a source: a store root that links to a regular file is read at its
/// target; any other path, a link that may not be followed included, is itself.
pub fn source_path(path: &Path) -> Cow<'_, Path> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata_is_link(&metadata) && is_store_root(path) => {
            root_file(path).map_or(Cow::Borrowed(path), |(target, _)| target)
        }
        _ => Cow::Borrowed(path),
    }
}

/// A store root link agrep will not follow, and why.
#[derive(Debug)]
struct Refused(String);

impl std::fmt::Display for Refused {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "store root is a symbolic link agrep does not follow: {}",
            self.0
        )
    }
}

impl std::error::Error for Refused {}

fn refused(reason: String) -> io::Error {
    io::Error::other(Refused(reason))
}

/// Whether `error` is a store root link refused by [`followed_root`]. The census reports it
/// against the root itself, so a reader that meets it again need not report it once more.
pub fn refused_root_link(error: &io::Error) -> bool {
    error.get_ref().is_some_and(|inner| inner.is::<Refused>())
}

thread_local! {
    static LISTING: Cell<bool> = const { Cell::new(false) };
}

/// Every adapter's freshness and archive roots with their kinds, first listing first. Crush and
/// cursor find theirs by reading their stores through [`root_file`], so a link met while
/// listing is followed without the overlap check, which needs this very list; the outer call
/// makes it. What their discovery reports while listing is dropped: it runs inside other work,
/// which would take it as its own failure, and each store reports it from its own read.
fn store_roots() -> Vec<(PathBuf, RootKind)> {
    struct Listing {
        outer: bool,
        diagnostics: Vec<String>,
    }
    impl Drop for Listing {
        fn drop(&mut self) {
            LISTING.set(self.outer);
            crate::intake::restore_diagnostics(std::mem::take(&mut self.diagnostics));
        }
    }
    let _listing = Listing {
        outer: LISTING.replace(true),
        diagnostics: crate::intake::take_diagnostics(),
    };
    let mut roots: Vec<(PathBuf, RootKind)> = Vec::new();
    for adapter in ADAPTERS {
        for path in adapter
            .freshness_roots()
            .into_iter()
            .chain(adapter.archive_roots())
        {
            if !roots.iter().any(|(seen, _)| path_eq(seen, &path)) {
                let content = adapter.freshness_content(&path) || adapter.archive_content(&path);
                roots.push((path, RootKind::of(content)));
            }
        }
    }
    roots
}

pub fn is_store_root(path: &Path) -> bool {
    store_roots().iter().any(|(root, _)| path_eq(root, path))
}

/// Whether this thread is listing the store roots, when a store's discovery reports nothing.
pub(crate) fn listing_roots() -> bool {
    LISTING.get()
}

/// Another store root whose real location contains, or lies inside, `target`: following the
/// link would read one store twice under two names. A plain root keeps its store; between two
/// linked roots the one listed first does, and a link that leads to the wrong kind keeps none.
fn overlapping_root(root: &Path, kind: RootKind, target: &Path) -> Option<PathBuf> {
    if LISTING.get() {
        return None;
    }
    let roots = store_roots();
    let position = roots.iter().position(|(path, _)| path_eq(path, root));
    roots
        .into_iter()
        .enumerate()
        .find_map(|(index, (other, other_kind))| {
            if path_eq(&other, root) {
                return None;
            }
            // Directory roots nested in one another by name (kimi's home and its sessions) are
            // one store; a file root inside a directory root may still lead back into it.
            if kind == RootKind::Directory && (other.starts_with(root) || root.starts_with(&other))
            {
                return None;
            }
            let linked = metadata_is_link(&fs::symlink_metadata(&other).ok()?);
            if linked && position.is_some_and(|position| index > position) {
                return None;
            }
            let real = if linked {
                root_target(&other, other_kind)?
            } else {
                fs::canonicalize(&other).ok()?
            };
            (real.starts_with(target) || target.starts_with(&real)).then_some(other)
        })
}

fn root_target(link: &Path, kind: RootKind) -> Option<PathBuf> {
    let target = fs::canonicalize(link).ok()?;
    fs::metadata(&target)
        .is_ok_and(|metadata| match kind {
            RootKind::Directory => metadata.is_dir(),
            RootKind::File => metadata.is_file(),
        })
        .then_some(target)
}

#[cfg(test)]
mod tests {
    #[cfg(unix)]
    #[test]
    fn a_linked_root_is_followed_and_a_broken_one_is_an_error_not_an_absence() {
        use super::root_metadata;
        use std::os::unix::fs::symlink;
        let t = tempfile::tempdir().unwrap();
        let real = t.path().join("real");
        std::fs::create_dir(&real).unwrap();
        let link = t.path().join("link");
        symlink(&real, &link).unwrap();
        assert!(root_metadata(&link).unwrap().is_dir());
        let file = t.path().join("file");
        std::fs::write(&file, "").unwrap();
        let to_file = t.path().join("to-file");
        symlink(&file, &to_file).unwrap();
        let dangling = t.path().join("dangling");
        symlink(t.path().join("missing"), &dangling).unwrap();
        for broken in [to_file, dangling] {
            let error = root_metadata(&broken).unwrap_err();
            assert_ne!(error.kind(), std::io::ErrorKind::NotFound);
            assert!(error.to_string().contains("does not follow"), "{error}");
        }
        assert_eq!(
            root_metadata(&t.path().join("absent")).unwrap_err().kind(),
            std::io::ErrorKind::NotFound
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_linked_file_root_is_read_at_its_target_and_one_of_another_kind_refused() {
        use super::root_file;
        use std::os::unix::fs::symlink;
        let t = tempfile::tempdir().unwrap();
        let file = t.path().join("real.db");
        std::fs::write(&file, "").unwrap();
        let link = t.path().join("opencode.db");
        symlink(&file, &link).unwrap();
        let (read, metadata) = root_file(&link).unwrap();
        assert_eq!(read, std::fs::canonicalize(&file).unwrap());
        assert!(metadata.is_file());
        let (read, _) = root_file(&file).unwrap();
        assert_eq!(read, file);
        let to_dir = t.path().join("to-dir");
        symlink(t.path(), &to_dir).unwrap();
        let dangling = t.path().join("dangling");
        symlink(t.path().join("missing"), &dangling).unwrap();
        for (broken, reason) in [
            (to_dir, "which is not a regular file"),
            (dangling, "its target cannot be reached"),
        ] {
            let error = root_file(&broken).unwrap_err();
            assert_ne!(error.kind(), std::io::ErrorKind::NotFound);
            assert!(error.to_string().contains(reason), "{error}");
        }
    }
}
