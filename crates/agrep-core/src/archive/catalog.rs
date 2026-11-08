//! The locked catalog of captured versions and their content-addressed gzip chunks.
use super::{hash, valid_hash, Record, CHUNK};
use crate::{
    data::{self, DataRoot},
    safe_fs,
};
use anyhow::{bail, Result};
use flate2::{read::GzDecoder, write::GzEncoder, Compression};
use rusqlite::Connection;
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::PathBuf,
};

pub(super) struct Archive {
    pub db: Connection,
    pub dir: PathBuf,
    _lock: File,
}
impl Archive {
    pub fn open(root: &DataRoot, blocking: bool) -> Result<Option<Self>> {
        if data::read_only() {
            bail!("data directory is read-only")
        }
        let dir = root.path().join("archive");
        data::private_dir(&dir)?;
        data::private_dir(&dir.join("blobs"))?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(dir.join("archive.lock"))?;
        match lock.try_lock() {
            Ok(()) => {}
            Err(std::fs::TryLockError::WouldBlock) if blocking => {
                crate::semantic_profile::event(serde_json::json!({"wait": "archive.lock"}));
                lock.lock()?;
            }
            Err(std::fs::TryLockError::WouldBlock) => return Ok(None),
            Err(e) => return Err(e.into()),
        }
        let db = Connection::open(dir.join("catalog.db"))?;
        let version: u32 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if version != 0 && version != 1 {
            bail!("unsupported archive format: {}", dir.display())
        }
        if version == 0 {
            let tables: i64 = db.query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table'",
                [],
                |r| r.get(0),
            )?;
            if tables != 0 {
                bail!("unsupported archive format: {}", dir.display())
            }
        }
        db.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY,agent TEXT NOT NULL,path TEXT NOT NULL,home_relative TEXT,tail TEXT NOT NULL,size INTEGER NOT NULL,sha TEXT NOT NULL,sqlite INTEGER NOT NULL,created INTEGER NOT NULL,fingerprint TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS versions_path ON versions(path,id DESC);
            CREATE TABLE IF NOT EXISTS chunks(version INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,position INTEGER NOT NULL,sha TEXT NOT NULL,PRIMARY KEY(version,position));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY CHECK(id=1),errors TEXT NOT NULL);
            PRAGMA user_version=1;")?;
        Ok(Some(Self {
            db,
            dir,
            _lock: lock,
        }))
    }
    fn blob(&self, sha: &str) -> Result<PathBuf> {
        if !valid_hash(sha) {
            bail!("invalid archive content hash")
        }
        let p = self.dir.join("blobs").join(format!("{sha}.gz"));
        Ok(p)
    }
    pub fn load(&self, sha: &str) -> Result<Vec<u8>> {
        let mut decoder = GzDecoder::new(File::open(self.blob(sha)?)?).take(CHUNK as u64 + 1);
        let mut bytes = Vec::new();
        decoder.read_to_end(&mut bytes)?;
        if bytes.len() > CHUNK || hash(&bytes) != sha {
            bail!("archive content hash mismatch: {sha}")
        }
        Ok(bytes)
    }
    pub fn store(&self, data: &[u8]) -> Result<(String, u64)> {
        let sha = hash(data);
        let p = self.blob(&sha)?;
        if p.exists() {
            return Ok((sha, 0));
        }
        let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(data)?;
        let compressed = encoder.finish()?;
        safe_fs::write_atomic(&p, &compressed, false)?;
        Ok((sha, compressed.len() as u64))
    }
    pub fn chunks(&self, id: i64) -> Result<Vec<String>> {
        Ok(self
            .db
            .prepare("SELECT sha FROM chunks WHERE version=? ORDER BY position")?
            .query_map([id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?)
    }
    pub fn latest(&self) -> Result<Vec<Record>> {
        let mut stmt = self.db.prepare("SELECT id,agent,path,home_relative,tail,size,sha,sqlite FROM versions WHERE id IN (SELECT max(id) FROM versions GROUP BY path) ORDER BY path")?;
        let records = stmt
            .query_map([], |r| {
                Ok(Record {
                    id: r.get(0)?,
                    agent: r.get(1)?,
                    path: r.get(2)?,
                    home_relative: r.get(3)?,
                    tail: r.get(4)?,
                    size: r.get(5)?,
                    sha: r.get(6)?,
                    sqlite: r.get(7)?,
                })
            })?
            .collect::<rusqlite::Result<_>>()?;
        Ok(records)
    }
    pub fn prune(&mut self, keep: usize) -> Result<(usize, u64)> {
        let tx = self.db.transaction()?;
        let removed = if keep == 0 {
            0
        } else {
            tx.execute("DELETE FROM versions WHERE id IN (SELECT id FROM (SELECT id,row_number() OVER(PARTITION BY path ORDER BY id DESC) n FROM versions) WHERE n>?)", [keep])?
        };
        tx.commit()?;
        Ok((removed, self.gc()?))
    }
    pub fn gc(&self) -> Result<u64> {
        let used: HashSet<String> = self
            .db
            .prepare("SELECT DISTINCT sha FROM chunks")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        let mut bytes = 0;
        for entry in fs::read_dir(self.dir.join("blobs"))? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if let Some(sha) = name.strip_suffix(".gz").filter(|h| valid_hash(h)) {
                if !used.contains(sha) {
                    let p = self.blob(sha)?;
                    bytes += fs::metadata(&p)?.len();
                    safe_fs::remove(&p)?;
                }
            }
        }
        Ok(bytes)
    }
}
