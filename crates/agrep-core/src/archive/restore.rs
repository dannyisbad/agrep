//! Restoring archived versions to their stores or to a chosen directory.
use super::{catalog::Archive, copy_database, Record};
use crate::{
    data::DataRoot,
    ingest::registry::{path_eq, relative_path, ADAPTERS},
    safe_fs,
};
use anyhow::{bail, Context, Result};
use rusqlite::{Connection, OpenFlags};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs,
    io::Write,
    path::{Component, Path, PathBuf},
    time::Duration,
};

/// Parse absolute paths written on any supported OS without trusting local Path semantics.
pub fn portable_parts(path: &str, absolute: bool) -> Result<Vec<String>> {
    let normalized = path.replace('\\', "/");
    let (is_absolute, body) = if normalized.starts_with('/') {
        (true, normalized.trim_start_matches('/'))
    } else if normalized.as_bytes().get(1) == Some(&b':')
        && normalized.as_bytes().get(2) == Some(&b'/')
    {
        (true, &normalized[3..])
    } else {
        (false, normalized.as_str())
    };
    if is_absolute != absolute {
        bail!("invalid archive path")
    }
    let mut out = Vec::new();
    for p in body.split('/') {
        if p.is_empty()
            || p == "."
            || p == ".."
            || p.contains([':', '\0'])
            || p.ends_with([' ', '.'])
        {
            bail!("path traversal or invalid archive path")
        }
        let stem = p.split('.').next().unwrap_or_default().to_ascii_uppercase();
        if matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.as_bytes()[3].is_ascii_digit())
        {
            bail!("reserved archive path")
        }
        out.push(p.into());
    }
    if out.is_empty() {
        bail!("empty archive path")
    };
    Ok(out)
}
fn relative(path: &str) -> Result<PathBuf> {
    Ok(portable_parts(path, false)?.iter().collect())
}
fn destination(record: &Record, to: Option<&Path>) -> Result<PathBuf> {
    portable_parts(&record.path, true)?;
    let rel = relative(record.home_relative.as_deref().unwrap_or(&record.tail))?;
    if let Some(to) = to {
        return Ok(to.join(rel));
    }
    let current = if record.home_relative.is_some() {
        crate::ingest::home().join(&rel)
    } else {
        PathBuf::from(&record.path)
    };
    let valid = ADAPTERS
        .iter()
        .filter(|a| a.name() == record.agent)
        .any(|a| {
            a.archive_content(&current)
                && a.archive_roots().iter().any(|r| {
                    path_eq(&current, r)
                        || relative_path(&current, r).is_some_and(|p| {
                            p.components().all(|c| matches!(c, Component::Normal(_)))
                        })
                })
        });
    if !valid {
        bail!("archive path is outside documented store roots")
    }
    Ok(current)
}
/// The version's content, verified against its recorded size and hash, in a temporary
/// file beside its destination.
fn verified_copy(a: &Archive, r: &Record, parent: &Path) -> Result<tempfile::NamedTempFile> {
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    let mut digest = Sha256::new();
    let mut size = 0u64;
    for sha in a.chunks(r.id)? {
        let bytes = a.load(&sha)?;
        digest.update(&bytes);
        size += bytes.len() as u64;
        temp.write_all(&bytes)?;
    }
    if size != r.size || crate::store::hex_digest(digest.finalize()) != r.sha {
        bail!("archive aggregate hash mismatch")
    }
    temp.as_file().sync_all()?;
    Ok(temp)
}
fn sidecar(dst: &Path, suffix: &str) -> PathBuf {
    PathBuf::from(format!("{}{suffix}", dst.display()))
}
fn restore_one(a: &Archive, r: &Record, dst: &Path, force: bool) -> Result<()> {
    safe_fs::plain_path(dst)?;
    let before = fs::metadata(dst).ok().as_ref().map(safe_fs::identity);
    if fs::metadata(dst).is_ok_and(|m| m.permissions().readonly()) {
        bail!("read-only file: {}", dst.display());
    }
    if dst.exists() && !force {
        bail!(
            "exists, not overwriting (--force to replace): {}",
            dst.display()
        )
    }
    let parent = dst.parent().context("destination has no parent")?;
    fs::create_dir_all(parent)?;
    safe_fs::plain_path(parent)?;
    let temp = verified_copy(a, r, parent)?;
    // SQLite backup waits for the target and preserves open readers and WAL coherency.
    if r.sqlite && dst.exists() {
        for suffix in ["-wal", "-shm", "-journal"] {
            safe_fs::plain_path(&sidecar(dst, suffix))?;
        }
        let source = Connection::open_with_flags(temp.path(), OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let mut target = Connection::open(dst)?;
        target.busy_timeout(Duration::ZERO)?;
        let backup = rusqlite::backup::Backup::new(&source, &mut target)?;
        copy_database(&backup, true)?;
    } else {
        safe_fs::plain_path(dst)?;
        if fs::metadata(dst).ok().as_ref().map(safe_fs::identity) != before {
            bail!("destination changed during restore");
        }
        if r.sqlite {
            for suffix in ["-wal", "-shm", "-journal"] {
                let sidecar = sidecar(dst, suffix);
                safe_fs::plain_path(&sidecar)?;
                if sidecar.exists() {
                    bail!(
                        "SQLite sidecar exists without its database: {}",
                        sidecar.display()
                    );
                }
            }
        }
        if force {
            temp.persist(dst).map_err(|e| e.error)?;
        } else {
            temp.persist_noclobber(dst).map_err(|e| e.error)?;
        }
    }
    #[cfg(unix)]
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}
/// Whether every matched path holds the session id the needle starts, so the match is
/// one session's files rather than an ambiguous one.
fn shared_session(records: &[Record], needle_lower: &str) -> Result<bool> {
    let session_pattern =
        regex::Regex::new(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")?;
    Ok(records.first().is_some_and(|first| {
        session_pattern.find_iter(&first.path).any(|m| {
            m.as_str().to_lowercase().starts_with(needle_lower)
                && records
                    .iter()
                    .all(|r| r.path.to_lowercase().contains(&m.as_str().to_lowercase()))
        })
    }))
}
pub fn restore(
    root: &DataRoot,
    needle: &str,
    to: Option<&Path>,
    force: bool,
) -> Result<(usize, Vec<String>)> {
    if needle.trim().is_empty() {
        bail!("needle must not be blank")
    }
    let Some(a) = Archive::open(root, true)? else {
        unreachable!("blocking archive lock")
    };
    let needle_lower = needle.to_lowercase().replace('\\', "/");
    let records: Vec<_> = a
        .latest()?
        .into_iter()
        .filter(|r| {
            r.path
                .to_lowercase()
                .replace('\\', "/")
                .contains(&needle_lower)
        })
        .collect();
    if records.is_empty() {
        return Ok((0, vec![format!("no archived file matches '{needle}'")]));
    }
    let shared_session = shared_session(&records, &needle_lower)?;
    if records.len() > 1 && to.is_none() && !shared_session {
        return Ok((0,vec![format!("{} archived files match {needle:?}; narrow the match, or --to a directory to restore all of them",records.len())]));
    }
    let mut plan = Vec::new();
    let mut destinations = HashSet::new();
    for r in records {
        let dst = destination(&r, to)?;
        if !destinations.insert(dst.to_string_lossy().to_lowercase()) {
            bail!("restore destination collision")
        };
        safe_fs::plain_path(&dst)?;
        plan.push((r, dst));
    }
    let mut count = 0;
    let mut errors = Vec::new();
    for (r, dst) in plan {
        match restore_one(&a, &r, &dst, force) {
            Ok(()) => {
                println!(
                    "restored {} ({} bytes, sha256 verified: {})",
                    crate::render::safe(&dst.display().to_string()),
                    r.size,
                    r.sha
                );
                count += 1;
            }
            Err(e) if e.to_string().starts_with("exists, not overwriting") => {
                errors.push(e.to_string())
            }
            Err(e) => errors.push(format!("cannot restore {}: {e:#}", dst.display())),
        }
    }
    Ok((count, errors))
}
