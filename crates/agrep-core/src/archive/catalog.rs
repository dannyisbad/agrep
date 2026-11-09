//! The locked catalog of captured versions and their content-addressed gzip chunks.
use super::{hash, valid_hash, Record, CHUNK};
use crate::{
    data::{self, DataRoot},
    safe_fs,
};
use anyhow::{bail, Result};
use flate2::{read::GzDecoder, write::GzEncoder, Compression};
use rusqlite::{params, Connection, ErrorCode};
use std::{
    collections::HashSet,
    fmt,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

/// What recovers from a damaged catalog.
pub const PURGE: &str =
    "`agrep archive --purge --yes` deletes the archive and the next capture starts a new one";
/// The catalog is not a readable database. Its versions cannot be listed, so nothing it
/// recorded can be restored or rebuilt; only purging the archive recovers.
#[derive(Debug)]
pub struct CatalogDamaged {
    path: PathBuf,
    cause: String,
}
impl CatalogDamaged {
    /// The damage without its remedy.
    pub fn detail(&self) -> String {
        format!(
            "archive catalog is damaged: {} ({})",
            self.path.display(),
            self.cause
        )
    }
}
impl fmt::Display for CatalogDamaged {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}; {PURGE}", self.detail())
    }
}
impl std::error::Error for CatalogDamaged {}
/// `e` as [`CatalogDamaged`] when SQLite found `path` corrupt or not a database.
pub(super) fn classify(path: &Path, e: anyhow::Error) -> anyhow::Error {
    match e.downcast_ref::<rusqlite::Error>() {
        Some(rusqlite::Error::SqliteFailure(failure, message))
            if matches!(
                failure.code,
                ErrorCode::NotADatabase | ErrorCode::DatabaseCorrupt
            ) =>
        {
            CatalogDamaged {
                path: path.to_path_buf(),
                cause: message.clone().unwrap_or_else(|| failure.to_string()),
            }
            .into()
        }
        _ => e,
    }
}

/// A stored chunk a version needs is missing or does not hold its content.
#[derive(Debug)]
pub struct ChunkDamaged(pub(super) String);
impl fmt::Display for ChunkDamaged {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for ChunkDamaged {}

/// The archive directory, held under archive.lock.
struct Locked {
    dir: PathBuf,
    _lock: File,
}
impl Locked {
    pub fn acquire(root: &DataRoot, blocking: bool) -> Result<Option<Self>> {
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
        Ok(Some(Self { dir, _lock: lock }))
    }
    fn catalog_path(&self) -> PathBuf {
        self.dir.join("catalog.db")
    }
    /// Deletes a damaged catalog with its journal files.
    fn remove_catalog(&self) -> Result<()> {
        for suffix in ["", "-wal", "-shm", "-journal"] {
            safe_fs::remove(&PathBuf::from(format!(
                "{}{suffix}",
                self.catalog_path().display()
            )))?;
        }
        Ok(())
    }
    /// The catalog, or with `replace_damaged` a new empty one in place of a damaged one.
    fn catalog(&self, replace_damaged: bool) -> Result<Connection> {
        match self.open_catalog() {
            Err(e) if replace_damaged && e.is::<CatalogDamaged>() => {
                self.remove_catalog()?;
                self.open_catalog()
            }
            opened => opened,
        }
    }
    fn open_catalog(&self) -> Result<Connection> {
        let path = self.catalog_path();
        let open = || -> Result<Connection> {
            let db = Connection::open(&path)?;
            let version: u32 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
            if version != 0 && version != 1 {
                bail!("unsupported archive format: {}", self.dir.display())
            }
            if version == 0 {
                let tables: i64 = db.query_row(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'",
                    [],
                    |r| r.get(0),
                )?;
                if tables != 0 {
                    bail!("unsupported archive format: {}", self.dir.display())
                }
            }
            db.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY,agent TEXT NOT NULL,path TEXT NOT NULL,home_relative TEXT,tail TEXT NOT NULL,size INTEGER NOT NULL,sha TEXT NOT NULL,sqlite INTEGER NOT NULL,created INTEGER NOT NULL,fingerprint TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS versions_path ON versions(path,id DESC);
            CREATE TABLE IF NOT EXISTS chunks(version INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,position INTEGER NOT NULL,sha TEXT NOT NULL,PRIMARY KEY(version,position));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY CHECK(id=1),errors TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blobs(sha TEXT PRIMARY KEY,identity TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS damaged(sha TEXT PRIMARY KEY);
            PRAGMA user_version=1;")?;
            Ok(db)
        };
        open().map_err(|e| classify(&path, e))
    }
}

pub(super) struct Archive {
    pub db: Connection,
    pub dir: PathBuf,
    /// Referenced chunks found missing or corrupt, until a capture re-stores them.
    pub damaged: HashSet<String>,
    /// Damaged chunks re-stored by this archive's captures.
    pub repaired: usize,
    /// Chunks verified or written since the last [`Archive::record_written`], with their
    /// file identity.
    written: Vec<(String, String)>,
    _lock: Locked,
}
/// What a blob's verification is valid for: the same file, unmodified.
fn blob_identity(meta: &fs::Metadata) -> String {
    format!("{:?}", safe_fs::identity(meta))
}
/// The content of chunk `sha` in `blobs`, or [`ChunkDamaged`] when it is missing or does
/// not hold that content.
pub(super) fn load_chunk(blobs: &Path, sha: &str) -> Result<Vec<u8>> {
    if !valid_hash(sha) {
        bail!("invalid archive content hash")
    }
    let short = &sha[..12];
    let file = match File::open(blobs.join(format!("{sha}.gz"))) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(
                ChunkDamaged(format!("chunk {short} is missing from {}", blobs.display())).into(),
            )
        }
        Err(e) => return Err(e.into()),
    };
    let corrupt = |cause: &dyn fmt::Display| {
        ChunkDamaged(format!(
            "chunk {short} in {} is corrupt ({cause})",
            blobs.display()
        ))
    };
    let mut decoder = GzDecoder::new(file).take(CHUNK as u64 + 1);
    let mut bytes = Vec::new();
    decoder.read_to_end(&mut bytes).map_err(|e| corrupt(&e))?;
    if bytes.len() > CHUNK || hash(&bytes) != sha {
        return Err(corrupt(&"content hash mismatch").into());
    }
    Ok(bytes)
}
impl Archive {
    pub fn open(root: &DataRoot, blocking: bool) -> Result<Option<Self>> {
        Self::open_with(root, blocking, false)
    }
    /// Opens the archive to purge it, putting an empty catalog in place of a damaged one.
    pub fn open_for_purge(root: &DataRoot) -> Result<Option<Self>> {
        Self::open_with(root, true, true)
    }
    fn open_with(root: &DataRoot, blocking: bool, replace_damaged: bool) -> Result<Option<Self>> {
        let Some(lock) = Locked::acquire(root, blocking)? else {
            return Ok(None);
        };
        Ok(Some(Self {
            db: lock.catalog(replace_damaged)?,
            dir: lock.dir.clone(),
            damaged: HashSet::new(),
            repaired: 0,
            written: Vec::new(),
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
        load_chunk(&self.dir.join("blobs"), sha)
    }
    pub fn store(&mut self, data: &[u8]) -> Result<(String, u64)> {
        let sha = hash(data);
        let p = self.blob(&sha)?;
        let damaged = self.damaged.contains(&sha);
        if !damaged && p.exists() {
            return Ok((sha, 0));
        }
        let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(data)?;
        let compressed = encoder.finish()?;
        safe_fs::write_atomic(&p, &compressed, damaged)?;
        if self.damaged.remove(&sha) {
            self.repaired += 1;
        }
        let identity = blob_identity(&fs::symlink_metadata(&p)?);
        self.written.push((sha.clone(), identity));
        Ok((sha, compressed.len() as u64))
    }
    /// Records the chunks verified or written so far, in one transaction: a chunk whose
    /// record a crash lost is verified again by the next pass.
    pub fn record_written(&mut self) -> Result<()> {
        let tx = self.db.transaction()?;
        {
            let mut insert = tx.prepare("INSERT OR REPLACE INTO blobs VALUES(?,?)")?;
            for (sha, identity) in self.written.drain(..) {
                insert.execute(params![sha, identity])?;
            }
        }
        tx.commit()?;
        Ok(())
    }
    /// Checks every chunk the catalog references. A chunk is damaged when its file is
    /// missing, or changed since it was last verified and no longer holds its content;
    /// unchanged files are trusted, so a pass costs a stat per chunk.
    pub fn verify(&mut self) -> Result<()> {
        let referenced: Vec<(String, Option<String>)> = self
            .db
            .prepare(
                "SELECT c.sha,b.identity FROM (SELECT DISTINCT sha FROM chunks) c LEFT JOIN blobs b ON b.sha=c.sha",
            )?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        for (sha, recorded) in referenced {
            let meta = match fs::symlink_metadata(self.blob(&sha)?) {
                Ok(meta) => Some(meta),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
                Err(e) => return Err(e.into()),
            };
            if let Some(meta) = meta.filter(fs::Metadata::is_file) {
                let identity = blob_identity(&meta);
                if recorded.as_deref() == Some(identity.as_str()) {
                    continue;
                }
                if self.load(&sha).is_ok() {
                    self.written.push((sha, identity));
                    continue;
                }
            }
            self.damaged.insert(sha);
        }
        self.record_written()
    }
    /// Whether any chunk of version `id` is damaged.
    pub fn version_damaged(&self, id: i64) -> Result<bool> {
        if self.damaged.is_empty() {
            return Ok(false);
        }
        Ok(self
            .chunks(id)?
            .iter()
            .any(|sha| self.damaged.contains(sha)))
    }
    /// Keeps the damaged chunks still referenced, for status between passes.
    pub fn record_damage(&mut self) -> Result<()> {
        if !self.damaged.is_empty() {
            let referenced = self.referenced()?;
            self.damaged.retain(|sha| referenced.contains(sha));
        }
        let tx = self.db.transaction()?;
        tx.execute("DELETE FROM damaged", [])?;
        {
            let mut insert = tx.prepare("INSERT INTO damaged VALUES(?)")?;
            for sha in &self.damaged {
                insert.execute([sha])?;
            }
        }
        tx.commit()?;
        Ok(())
    }
    /// Records chunks found damaged outside a capture, and forgets that they were verified,
    /// so that status reports them and the next capture checks and re-stores them.
    pub fn record_found_damage(&mut self, damaged: &HashSet<String>) -> Result<()> {
        let tx = self.db.transaction()?;
        {
            let mut forget = tx.prepare("DELETE FROM blobs WHERE sha=?")?;
            let mut record = tx.prepare("INSERT OR IGNORE INTO damaged VALUES(?)")?;
            for sha in damaged {
                forget.execute([sha])?;
                record.execute([sha])?;
            }
        }
        tx.commit()?;
        Ok(())
    }
    fn referenced(&self) -> Result<HashSet<String>> {
        Ok(self
            .db
            .prepare("SELECT DISTINCT sha FROM chunks")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?)
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
        let used = self.referenced()?;
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
        self.db.execute(
            "DELETE FROM blobs WHERE sha NOT IN (SELECT sha FROM chunks)",
            [],
        )?;
        Ok(bytes)
    }
}
