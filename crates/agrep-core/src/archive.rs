//! Native-source archives: transactional catalog, immutable SHA-256 gzip chunks.
use crate::{
    data::{self, DataRoot},
    ingest::registry::{Adapter, ADAPTERS},
    safe_fs,
};
use anyhow::{bail, Context, Result};
use flate2::{read::GzDecoder, write::GzEncoder, Compression};
use fs2::FileExt;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
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
struct Archive {
    db: Connection,
    dir: PathBuf,
    _lock: File,
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn valid_hash(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

impl Archive {
    fn open(root: &DataRoot, blocking: bool) -> Result<Option<Self>> {
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
        if blocking {
            lock.lock_exclusive()?;
        } else if let Err(e) = lock.try_lock_exclusive() {
            if e.kind() == std::io::ErrorKind::WouldBlock {
                return Ok(None);
            }
            return Err(e.into());
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
    fn load(&self, sha: &str) -> Result<Vec<u8>> {
        let mut decoder = GzDecoder::new(File::open(self.blob(sha)?)?).take(CHUNK as u64 + 1);
        let mut bytes = Vec::new();
        decoder.read_to_end(&mut bytes)?;
        if bytes.len() > CHUNK || hash(&bytes) != sha {
            bail!("archive content hash mismatch: {sha}")
        }
        Ok(bytes)
    }
    fn store(&self, data: &[u8]) -> Result<(String, u64)> {
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
    fn chunks(&self, id: i64) -> Result<Vec<String>> {
        Ok(self
            .db
            .prepare("SELECT sha FROM chunks WHERE version=? ORDER BY position")?
            .query_map([id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?)
    }
    fn latest(&self) -> Result<Vec<Record>> {
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
    fn prune(&mut self, keep: usize) -> Result<(usize, u64)> {
        let tx = self.db.transaction()?;
        let removed = if keep == 0 {
            0
        } else {
            tx.execute("DELETE FROM versions WHERE id IN (SELECT id FROM (SELECT id,row_number() OVER(PARTITION BY path ORDER BY id DESC) n FROM versions) WHERE n>?)", [keep])?
        };
        tx.commit()?;
        Ok((removed, self.gc()?))
    }
    fn gc(&self) -> Result<u64> {
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
                    let rel = entry
                        .path()
                        .strip_prefix(root)
                        .unwrap_or(&entry.path())
                        .to_path_buf();
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
    let rel = path.strip_prefix(root).unwrap_or(path);
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
    out.sort_by(|a, b| a.path.cmp(&b.path));
    out.dedup_by(|a, b| a.path == b.path);
    (out, failures)
}

fn open_source(path: &Path) -> Result<File> {
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("source is a symbolic link");
    }
    let mut options = OpenOptions::new();
    options.read(true);
    crate::ingest::registry::secure_read_options(&mut options);
    let file = options.open(path)?;
    if !file.metadata()?.is_file() {
        bail!("source is not a regular file")
    }
    Ok(file)
}
fn copy_database(backup: &rusqlite::backup::Backup<'_, '_>, blocking: bool) -> Result<()> {
    loop {
        match backup.step(-1)? {
            rusqlite::backup::StepResult::Done => return Ok(()),
            rusqlite::backup::StepResult::More => continue,
            _ if blocking => std::thread::sleep(Duration::from_millis(10)),
            _ => bail!("could not snapshot SQLite source"),
        }
    }
}
fn sqlite_snapshot(path: &Path, dir: &Path, blocking: bool) -> Result<tempfile::NamedTempFile> {
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("source is a symbolic link");
    }
    for suffix in ["-wal", "-shm", "-journal"] {
        if fs::symlink_metadata(format!("{}{suffix}", path.display()))
            .is_ok_and(|m| m.file_type().is_symlink())
        {
            bail!("source sidecar is a symbolic link");
        }
    }
    let source = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    source.busy_timeout(Duration::ZERO)?;
    let temp = tempfile::NamedTempFile::new_in(dir)?;
    let mut destination = Connection::open(temp.path())?;
    {
        let backup = rusqlite::backup::Backup::new(&source, &mut destination)?;
        copy_database(&backup, blocking)?;
    }
    destination.close().map_err(|(_, e)| e)?;
    Ok(temp)
}
fn capture_one(
    archive: &mut Archive,
    source: &Source,
    stats: &mut Stats,
    scheduled: bool,
) -> Result<()> {
    let mut file = open_source(&source.path)?;
    let before = safe_fs::identity(&file.metadata()?);
    let mut header = [0u8; 16];
    let n = file.read(&mut header)?;
    let sqlite = n == 16 && &header == b"SQLite format 3\0";
    let mut fingerprint = format!("{:?}", safe_fs::identity(&file.metadata()?));
    if sqlite {
        let wal = PathBuf::from(format!("{}-wal", source.path.display()));
        if fs::symlink_metadata(&wal).is_ok_and(|m| m.file_type().is_symlink()) {
            bail!("source sidecar is a symbolic link");
        }
        fingerprint.push_str(&format!(
            "{:?}",
            fs::metadata(wal).ok().as_ref().map(safe_fs::identity)
        ));
    }
    let previous_fingerprint: Option<String> = archive
        .db
        .query_row(
            "SELECT fingerprint FROM versions WHERE path=? ORDER BY id DESC LIMIT 1",
            [source.path.to_string_lossy().as_ref()],
            |r| r.get(0),
        )
        .optional()?;
    if previous_fingerprint.as_deref() == Some(fingerprint.as_str()) {
        stats.unchanged += 1;
        return Ok(());
    }
    use std::io::{Seek, SeekFrom};
    file.seek(SeekFrom::Start(0))?;
    let snapshot = if sqlite {
        Some(sqlite_snapshot(&source.path, &archive.dir, !scheduled)?)
    } else {
        None
    };
    if let Some(ref snapshot) = snapshot {
        file = File::open(snapshot.path())?;
    }
    let previous: Option<(i64, u64, String)> = archive
        .db
        .query_row(
            "SELECT id,size,sha FROM versions WHERE path=? ORDER BY id DESC LIMIT 1",
            [source.path.to_string_lossy().as_ref()],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    let old_chunks = if let Some((id, _, _)) = &previous {
        archive.chunks(*id)?
    } else {
        Vec::new()
    };
    let mut chunks = Vec::new();
    let mut digest = Sha256::new();
    let mut size = 0;
    let mut added = 0;
    let mut prefix_matches = !sqlite && previous.is_some();
    loop {
        let mut chunk = vec![0; CHUNK];
        let mut n = 0;
        while n < CHUNK {
            let count = file.read(&mut chunk[n..])?;
            if count == 0 {
                break;
            }
            n += count;
        }
        if n == 0 {
            break;
        }
        chunk.truncate(n);
        if prefix_matches {
            if let Some(sha) = old_chunks.get(chunks.len()) {
                let identical = hash(&chunk) == *sha;
                let extends_partial = previous.as_ref().is_some_and(|(_, len, _)| {
                    *len % CHUNK as u64 != 0 && chunks.len() + 1 == old_chunks.len()
                });
                if !identical
                    && !(extends_partial && {
                        let old = archive.load(sha)?;
                        chunk.starts_with(&old)
                    })
                {
                    prefix_matches = false;
                }
            }
        }
        digest.update(&chunk);
        size += n as u64;
        let (sha, bytes) = archive.store(&chunk)?;
        chunks.push(sha);
        added += bytes;
    }
    if !sqlite
        && (safe_fs::identity(&file.metadata()?) != before
            || safe_fs::identity(&fs::metadata(&source.path)?) != before)
    {
        bail!("source changed during capture")
    }
    if size == 0 {
        stats.unchanged += 1;
        return Ok(());
    }
    let sha = format!("{:x}", digest.finalize());
    if previous.as_ref().is_some_and(|(_, _, old)| old == &sha) {
        archive.db.execute(
            "UPDATE versions SET fingerprint=? WHERE id=?",
            params![fingerprint, previous.as_ref().unwrap().0],
        )?;
        stats.unchanged += 1;
        return Ok(());
    }
    let home = crate::ingest::home();
    let home_relative = source
        .path
        .strip_prefix(&home)
        .ok()
        .map(|p| p.to_string_lossy().into_owned());
    let tail = if source.path == source.root {
        source
            .path
            .file_name()
            .context("source has no filename")?
            .to_string_lossy()
            .into_owned()
    } else {
        source
            .path
            .strip_prefix(&source.root)?
            .to_string_lossy()
            .into_owned()
    };
    let tx = archive.db.transaction()?;
    tx.execute("INSERT INTO versions(agent,path,home_relative,tail,size,sha,sqlite,created,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",params![source.agent,source.path.to_string_lossy(),home_relative,tail,size,sha,sqlite,now(),fingerprint])?;
    let id = tx.last_insert_rowid();
    for (i, sha) in chunks.iter().enumerate() {
        tx.execute("INSERT INTO chunks VALUES(?,?,?)", params![id, i, sha])?;
    }
    tx.commit()?;
    if prefix_matches && previous.is_some_and(|(_, old, _)| size > old) {
        stats.appended += 1;
    } else {
        stats.full += 1;
    }
    stats.bytes_stored += added;
    Ok(())
}

pub fn capture(root: &DataRoot, scheduled: bool) -> Result<Stats> {
    let Some(mut archive) = Archive::open(root, !scheduled)? else {
        return Ok(Stats::default());
    };
    let policy = root.config()?.archive;
    if scheduled {
        let last: i64 = archive
            .db
            .query_row("SELECT value FROM meta WHERE key='last_capture'", [], |r| {
                r.get(0)
            })
            .optional()?
            .unwrap_or(0);
        if !policy.enabled || now() - last < INTERVAL_MS {
            return Ok(Stats::default());
        }
    }
    let (sources, failures) = discover();
    let mut stats = Stats {
        files: sources.len(),
        failures,
        ..Stats::default()
    };
    for source in sources {
        if let Err(e) = capture_one(&mut archive, &source, &mut stats, scheduled) {
            stats
                .failures
                .push(format!("{}: {e:#}", source.path.display()));
        }
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
                    current == *r
                        || current.strip_prefix(r).is_ok_and(|p| {
                            p.components().all(|c| matches!(c, Component::Normal(_)))
                        })
                })
        });
    if !valid {
        bail!("archive path is outside documented store roots")
    }
    Ok(current)
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
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    let mut digest = Sha256::new();
    let mut size = 0u64;
    for sha in a.chunks(r.id)? {
        let bytes = a.load(&sha)?;
        digest.update(&bytes);
        size += bytes.len() as u64;
        temp.write_all(&bytes)?;
    }
    if size != r.size || format!("{:x}", digest.finalize()) != r.sha {
        bail!("archive aggregate hash mismatch")
    }
    temp.as_file().sync_all()?;
    // SQLite backup waits for the target and preserves open readers and WAL coherency.
    if r.sqlite && dst.exists() {
        for suffix in ["-wal", "-shm", "-journal"] {
            safe_fs::plain_path(&PathBuf::from(format!("{}{suffix}", dst.display())))?;
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
                let sidecar = PathBuf::from(format!("{}{suffix}", dst.display()));
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
    File::open(parent)?.sync_all()?;
    Ok(())
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
    let session_pattern =
        regex::Regex::new(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")?;
    let shared_session = records.first().is_some_and(|first| {
        session_pattern.find_iter(&first.path).any(|m| {
            m.as_str().to_lowercase().starts_with(&needle_lower)
                && records
                    .iter()
                    .all(|r| r.path.to_lowercase().contains(&m.as_str().to_lowercase()))
        })
    });
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
pub fn status(root: &DataRoot) -> Result<serde_json::Value> {
    let policy = root.config()?.archive;
    let path = root.path().join("archive/catalog.db");
    if !path.exists() {
        return Ok(
            serde_json::json!({"enabled":policy.enabled,"keep":policy.keep,"files":0,"versions":0,"stored_bytes":0,"source_bytes":0,"last_capture":null}),
        );
    }
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let (versions, files, size): (u64, u64, u64) = db.query_row(
        "SELECT count(*),count(DISTINCT path),coalesce(sum(size),0) FROM versions",
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let mut stored = 0;
    for entry in fs::read_dir(root.path().join("archive/blobs"))? {
        let entry = entry?;
        stored += entry.metadata()?.len();
    }
    let last: Option<i64> = db
        .query_row("SELECT value FROM meta WHERE key='last_capture'", [], |r| {
            r.get(0)
        })
        .optional()?;
    let errors: Option<String> = db
        .query_row("SELECT errors FROM health WHERE id=1", [], |r| r.get(0))
        .optional()?;
    let errors: Vec<String> = serde_json::from_str(errors.as_deref().unwrap_or("[]"))?;
    Ok(
        serde_json::json!({"enabled":policy.enabled,"keep":policy.keep,"files":files,"versions":versions,"stored_bytes":stored,"source_bytes":size,"last_capture":last,"failures":errors}),
    )
}

#[cfg(test)]
mod source_open_tests {
    use super::*;
    #[cfg(unix)]
    fn assert_source_flags(file: &File) {
        use std::os::fd::AsRawFd;
        let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
        assert!(flags >= 0);
        assert_ne!(
            flags & libc::O_NONBLOCK,
            0,
            "a source swapped for a FIFO must not block open"
        );
        assert_eq!(flags & libc::O_ACCMODE, libc::O_RDONLY);
        let temp = tempfile::tempdir().unwrap();
        let fifo = temp.path().join("source.fifo");
        use std::os::unix::ffi::OsStrExt;
        let name = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(open_source(&fifo)
            .unwrap_err()
            .to_string()
            .contains("not a regular file"));
    }
    #[cfg(not(unix))]
    fn assert_source_flags(file: &File) {
        assert!(file.metadata().unwrap().is_file());
        let mut read_only = file;
        assert!(read_only.write_all(b"must remain read only").is_err());
    }
    #[test]
    fn source_open_is_nonblocking_and_read_only() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("source");
        fs::write(&path, b"fixture").unwrap();
        let mut file = open_source(&path).unwrap();
        assert_source_flags(&file);
        let mut text = String::new();
        file.read_to_string(&mut text).unwrap();
        assert_eq!(text, "fixture");
    }
}
