use super::Stamp;
use crate::ingest::registry::{self, Adapter, ADAPTERS};
use crate::source_token::SourceToken;
use anyhow::Result;
use rayon::prelude::*;
use std::fs;
use std::path::{Path, PathBuf};

/// Only a root may be a link, which is followed to its kind (see `RootKind::of`).
fn walk(path: &Path, adapter: &dyn Adapter, paths: &mut Vec<PathBuf>, root: bool) -> Result<()> {
    let found = if root {
        registry::followed_root(path, || {
            registry::RootKind::of(adapter.freshness_content(path))
        })
        .map(|(_, metadata)| metadata)
    } else {
        fs::symlink_metadata(path)
    };
    let meta = match found {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    if registry::metadata_is_link(&meta) {
        return Ok(());
    }
    if meta.is_file() {
        if adapter.freshness_content(path) {
            paths.push(path.into())
        }
        return Ok(());
    }
    if meta.is_dir() {
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            let ty = entry.file_type()?;
            let path = entry.path();
            if ty.is_dir() {
                walk(&path, adapter, paths, false)?
            } else if ty.is_file() && adapter.freshness_content(&path) {
                paths.push(path);
            }
        }
    }
    Ok(())
}
fn stamp(a: &str, p: &Path) -> Stamp {
    stamp_meta(a, p, fs::symlink_metadata(p))
}
pub(crate) fn stamp_present(a: &str, p: &Path) -> Option<Stamp> {
    let meta = fs::symlink_metadata(p);
    if meta
        .as_ref()
        .is_err_and(|e| registry::stat_proves_absent(p, e))
    {
        return None;
    }
    Some(stamp_meta(a, p, meta))
}
pub(crate) fn stamp_owned(a: &str, path: PathBuf) -> Option<Stamp> {
    let metadata = fs::symlink_metadata(&path);
    if metadata
        .as_ref()
        .is_err_and(|error| registry::stat_proves_absent(&path, error))
    {
        return None;
    }
    Some(stamp_meta_owned(a, path, metadata))
}
/// Whether an indexed source the census did not stamp is gone: its stat proves it absent, or
/// a directory stands at its path. Sources are files, so an indexed directory is stale: the
/// error of a census that could not list it then, or a source a directory replaced. A link to
/// a directory is one too: a store root the census follows again, or one that is a root no
/// longer, such as a crush data root that could not be read once.
pub(crate) fn gone(path: &Path) -> bool {
    match fs::symlink_metadata(path) {
        Ok(meta) if registry::metadata_is_link(&meta) => {
            fs::metadata(path).is_ok_and(|target| target.is_dir())
        }
        Ok(meta) => meta.is_dir(),
        Err(e) => registry::stat_proves_absent(path, &e),
    }
}
pub(crate) fn stamp_meta(a: &str, p: &Path, meta: std::io::Result<fs::Metadata>) -> Stamp {
    stamp_meta_owned(a, p.to_owned(), meta)
}
fn stamp_meta_owned(a: &str, path: PathBuf, meta: std::io::Result<fs::Metadata>) -> Stamp {
    let p = path.as_path();
    let result = (|| -> Result<(u64, i64, SourceToken)> {
        let m = meta?;
        let (read, m) = if !registry::metadata_is_link(&m) {
            (std::borrow::Cow::Borrowed(p), m)
        } else if registry::is_store_root(p) {
            registry::root_file(p)?
        } else {
            anyhow::bail!("source is a symlink")
        };
        let mut token = SourceToken::File(registry::metadata_change_token(&read, &m)?);
        // A source SQLite WAL is an input too. Reading metadata never opens it writable.
        if p.extension()
            .and_then(|s| s.to_str())
            .is_some_and(|s| matches!(s, "db" | "sqlite" | "sqlite3" | "vscdb"))
        {
            let wal = PathBuf::from(format!("{}-wal", read.to_string_lossy()));
            if let Ok(w) = fs::metadata(&wal) {
                token = SourceToken::Opaque(format!(
                    "{}:{}:{:?}:{:?}",
                    token.diagnostic(),
                    w.len(),
                    w.modified()?,
                    registry::metadata_change_token(&wal, &w)?
                ));
            }
        }
        Ok((
            m.len(),
            crate::intake_batch::epoch_ns(m.modified()?).unwrap_or(0),
            token,
        ))
    })();
    let path = path
        .into_os_string()
        .into_string()
        .unwrap_or_else(|s| s.to_string_lossy().into_owned());
    match result {
        Ok((size, mtime_ns, token)) => Stamp {
            path,
            agent: a.into(),
            size,
            mtime_ns,
            token,
            error: None,
        },
        Err(e) => Stamp {
            path,
            agent: a.into(),
            size: 0,
            mtime_ns: 0,
            token: SourceToken::default(),
            error: Some(e.to_string()),
        },
    }
}
pub fn census() -> Result<Vec<Stamp>> {
    let groups: Vec<Result<Vec<Stamp>>> = ADAPTERS
        .par_iter()
        .map(|a| {
            let mut paths = vec![];
            let mut issues = vec![];
            for root in a.freshness_roots() {
                if let Err(e) = walk(&root, *a, &mut paths, true) {
                    issues.push(Stamp {
                        path: root.to_string_lossy().into(),
                        agent: a.name().into(),
                        size: 0,
                        mtime_ns: 0,
                        token: SourceToken::default(),
                        error: Some(e.to_string()),
                    });
                }
            }
            paths.sort();
            paths.dedup();
            issues.extend(
                paths
                    .par_iter()
                    .map(|p| stamp(a.name(), p))
                    .collect::<Vec<_>>(),
            );
            Ok(issues)
        })
        .collect();
    let mut out = vec![];
    for g in groups {
        out.extend(g?)
    }
    out.sort_by(|a, b| a.path.cmp(&b.path));
    out.dedup_by(|a, b| a.path == b.path);
    Ok(out)
}
