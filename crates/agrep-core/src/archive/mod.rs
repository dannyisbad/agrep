//! Native-source archives: transactional catalog, immutable SHA-256 gzip chunks.
mod capture;
mod catalog;
mod discover;
mod restore;
use crate::data::DataRoot;
use anyhow::{bail, Result};
use catalog::Archive;
pub use discover::discover;
pub use restore::{portable_parts, restore};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const CHUNK: usize = 1024 * 1024;
const INTERVAL_MS: i64 = 10 * 60 * 1000;
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
    /// One line per source captured, appended or failed, in pass order.
    pub log: Vec<String>,
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

pub fn capture(root: &DataRoot, scheduled: bool) -> Result<Stats> {
    let Some(mut archive) = Archive::open(root, !scheduled)? else {
        return Ok(Stats::default());
    };
    let policy = root.config()?.archive;
    if scheduled
        && (!policy.enabled || now() - last_capture(&archive.db)?.unwrap_or(0) < INTERVAL_MS)
    {
        return Ok(Stats::default());
    }
    let (sources, failures) = discover();
    let discovery = failures.len();
    let mut stats = Stats {
        files: sources.len(),
        failures,
        ..Stats::default()
    };
    for source in sources {
        if let Err(e) = capture::capture_one(&mut archive, &source, &mut stats, scheduled) {
            let failure = format!("{}: {e:#}", source.path.display());
            stats.log.push(format!("failed {failure}"));
            stats.failures.push(failure);
        }
    }
    for failure in &stats.failures[..discovery] {
        stats.log.push(format!("failed {failure}"));
    }
    archive.prune(policy.keep)?;
    archive.db.execute(
        "INSERT OR REPLACE INTO meta VALUES('last_capture',?)",
        [now()],
    )?;
    archive.db.execute(
        "INSERT OR REPLACE INTO health VALUES(1,?)",
        [serde_json::to_string(&stats.failures)?],
    )?;
    Ok(stats)
}
/// Invoke once from __work. Disabled archives perform no filesystem work.
pub fn capture_if_due(root: &DataRoot) -> Result<Option<Stats>> {
    if !root.config()?.archive.enabled {
        return Ok(None);
    }
    Ok(Some(capture(root, true)?))
}
fn last_capture(db: &Connection) -> rusqlite::Result<Option<i64>> {
    db.query_row("SELECT value FROM meta WHERE key='last_capture'", [], |r| {
        r.get(0)
    })
    .optional()
}
/// Whether [`capture_if_due`] would capture now, so that commands start no worker for
/// nothing. A catalog this cannot read counts as due: the worker's capture then reports
/// the error where it reports every capture failure, instead of every query failing.
pub fn due(root: &DataRoot) -> Result<bool> {
    if !root.config()?.archive.enabled {
        return Ok(false);
    }
    let path = root.path().join("archive/catalog.db");
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
    Ok(Some(a.prune(root.config()?.archive.keep)?))
}
pub fn purge(root: &DataRoot) -> Result<bool> {
    let Some(mut a) = Archive::open(root, true)? else {
        return Ok(false);
    };
    let tx = a.db.transaction()?;
    tx.execute("DELETE FROM versions", [])?;
    tx.execute("DELETE FROM meta", [])?;
    tx.execute("DELETE FROM health", [])?;
    tx.commit()?;
    a.gc()?;
    Ok(true)
}
pub fn status(root: &DataRoot) -> Result<serde_json::Value> {
    let policy = root.config()?.archive;
    let path = root.path().join("archive/catalog.db");
    if !path.exists() {
        return Ok(
            serde_json::json!({"enabled":policy.enabled,"keep":policy.keep,"files":0,"versions":0,"stored_bytes":0,"source_bytes":0,"raw_bytes":0,"last_capture":null}),
        );
    }
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
    // A capture prunes blobs while this reads them; a blob gone before it is examined is
    // no longer stored.
    let (stored, _) = crate::diagnostics::disk_usage(&root.path().join("archive/blobs"))?;
    let last = last_capture(&db)?;
    let errors: Option<String> = db
        .query_row("SELECT errors FROM health WHERE id=1", [], |r| r.get(0))
        .optional()?;
    let errors: Vec<String> = serde_json::from_str(errors.as_deref().unwrap_or("[]"))?;
    Ok(
        serde_json::json!({"enabled":policy.enabled,"keep":policy.keep,"files":files,"versions":versions,"stored_bytes":stored,"source_bytes":size,"raw_bytes":latest,"last_capture":last,"failures":errors}),
    )
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
}
