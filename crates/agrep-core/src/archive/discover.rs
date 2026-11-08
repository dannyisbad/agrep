//! Finding every archivable source file under the adapters' archive roots.
use super::Source;
use crate::ingest::registry::{path_eq, relative_path, Adapter, ADAPTERS};
use anyhow::Result;
use std::{fs, path::Path};

fn walk(
    adapter: &dyn Adapter,
    root: &Path,
    path: &Path,
    out: &mut Vec<Source>,
    failures: &mut Vec<String>,
) {
    let result = (|| -> Result<()> {
        let meta = match fs::symlink_metadata(path) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.into()),
        };
        if meta.file_type().is_symlink() {
            return Ok(());
        }
        if meta.is_dir() {
            for entry in fs::read_dir(path)? {
                let entry = entry?;
                let ty = entry.file_type()?;
                if ty.is_dir() || ty.is_file() {
                    let rel = relative_path(&entry.path(), root).unwrap_or_else(|| entry.path());
                    if adapter.name() == "opencode"
                        && ty.is_dir()
                        && rel.components().count() == 1
                        && rel != Path::new("storage")
                    {
                        continue;
                    }
                    if adapter.name() == "cline" && entry.file_name() == "checkpoints" {
                        continue;
                    }
                    walk(adapter, root, &entry.path(), out, failures);
                }
            }
        } else if meta.is_file() && archive_content(adapter, root, path) {
            out.push(Source {
                agent: adapter.name().into(),
                path: path.to_path_buf(),
                root: root.to_path_buf(),
            });
        }
        Ok(())
    })();
    if let Err(e) = result {
        failures.push(format!("{}: {e:#}", path.display()));
    }
}
fn archive_content(adapter: &dyn Adapter, root: &Path, path: &Path) -> bool {
    let rel = relative_path(path, root).unwrap_or_else(|| path.to_path_buf());
    if adapter.name() == "opencode" && path.extension().is_some_and(|x| x == "json") {
        return rel
            .components()
            .next()
            .is_some_and(|x| x.as_os_str() == "storage");
    }
    if adapter.name() == "cline" {
        return path.extension().is_some_and(|x| x == "json")
            && (rel
                .components()
                .next()
                .is_some_and(|x| x.as_os_str() == "tasks")
                || rel.file_name().is_some_and(|x| x == "taskHistory.json"));
    }
    adapter.archive_content(path)
}
pub fn discover() -> (Vec<Source>, Vec<String>) {
    let mut out = Vec::new();
    let mut failures = Vec::new();
    for adapter in ADAPTERS {
        for root in adapter.archive_roots() {
            walk(*adapter, &root, &root, &mut out, &mut failures);
        }
    }
    #[cfg(windows)]
    out.sort_by_cached_key(|s| crate::ingest::registry::logical_path_key(&s.path));
    #[cfg(not(windows))]
    out.sort_by(|a, b| a.path.cmp(&b.path));
    out.dedup_by(|a, b| path_eq(&a.path, &b.path));
    (out, failures)
}
