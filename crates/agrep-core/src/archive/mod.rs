//! Native-source archives: transactional catalog, immutable SHA-256 gzip chunks.
mod capture;
mod catalog;
mod discover;
mod restore;
use crate::{data::DataRoot, safe_fs};
use anyhow::{bail, Result};
use catalog::Archive;
pub use catalog::{CatalogDamaged, ChunkDamaged, PURGE};
pub use discover::discover;
pub use restore::{portable_parts, restore};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeSet, HashSet},
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const CHUNK: usize = 1024 * 1024;
const INTERVAL_MS: i64 = 10 * 60 * 1000;
/// The longest a failing scheduled capture waits before it is tried again.
const RETRY_CAP_MS: i64 = 24 * 60 * 60 * 1000;
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Policy {
    pub enabled: bool,
    pub keep: usize,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            enabled: false,
            keep: 3,
        }
    }
}
#[derive(Debug, Default, Serialize)]
pub struct Stats {
    pub files: usize,
    pub unchanged: usize,
    pub appended: usize,
    pub full: usize,
    pub bytes_stored: u64,
    pub failures: Vec<String>,
    /// Damaged chunks re-stored from live files holding their content.
    pub repaired: usize,
    /// Versions that still need a missing or corrupt chunk, and their files' paths.
    pub damaged_versions: usize,
    pub damaged_paths: Vec<String>,
}
#[derive(Debug, Clone)]
pub struct Source {
    pub agent: String,
    pub path: PathBuf,
    pub root: PathBuf,
}
#[derive(Debug, Clone)]
struct Record {
    id: i64,
    agent: String,
    path: String,
    home_relative: Option<String>,
    tail: String,
    size: u64,
    sha: String,
    sqlite: bool,
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}
fn hash(bytes: &[u8]) -> String {
    crate::store::hex_digest(Sha256::digest(bytes))
}
fn valid_hash(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn copy_database(backup: &rusqlite::backup::Backup<'_, '_>, blocking: bool) -> Result<()> {
    let mut waiting = false;
    loop {
        match backup.step(-1)? {
            rusqlite::backup::StepResult::Done => return Ok(()),
            rusqlite::backup::StepResult::More => continue,
            // SQLite file locks give no wake-up on release, so another process's lock is polled.
            _ if blocking => {
                if !std::mem::replace(&mut waiting, true) {
                    crate::semantic_profile::event(serde_json::json!({"wait": "sqlite"}));
                }
                std::thread::sleep(Duration::from_millis(10))
            }
            _ => bail!("could not snapshot SQLite source"),
        }
    }
}
fn catalog_path(root: &DataRoot) -> PathBuf {
    root.path().join("archive/catalog.db")
}

/// One capture pass; `log` receives a line per source captured, appended, repaired or
/// failed, as it happens.
pub fn capture(root: &DataRoot, scheduled: bool, log: &mut dyn FnMut(&str)) -> Result<Stats> {
    let pass =
        capture_pass(root, scheduled, log).map_err(|e| catalog::classify(&catalog_path(root), e));
    match &pass {
        Ok(Some(_)) => safe_fs::remove(&failure_marker(root))?,
        Ok(None) => {}
        // The pass's own error is the one to report; without the marker the next worker
        // only retries sooner.
        Err(_) => {
            let _ = record_failure(root);
        }
    }
    Ok(pass?.unwrap_or_default())
}
fn capture_pass(
    root: &DataRoot,
    scheduled: bool,
    log: &mut dyn FnMut(&str),
) -> Result<Option<Stats>> {
    let Some(mut archive) = Archive::open(root, !scheduled)? else {
        return Ok(None);
    };
    let policy = root.config()?.archive;
    if scheduled
        && (!policy.enabled || now() - last_capture(&archive.db)?.unwrap_or(0) < INTERVAL_MS)
    {
        return Ok(None);
    }
    archive.verify()?;
    let (sources, failures) = discover();
    let discovery = failures.len();
    let mut stats = Stats {
        files: sources.len(),
        failures,
        ..Stats::default()
    };
    for source in sources {
        if let Err(e) = capture::capture_one(&mut archive, &source, &mut stats, scheduled, log) {
            let failure = format!("{}: {e:#}", source.path.display());
            log(&format!("failed {failure}"));
            stats.failures.push(failure);
        }
    }
    for failure in &stats.failures[..discovery] {
        log(&format!("failed {failure}"));
    }
    archive.record_written()?;
    archive.prune(policy.keep)?;
    archive.record_damage()?;
    stats.repaired = archive.repaired;
    (stats.damaged_versions, stats.damaged_paths) = damage(&archive.db, &archive.damaged)?;
    archive.db.execute(
        "INSERT OR REPLACE INTO meta VALUES('last_capture',?)",
        [now()],
    )?;
    archive.db.execute(
        "INSERT OR REPLACE INTO health VALUES(1,?)",
        [serde_json::to_string(&stats.failures)?],
    )?;
    Ok(Some(stats))
}
/// The versions needing a chunk in `damaged`, and their files' paths in order.
fn damage(db: &Connection, damaged: &HashSet<String>) -> Result<(usize, Vec<String>)> {
    if damaged.is_empty() {
        return Ok((0, Vec::new()));
    }
    let mut versions = HashSet::new();
    let mut paths = BTreeSet::new();
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
            versions.insert(version);
            paths.insert(path);
        }
    }
    Ok((versions.len(), paths.into_iter().collect()))
}
/// Invoke once from __work. Disabled archives perform no filesystem work.
pub fn capture_if_due(root: &DataRoot) -> Result<Option<Stats>> {
    if !root.config()?.archive.enabled || backing_off(root) {
        return Ok(None);
    }
    Ok(Some(capture(root, true, &mut |_| {})?))
}
fn last_capture(db: &Connection) -> rusqlite::Result<Option<i64>> {
    db.query_row("SELECT value FROM meta WHERE key='last_capture'", [], |r| {
        r.get(0)
    })
    .optional()
}
/// Present while capture passes fail: their count and when the last one failed.
fn failure_marker(root: &DataRoot) -> PathBuf {
    root.path().join("archive/capture.failed")
}
fn failed_passes(root: &DataRoot) -> Option<(u32, i64)> {
    let text = fs::read_to_string(failure_marker(root)).ok()?;
    let (count, at) = text.trim().split_once(' ')?;
    Some((count.parse().ok()?, at.parse().ok()?))
}
/// How many capture passes in a row have failed.
pub fn failing_captures(root: &DataRoot) -> u32 {
    failed_passes(root).map_or(0, |(count, _)| count)
}
/// What is wrong with the archive, each with the action that resolves it: a damaged
/// catalog, versions that cannot be restored, failing passes, unreadable sources.
pub fn problems(root: &DataRoot) -> Result<Vec<(String, String)>> {
    if !root.path().join("archive").is_dir() {
        return Ok(Vec::new());
    }
    let s = catalog_state(root)?.unwrap_or_default();
    if let Some(damaged) = s["catalog_damaged"].as_str() {
        return Ok(vec![(damaged.to_owned(), PURGE.to_owned())]);
    }
    let mut problems = Vec::new();
    let versions = s["damaged_versions"].as_u64().unwrap_or(0);
    if versions > 0 {
        problems.push((
            format!("{versions} archived version(s) of {} file(s) cannot be restored: their stored chunks are missing or corrupt", s["damaged_files"]),
            "`agrep archive` re-stores those that live files still hold".to_owned(),
        ));
    }
    let failing = failing_captures(root);
    if failing > 0 {
        problems.push((
            format!("the last {failing} capture pass(es) failed"),
            "`agrep archive` runs one and shows the error".to_owned(),
        ));
    }
    let failed = s["failures"].as_array().map_or(0, Vec::len);
    if failed > 0 {
        problems.push((
            format!("{failed} source path(s) failed in the last capture"),
            "`agrep archive` retries them and lists those that still fail".to_owned(),
        ));
    }
    Ok(problems)
}
fn record_failure(root: &DataRoot) -> Result<()> {
    let marker = failure_marker(root);
    if crate::data::read_only() || !marker.parent().is_some_and(Path::is_dir) {
        return Ok(());
    }
    let count = failed_passes(root).map_or(0, |(count, _)| count) + 1;
    safe_fs::write_atomic(&marker, format!("{count} {}", now()).as_bytes(), true)
}
/// Whether a scheduled capture waits after failing: ten minutes, doubling with each
/// consecutive failure up to a day, so a lasting fault is not retried by every command.
fn backing_off(root: &DataRoot) -> bool {
    failed_passes(root).is_some_and(|(count, at)| {
        let wait = (INTERVAL_MS << count.saturating_sub(1).min(16)).min(RETRY_CAP_MS);
        now() - at < wait
    })
}
/// Whether [`capture_if_due`] would capture now, so that commands start no worker for
/// nothing. A catalog this cannot read counts as due, unless a failed pass is backing off:
/// the worker's capture then reports the error where it reports every capture failure.
pub fn due(root: &DataRoot) -> Result<bool> {
    if !root.config()?.archive.enabled || backing_off(root) {
        return Ok(false);
    }
    let path = catalog_path(root);
    if !path.exists() {
        return Ok(true);
    }
    let last = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .and_then(|db| last_capture(&db));
    Ok(match last {
        Ok(last) => now() - last.unwrap_or(0) >= INTERVAL_MS,
        Err(_) => true,
    })
}
pub fn prune(root: &DataRoot) -> Result<Option<(usize, u64)>> {
    let Some(mut a) = Archive::open(root, true)? else {
        return Ok(None);
    };
    let pruned = a.prune(root.config()?.archive.keep);
    Ok(Some(
        pruned.map_err(|e| catalog::classify(&catalog_path(root), e))?,
    ))
}
/// Deletes every captured version and stored chunk; a damaged catalog is replaced.
pub fn purge(root: &DataRoot) -> Result<bool> {
    let Some(mut a) = Archive::open_for_purge(root)? else {
        return Ok(false);
    };
    let tx = a.db.transaction()?;
    for table in ["versions", "meta", "health", "damaged"] {
        tx.execute(&format!("DELETE FROM {table}"), [])?;
    }
    tx.commit()?;
    a.gc()?;
    safe_fs::remove(&failure_marker(root))?;
    Ok(true)
}
pub fn status(root: &DataRoot) -> Result<serde_json::Value> {
    let policy = root.config()?.archive;
    let Some(mut s) = catalog_state(root)? else {
        return Ok(
            serde_json::json!({"enabled":policy.enabled,"keep":policy.keep,"files":0,"versions":0,"stored_bytes":0,"source_bytes":0,"raw_bytes":0,"last_capture":null}),
        );
    };
    // A capture prunes blobs while this reads them; a blob gone before it is examined is
    // no longer stored.
    let (stored, _) = crate::diagnostics::disk_usage(&root.path().join("archive/blobs"))?;
    s["enabled"] = policy.enabled.into();
    s["keep"] = policy.keep.into();
    s["stored_bytes"] = stored.into();
    Ok(s)
}
/// The catalog's counts, health and damage, or `catalog_damaged` when it is unreadable;
/// `None` before the first capture.
fn catalog_state(root: &DataRoot) -> Result<Option<serde_json::Value>> {
    let path = catalog_path(root);
    if !path.exists() {
        return Ok(None);
    }
    match catalog_status(&path, &root.path().join("archive/blobs")) {
        Ok(s) => Ok(Some(s)),
        Err(e) => match catalog::classify(&path, e).downcast::<CatalogDamaged>() {
            Ok(damaged) => Ok(Some(
                serde_json::json!({"catalog_damaged":damaged.detail()}),
            )),
            Err(e) => Err(e),
        },
    }
}
fn catalog_status(path: &Path, blobs: &Path) -> Result<serde_json::Value> {
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let (versions, files, size): (u64, u64, u64) = db.query_row(
        "SELECT count(*),count(DISTINCT path),coalesce(sum(size),0) FROM versions",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let latest: u64 = db.query_row(
        "SELECT coalesce(sum(size),0) FROM versions WHERE id IN (SELECT max(id) FROM versions GROUP BY path)",
        [],
        |r| r.get(0),
    )?;
    let last = last_capture(&db)?;
    let errors: Option<String> = db
        .query_row("SELECT errors FROM health WHERE id=1", [], |r| r.get(0))
        .optional()?;
    let errors: Vec<String> = serde_json::from_str(errors.as_deref().unwrap_or("[]"))?;
    let (damaged_versions, damaged_paths) = damage(&db, &damaged_chunks(&db, blobs)?)?;
    Ok(
        serde_json::json!({"files":files,"versions":versions,"source_bytes":size,"raw_bytes":latest,"last_capture":last,"failures":errors,"damaged_versions":damaged_versions,"damaged_files":damaged_paths.len()}),
    )
}
/// The chunks the last capture found damaged and those missing now. The catalog is read
/// before the blob directory: a capture writes a chunk before the version needing it, and
/// prunes a version before its chunks, so a chunk missing from the listing is missing
/// unless a prune committed meanwhile, which the second read excludes.
fn damaged_chunks(db: &Connection, blobs: &Path) -> Result<HashSet<String>> {
    let referenced = |db: &Connection| -> Result<HashSet<String>> {
        Ok(db
            .prepare("SELECT DISTINCT sha FROM chunks")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?)
    };
    let before = referenced(db)?;
    let mut present = HashSet::new();
    match fs::read_dir(blobs) {
        Ok(entries) => {
            for entry in entries {
                let name = entry?.file_name().to_string_lossy().into_owned();
                if let Some(sha) = name.strip_suffix(".gz") {
                    present.insert(sha.to_owned());
                }
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
    let mut damaged: HashSet<String> = before
        .into_iter()
        .filter(|sha| !present.contains(sha))
        .collect();
    if !damaged.is_empty() {
        let now = referenced(db)?;
        damaged.retain(|sha| now.contains(sha));
    }
    let recorded: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='damaged')",
        [],
        |r| r.get(0),
    )?;
    if recorded {
        let mut stmt = db.prepare("SELECT sha FROM damaged")?;
        for sha in stmt.query_map([], |r| r.get::<_, String>(0))? {
            damaged.insert(sha?);
        }
    }
    Ok(damaged)
}

#[cfg(test)]
mod due_tests {
    use super::*;
    #[test]
    fn a_capture_is_due_once_the_archive_is_on_and_the_interval_has_passed() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().join("data"), true).unwrap();
        assert!(!due(&root).unwrap());
        let mut config = root.config().unwrap();
        config.archive.enabled = true;
        root.save_config(&config).unwrap();
        assert!(due(&root).unwrap());
        let archive = Archive::open(&root, true).unwrap().unwrap();
        assert!(due(&root).unwrap());
        let captured = |at: i64| {
            archive
                .db
                .execute("INSERT OR REPLACE INTO meta VALUES('last_capture',?)", [at])
                .unwrap();
        };
        captured(now());
        assert!(!due(&root).unwrap());
        captured(now() - INTERVAL_MS);
        assert!(due(&root).unwrap());
        config.archive.enabled = false;
        root.save_config(&config).unwrap();
        assert!(!due(&root).unwrap());
    }
    #[test]
    fn failed_captures_wait_longer_after_each_consecutive_failure() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().join("data"), true).unwrap();
        let mut config = root.config().unwrap();
        config.archive.enabled = true;
        root.save_config(&config).unwrap();
        drop(Archive::open(&root, true).unwrap().unwrap());
        let failed = |count: u32, minutes_ago: i64| {
            fs::write(
                failure_marker(&root),
                format!("{count} {}", now() - minutes_ago * 60_000),
            )
            .unwrap();
        };
        failed(1, 9);
        assert!(!due(&root).unwrap());
        failed(1, 11);
        assert!(due(&root).unwrap());
        failed(3, 39);
        assert!(!due(&root).unwrap());
        failed(3, 41);
        assert!(due(&root).unwrap());
        failed(40, 23 * 60);
        assert!(!due(&root).unwrap());
        failed(40, 25 * 60);
        assert!(due(&root).unwrap());
        record_failure(&root).unwrap();
        assert_eq!(failed_passes(&root).unwrap().0, 41);
        assert!(!due(&root).unwrap());
    }
}
