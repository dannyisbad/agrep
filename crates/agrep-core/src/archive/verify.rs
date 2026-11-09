//! The deep check: every chunk the catalog references is decompressed and re-hashed, and
//! each damaged version is classed by whether a live file still holds its content.
use super::{
    capture::{fingerprint, is_sqlite, next_chunk, open_source, sqlite_snapshot},
    catalog::{load_chunk, Archive, ChunkDamaged},
    catalog_path, discover, hash, ChunkProgress,
};
use crate::data::{self, DataRoot};
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet},
    fs::File,
    io::Seek,
    path::Path,
    sync::atomic::{AtomicUsize, Ordering},
};

/// Versions with a missing or corrupt chunk, and their files' paths in order.
#[derive(Debug, Default)]
pub struct Damaged {
    pub versions: usize,
    pub paths: Vec<String>,
}
/// The versions the deep check found damaged.
#[derive(Debug, Default)]
pub struct Verified {
    /// Live files still hold their content, which the next capture re-stores.
    pub restorable: Damaged,
    /// No live file holds their content any more: they cannot be restored.
    pub lost: Damaged,
}

/// Finds the damaged chunks without the archive lock, so that captures are not held up
/// while every chunk is read. A capture may prune or re-store chunks meanwhile, so the
/// findings are confirmed under the lock and recorded: status then reports them, and the
/// next capture checks them again though their files look unchanged, and re-stores them.
pub(super) fn verify(root: &DataRoot, progress: &ChunkProgress<'_>) -> Result<Verified> {
    let catalog = catalog_path(root);
    let blobs = root.path().join("archive").join("blobs");
    let open = || Connection::open_with_flags(&catalog, OpenFlags::SQLITE_OPEN_READ_ONLY);
    let chunks = referenced(&open()?)?;
    let total = chunks.len();
    let checked = AtomicUsize::new(0);
    let suspects = chunks
        .into_par_iter()
        .map(|sha| {
            let damaged = chunk_damaged(&blobs, &sha)?;
            progress(checked.fetch_add(1, Ordering::Relaxed) + 1, total);
            Ok(damaged.then_some(sha))
        })
        .filter_map(Result::transpose)
        .collect::<Result<Vec<String>>>()?;
    if suspects.is_empty() {
        return Ok(Verified::default());
    }
    let mut archive = if data::read_only() {
        None
    } else {
        Archive::open(root, true)?
    };
    let read_only;
    let db = match &archive {
        Some(archive) => &archive.db,
        None => {
            read_only = open()?;
            &read_only
        }
    };
    let now = referenced(db)?;
    let mut damaged = HashSet::new();
    for sha in suspects {
        if now.contains(&sha) && chunk_damaged(&blobs, &sha)? {
            damaged.insert(sha);
        }
    }
    let verified = classify(db, &damaged)?;
    if let Some(archive) = &mut archive {
        archive.record_found_damage(&damaged)?;
    }
    Ok(verified)
}
fn referenced(db: &Connection) -> Result<HashSet<String>> {
    Ok(db
        .prepare("SELECT DISTINCT sha FROM chunks")?
        .query_map([], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?)
}
fn chunk_damaged(blobs: &Path, sha: &str) -> Result<bool> {
    match load_chunk(blobs, sha) {
        Ok(_) => Ok(false),
        Err(e) if e.is::<ChunkDamaged>() => Ok(true),
        Err(e) => Err(e),
    }
}

/// The versions needing a `damaged` chunk, by whether live files hold all of it.
fn classify(db: &Connection, damaged: &HashSet<String>) -> Result<Verified> {
    if damaged.is_empty() {
        return Ok(Verified::default());
    }
    // version -> (path, its damaged chunks)
    let mut versions: BTreeMap<i64, (String, Vec<String>)> = BTreeMap::new();
    let mut stmt = db
        .prepare("SELECT c.version,v.path,c.sha FROM chunks c JOIN versions v ON v.id=c.version")?;
    let rows = stmt.query_map([], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
        ))
    })?;
    for row in rows {
        let (version, path, sha) = row?;
        if damaged.contains(&sha) {
            versions
                .entry(version)
                .or_insert((path, Vec::new()))
                .1
                .push(sha);
        }
    }
    let held = held_by_live_files(db, &versions, damaged)?;
    let mut restorable = (0, BTreeSet::new());
    let mut lost = (0, BTreeSet::new());
    for (path, shas) in versions.values() {
        let class = if shas.iter().all(|sha| held.contains(sha)) {
            &mut restorable
        } else {
            &mut lost
        };
        class.0 += 1;
        class.1.insert(path.clone());
    }
    let damaged = |(versions, paths): (usize, BTreeSet<String>)| Damaged {
        versions,
        paths: paths.into_iter().collect(),
    };
    Ok(Verified {
        restorable: damaged(restorable),
        lost: damaged(lost),
    })
}
/// The `damaged` chunks that live files hold and the next capture reads, so re-stores. Like
/// a capture it reads a source that is new, changed since its latest version, or whose
/// latest version is damaged: a copy of a file, such as a forked chat, may hold the content.
fn held_by_live_files(
    db: &Connection,
    versions: &BTreeMap<i64, (String, Vec<String>)>,
    damaged: &HashSet<String>,
) -> Result<HashSet<String>> {
    let mut latest: HashMap<String, (i64, String)> = HashMap::new();
    let mut stmt = db.prepare("SELECT path,id,fingerprint FROM versions WHERE id IN (SELECT max(id) FROM versions GROUP BY path)")?;
    for row in stmt.query_map([], |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?))))? {
        let (path, version) = row?;
        latest.insert(path, version);
    }
    let (sources, _) = discover();
    let mut held = HashSet::new();
    for source in sources {
        let previous = latest.get(source.path.to_string_lossy().as_ref());
        let read = |fingerprint: &str| {
            previous.is_none_or(|(id, f)| f != fingerprint || versions.contains_key(id))
        };
        // An unreadable file holds nothing a capture could re-store.
        if let Ok(hashes) = live_chunk_hashes(&source.path, read) {
            held.extend(hashes.into_iter().filter(|sha| damaged.contains(sha)));
        }
    }
    Ok(held)
}
/// The hashes of the chunks a capture of the file at `path` would store now, if `read`
/// accepts the file's fingerprint.
fn live_chunk_hashes(path: &Path, read: impl Fn(&str) -> bool) -> Result<Vec<String>> {
    let source = crate::ingest::registry::source_path(path);
    let mut file = open_source(&source)?;
    let sqlite = is_sqlite(&mut file)?;
    if !read(&fingerprint(&file, &source, sqlite)?) {
        return Ok(Vec::new());
    }
    let snapshot = if sqlite {
        Some(sqlite_snapshot(&source, &std::env::temp_dir(), true)?)
    } else {
        None
    };
    let mut file = match &snapshot {
        Some(snapshot) => File::open(snapshot.path())?,
        None => {
            file.rewind()?;
            file
        }
    };
    let mut hashes = Vec::new();
    loop {
        let chunk = next_chunk(&mut file)?;
        if chunk.is_empty() {
            return Ok(hashes);
        }
        hashes.push(hash(&chunk));
    }
}
