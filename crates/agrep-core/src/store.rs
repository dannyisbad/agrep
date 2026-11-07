//! One WAL transaction is the visibility boundary for rows, indexes and fingerprints.
pub use crate::source_token::SourceToken;
use crate::{
    data::DataRoot,
    ingest::registry::{self, Adapter, ADAPTERS},
    intake_batch::{IntakeBatch, SourceBatch},
    intake_pipeline::{parse_ahead, Parsed},
    model::Message,
};
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Transaction};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
    time::Instant,
};
pub const BLOCK: i64 = 512;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Stamp {
    pub path: String,
    pub agent: String,
    pub size: u64,
    pub mtime_ns: i64,
    pub token: SourceToken,
    pub error: Option<String>,
}
#[derive(Default, Debug, Serialize)]
pub struct Refresh {
    pub sources: usize,
    pub parsed: usize,
    pub changed_rows: usize,
    pub errors: usize,
    pub skipped_lock: bool,
    pub census_ms: f64,
    pub total_ms: f64,
    pub parse_ms: f64,
    pub write_ms: f64,
    pub bounds_ms: f64,
    pub stats_ms: f64,
    pub resumed_sources: usize,
    pub bytes_read: usize,
    pub bytes_parsed: usize,
    pub adapter_parse_ms: BTreeMap<String, f64>,
    pub adapter_source_parse_ms: BTreeMap<String, f64>,
    pub adapter_sources: BTreeMap<String, usize>,
    pub adapter_write_ms: BTreeMap<String, f64>,
    pub row_diff_ms: f64,
    pub fts_ms: f64,
    pub secondary_indexes_ms: f64,
    pub canonical_ms: f64,
    pub dictionary_ms: f64,
    pub node_chunks_ms: f64,
    pub commit_ms: f64,
    /// Opening the writer connection, WAL recovery included.
    pub open_ms: f64,
}
thread_local! {
    static WRITE_TIMES: std::cell::Cell<(u64,u64)> = const { std::cell::Cell::new((0,0)) };
}
struct WriteTimer {
    start: Instant,
    fts: bool,
    before_fts: u64,
}
impl WriteTimer {
    fn new(fts: bool) -> Self {
        Self {
            start: Instant::now(),
            fts,
            before_fts: WRITE_TIMES.get().1,
        }
    }
}
impl Drop for WriteTimer {
    fn drop(&mut self) {
        let elapsed = self.start.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64;
        let (rows, fts) = WRITE_TIMES.get();
        WRITE_TIMES.set(if self.fts {
            (rows, fts.saturating_add(elapsed))
        } else {
            (
                rows.saturating_add(elapsed.saturating_sub(fts.saturating_sub(self.before_fts))),
                fts,
            )
        });
    }
}
pub fn reader(root: &DataRoot) -> Result<Connection> {
    let c = {
        let _span = crate::semantic_profile::span("reader_open");
        Connection::open_with_flags(
            root.db(),
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?
    };
    {
        let _span = crate::semantic_profile::span("reader_pragmas");
        register_functions(&c)?;
        c.execute_batch("PRAGMA query_only=ON; BEGIN;")?;
    }
    if crate::semantic_profile::timing_enabled() {
        // Force the otherwise lazy schema read into its own diagnostic phase.
        // Ordinary commands still load it with their first real statement.
        let _span = crate::semantic_profile::span("reader_schema");
        let _statement = c.prepare("SELECT path FROM sources LIMIT 0")?;
    }
    Ok(c)
}
pub fn writer(root: &DataRoot) -> Result<Connection> {
    open_writer(&root.db(), true)
}
fn open_writer(path: &Path, wal: bool) -> Result<Connection> {
    let initialize = path.metadata().map_or(true, |m| m.len() == 0);
    let c = Connection::open(path)?;
    c.set_prepared_statement_cache_capacity(256);
    c.execute_batch(if wal {"PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-32000; PRAGMA wal_autocheckpoint=4096; PRAGMA journal_size_limit=0;"}else{"PRAGMA journal_mode=DELETE; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-131072;"})?;
    if initialize {
        schema(&c)?;
    }
    register_functions(&c)?;
    Ok(c)
}
fn register_functions(c: &Connection) -> Result<()> {
    c.create_scalar_function(
        "project_match",
        2,
        rusqlite::functions::FunctionFlags::SQLITE_UTF8
            | rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| {
            let value: String = ctx.get(0)?;
            let pattern: String = ctx.get(1)?;
            Ok(crate::query::project_ok(&value, &pattern))
        },
    )?;
    Ok(())
}
pub(crate) fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,agent TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,change_token BLOB NOT NULL,last_error TEXT);
CREATE TABLE IF NOT EXISTS sessions(agent TEXT NOT NULL,id TEXT NOT NULL,project TEXT NOT NULL,cwd TEXT NOT NULL DEFAULT '',parent TEXT NOT NULL,family TEXT NOT NULL,side INTEGER NOT NULL,title TEXT NOT NULL,first_ts INTEGER NOT NULL,last_ts INTEGER NOT NULL,PRIMARY KEY(agent,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS sessions_family ON sessions(agent,family);
CREATE INDEX IF NOT EXISTS sessions_parent ON sessions(agent,parent);
CREATE TABLE IF NOT EXISTS family_nodes(agent TEXT NOT NULL,id TEXT NOT NULL,parent TEXT NOT NULL,PRIMARY KEY(agent,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS family_alias_claims(agent TEXT NOT NULL,alias TEXT NOT NULL,session TEXT NOT NULL,PRIMARY KEY(agent,alias,session)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS family_alias_session ON family_alias_claims(agent,session);
CREATE TABLE IF NOT EXISTS session_sources(agent TEXT NOT NULL,session TEXT NOT NULL,source INTEGER NOT NULL,PRIMARY KEY(agent,session,source)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS session_sources_source ON session_sources(source);
CREATE TABLE IF NOT EXISTS reply_shapes(source INTEGER NOT NULL,session TEXT NOT NULL,turn INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(source,session,turn)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS reply_shapes_session ON reply_shapes(session,source,turn);
CREATE TABLE IF NOT EXISTS term_stats(token TEXT PRIMARY KEY,n INTEGER NOT NULL,s INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS source_directories(path TEXT PRIMARY KEY,agent TEXT NOT NULL,mtime INTEGER NOT NULL,dirs BLOB NOT NULL,files BLOB NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS parser_cursors(path TEXT PRIMARY KEY REFERENCES sources(path) ON DELETE CASCADE ON UPDATE CASCADE,body BLOB NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS parser_graph(source INTEGER NOT NULL,bucket INTEGER NOT NULL,body BLOB NOT NULL,PRIMARY KEY(source,bucket)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gram_postings(lane INTEGER NOT NULL,gram INTEGER NOT NULL,chunk INTEGER NOT NULL,body BLOB NOT NULL,PRIMARY KEY(lane,gram,chunk)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bound_rebuild(block INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS gram_raises(lane INTEGER NOT NULL,gram INTEGER NOT NULL,block INTEGER NOT NULL,frequency INTEGER NOT NULL,max_ts INTEGER NOT NULL,weight INTEGER NOT NULL,PRIMARY KEY(lane,gram,block)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS gram_raises_block ON gram_raises(lane,block);
CREATE TABLE IF NOT EXISTS block_grams(lane INTEGER NOT NULL,block INTEGER NOT NULL,body BLOB NOT NULL,PRIMARY KEY(lane,block)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS blocks(id INTEGER PRIMARY KEY,max_ts INTEGER NOT NULL,user_ts INTEGER,weight REAL NOT NULL,min_session TEXT NOT NULL,min_turn INTEGER NOT NULL,max_gap INTEGER NOT NULL,all_ascii INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tool_blocks(id INTEGER PRIMARY KEY,max_ts INTEGER NOT NULL,user_ts INTEGER,weight REAL NOT NULL,min_session TEXT NOT NULL,min_turn INTEGER NOT NULL,max_gap INTEGER NOT NULL,all_ascii INTEGER NOT NULL);
")?;
    crate::tool_store::schema(c)?;
    for table in ["prose", "tools"] {
        if table == "prose" {
            c.execute_batch(&format!("CREATE TABLE IF NOT EXISTS {table}(id INTEGER PRIMARY KEY,source INTEGER NOT NULL,agent TEXT NOT NULL,session TEXT NOT NULL,project TEXT NOT NULL,turn INTEGER NOT NULL,who TEXT NOT NULL,model TEXT NOT NULL,model_source INTEGER NOT NULL,ts INTEGER NOT NULL,text BLOB NOT NULL,digest TEXT NOT NULL,extra TEXT NOT NULL,position INTEGER NOT NULL DEFAULT 0,visible INTEGER NOT NULL DEFAULT 1,UNIQUE(source,session,turn,who,extra));
CREATE INDEX IF NOT EXISTS {table}_scope ON {table}(agent,ts DESC);
CREATE INDEX IF NOT EXISTS {table}_session ON {table}(session,turn);
CREATE INDEX IF NOT EXISTS {table}_time ON {table}(ts DESC);
CREATE INDEX IF NOT EXISTS {table}_project ON {table}(project,ts DESC);
CREATE INDEX IF NOT EXISTS {table}_model ON {table}(model,ts DESC);


"))?;
            c.execute_batch(&format!("CREATE TRIGGER IF NOT EXISTS {table}_count_insert AFTER INSERT ON {table} WHEN new.visible=1 BEGIN INSERT INTO state(key,value) VALUES('{table}_count',1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; END;
CREATE TRIGGER IF NOT EXISTS {table}_count_delete AFTER DELETE ON {table} WHEN old.visible=1 BEGIN UPDATE state SET value=CAST(value AS INTEGER)-1 WHERE key='{table}_count'; END;
CREATE TRIGGER IF NOT EXISTS {table}_count_visibility AFTER UPDATE OF visible ON {table} WHEN old.visible!=new.visible BEGIN UPDATE state SET value=CAST(value AS INTEGER)+new.visible-old.visible WHERE key='{table}_count'; END;
CREATE TRIGGER IF NOT EXISTS prose_user_count_insert AFTER INSERT ON prose WHEN new.visible=1 AND new.who!='agent' BEGIN INSERT INTO state(key,value) VALUES('prose_user_count',1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; END;
CREATE TRIGGER IF NOT EXISTS prose_user_count_delete AFTER DELETE ON prose WHEN old.visible=1 AND old.who!='agent' BEGIN UPDATE state SET value=CAST(value AS INTEGER)-1 WHERE key='prose_user_count'; END;
CREATE TRIGGER IF NOT EXISTS prose_user_count_visibility AFTER UPDATE OF visible ON prose WHEN old.visible!=new.visible AND new.who!='agent' BEGIN UPDATE state SET value=CAST(value AS INTEGER)+new.visible-old.visible WHERE key='prose_user_count'; END;"))?;
        }
        let columns = "text";
        let tokenizer = if table == "tools" {
            "unicode61 remove_diacritics 0"
        } else {
            "trigram case_sensitive 1"
        };
        c.execute_batch(&format!("CREATE VIRTUAL TABLE IF NOT EXISTS {table}_fts USING fts5({columns},content='',contentless_delete=1,tokenize='{tokenizer}',detail=full);"))?;
        c.execute_batch(&format!("CREATE VIRTUAL TABLE IF NOT EXISTS {table}_case_fts USING fts5(text,content='',contentless_delete=1,tokenize='trigram case_sensitive 1',detail=full);"))?;
        c.execute_batch(&format!(
            "CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vocab USING fts5vocab({table}_fts, row);"
        ))?;
    }
    crate::row_stats::schema(c)?;
    c.execute_batch("CREATE TABLE IF NOT EXISTS bound_updates(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS tool_bound_updates(id INTEGER PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS tool_bound_update AFTER UPDATE OF tool_id,blob,visible,active,ts,position,session_key ON event_rows WHEN old.active=1 AND old.blob IS NOT NULL AND (old.tool_id!=new.tool_id OR old.blob IS NOT new.blob OR old.visible!=new.visible OR old.active!=new.active OR old.ts!=new.ts OR old.position!=new.position OR old.session_key!=new.session_key) BEGIN INSERT OR IGNORE INTO tool_bound_updates VALUES(old.tool_id); END;
CREATE TRIGGER IF NOT EXISTS tool_bound_delete BEFORE DELETE ON event_rows WHEN old.active=1 AND old.blob IS NOT NULL BEGIN INSERT OR IGNORE INTO tool_bound_updates VALUES(old.tool_id); END;
CREATE TRIGGER IF NOT EXISTS prose_bound_update AFTER UPDATE OF text,ts,who,session,position,visible ON prose WHEN (old.visible=1 OR new.visible=1) AND (old.text IS NOT new.text OR old.ts!=new.ts OR old.who!=new.who OR old.session!=new.session OR old.position!=new.position OR old.visible!=new.visible) BEGIN INSERT OR IGNORE INTO bound_updates VALUES(old.id); END;
CREATE TRIGGER IF NOT EXISTS prose_bound_delete BEFORE DELETE ON prose WHEN old.visible=1 BEGIN INSERT OR IGNORE INTO bound_updates VALUES(old.id); END;")?;
    c.execute_batch("CREATE TABLE IF NOT EXISTS semantic_queue(row_id INTEGER PRIMARY KEY,digest TEXT NOT NULL,ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS semantic_queue_time ON semantic_queue(ts DESC);
CREATE TABLE IF NOT EXISTS semantic_refs(row_id INTEGER PRIMARY KEY,digest TEXT NOT NULL,content_key TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS semantic_refs_content ON semantic_refs(content_key);
CREATE TABLE IF NOT EXISTS semantic_blocks(name TEXT PRIMARY KEY,rows INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_chunks(content_key TEXT NOT NULL,ordinal INTEGER NOT NULL,block TEXT NOT NULL,slot INTEGER NOT NULL,PRIMARY KEY(content_key,ordinal)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS semantic_chunks_location ON semantic_chunks(block,slot);
CREATE TRIGGER IF NOT EXISTS semantic_ref_insert AFTER INSERT ON semantic_refs BEGIN INSERT INTO state(key,value) VALUES('semantic_ref_count',1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; END;
CREATE TRIGGER IF NOT EXISTS semantic_ref_delete AFTER DELETE ON semantic_refs BEGIN UPDATE state SET value=CAST(value AS INTEGER)-1 WHERE key='semantic_ref_count'; END;
CREATE TRIGGER IF NOT EXISTS semantic_insert AFTER INSERT ON prose WHEN new.visible=1 BEGIN INSERT OR REPLACE INTO semantic_queue VALUES(new.id,new.digest,new.ts); END;
CREATE TRIGGER IF NOT EXISTS semantic_update AFTER UPDATE OF text,digest,visible,who,agent ON prose BEGIN DELETE FROM semantic_refs WHERE row_id=new.id; DELETE FROM semantic_queue WHERE row_id=new.id; INSERT INTO semantic_queue SELECT new.id,new.digest,new.ts WHERE new.visible=1; END;
CREATE TRIGGER IF NOT EXISTS semantic_delete AFTER DELETE ON prose BEGIN DELETE FROM semantic_refs WHERE row_id=old.id; DELETE FROM semantic_queue WHERE row_id=old.id; END;")?;
    crate::prose_index::schema(c)?;
    crate::tool_index::schema(c)?;
    Ok(())
}
// Initial construction can sort these indexes once after ingestion. Incremental
// transactions keep them live because their scope probes are then required.
fn search_indexes(c: &Connection, create: bool) -> Result<()> {
    {
        let table = "prose";
        for (suffix, columns) in [
            ("scope", "agent,ts DESC"),
            ("session", "session,turn"),
            ("time", "ts DESC"),
            ("project", "project,ts DESC"),
            ("model", "model,ts DESC"),
        ] {
            c.execute_batch(&if create {
                format!("CREATE INDEX IF NOT EXISTS {table}_{suffix} ON {table}({columns});")
            } else {
                format!("DROP INDEX IF EXISTS {table}_{suffix};")
            })?;
        }
    }
    crate::tool_store::indexes(c, create)
}
pub fn digest(text: &str) -> String {
    hex_digest(Sha256::digest(text.as_bytes()))
}
pub(crate) fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(64);
    for &byte in bytes.as_ref() {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 15) as usize] as char);
    }
    out
}
pub fn normalize(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    crate::boundary_rank::normalize_token(text)
}
/// A literal-search key: Python `re.I` equivalences, without compatibility
/// normalization or expansions. Boundary statistics keep their separate NFKC fold.
pub(crate) fn search_fold(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    crate::unicode_v16::sre_fold_text(text, |_, _, _| {})
}

fn search_key(text: &str) -> (String, bool) {
    if text.is_ascii() {
        return (text.to_ascii_lowercase(), false);
    }
    let mut aliases = false;
    let key = crate::unicode_v16::sre_fold_text(text, |_, _, mask| aliases |= mask != 0);
    (key, aliases)
}

/// Remove precisely the separators the phrase matcher permits between query terms.
pub fn joined(text: &str) -> String {
    if text.is_ascii() {
        return text
            .bytes()
            .filter(|b| b.is_ascii_alphanumeric())
            .map(char::from)
            .collect();
    }
    static SEPARATORS: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"[\W_]").unwrap());
    SEPARATORS.replace_all(text, "").into_owned()
}
pub fn decode(table: &str, bytes: Vec<u8>) -> Result<String> {
    if table == "tools" {
        crate::tool_store::decode(&bytes)
    } else {
        Ok(String::from_utf8(bytes)?)
    }
}
pub fn encode_tool(text: &str) -> Result<Vec<u8>> {
    crate::tool_store::encode(text)
}
fn walk(path: &Path, adapter: &dyn Adapter, paths: &mut Vec<PathBuf>) -> Result<()> {
    let meta = match fs::symlink_metadata(path) {
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
                walk(&path, adapter, paths)?
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
/// error of a census that could not list it then, or a source a directory replaced.
pub(crate) fn gone(path: &Path) -> bool {
    match fs::symlink_metadata(path) {
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
        if registry::metadata_is_link(&m) {
            anyhow::bail!("source is a symlink")
        }
        let mut token = SourceToken::File(registry::metadata_change_token(p, &m)?);
        // A source SQLite WAL is an input too. Reading metadata never opens it writable.
        if p.extension()
            .and_then(|s| s.to_str())
            .is_some_and(|s| matches!(s, "db" | "sqlite" | "sqlite3" | "vscdb"))
        {
            let wal = PathBuf::from(format!("{}-wal", p.to_string_lossy()));
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
                if let Err(e) = walk(&root, *a, &mut paths) {
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
pub fn refresh(root: &DataRoot, explicit: bool, full: bool) -> Result<Refresh> {
    refresh_inner(root, explicit, full, false, None, None)
}
/// Wait for preceding writers, then refresh changes from the caller's census against
/// their committed stamps. Sources they already covered need no parsing.
pub fn refresh_checked(root: &DataRoot, check: crate::freshness::Check) -> Result<Refresh> {
    refresh_inner(root, true, false, false, None, Some(check.census))
}
pub fn reindex(root: &DataRoot) -> Result<Refresh> {
    refresh_inner(root, true, true, true, None, None)
}
/// Refresh only sources already attributed to this transcript. Other changed
/// sources remain pending and their recorded stamps are not advanced.
pub fn refresh_session(root: &DataRoot, agent: &str, session: &str) -> Result<Refresh> {
    let c = reader(root)?;
    let paths = c.prepare("SELECT s.path FROM sources s JOIN session_sources x ON x.source=s.rowid WHERE x.agent=? AND x.session=?")?
        .query_map([agent, session], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<HashSet<_>>>()?;
    anyhow::ensure!(!paths.is_empty(), "caller has no indexed source");
    drop(c);
    refresh_inner(root, true, false, false, Some(&paths), None)
}
fn refresh_inner(
    root: &DataRoot,
    explicit: bool,
    full: bool,
    rebuild: bool,
    scope: Option<&HashSet<String>>,
    census: Option<crate::freshness::Census>,
) -> Result<Refresh> {
    let start = Instant::now();
    WRITE_TIMES.set((0, 0));
    let first = !root.db().exists();
    let lock = match root.lock(explicit || first)? {
        // A merge step is bounded and yields to queued writers, so wait for it rather
        // than leave the change pending until another command starts a worker.
        None if root.merging()? => root.lock(true)?,
        lock => lock,
    };
    if lock.is_none() {
        return Ok(Refresh {
            skipped_lock: true,
            ..Default::default()
        });
    }
    let first = !root.db().exists();
    let initial = root.path().join("index.building.db");
    if first {
        for suffix in ["", "-journal", "-wal", "-shm"] {
            let p = PathBuf::from(format!("{}{suffix}", initial.display()));
            match fs::remove_file(p) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
        }
    }
    let opening = Instant::now();
    let mut c = if first {
        open_writer(&initial, false)?
    } else {
        writer(root)?
    };
    let open_ms = opening.elapsed().as_secs_f64() * 1000.;
    if first {
        search_indexes(&c, false)?;
        // Initial rows are invisible until the final commit. Build their derived
        // bookkeeping in bulk instead of maintaining counters and dirty queues
        // for each of the million inserted rows.
        for name in [
            "prose_count_insert",
            "prose_count_delete",
            "prose_count_visibility",
            "prose_user_count_insert",
            "prose_user_count_delete",
            "prose_user_count_visibility",
            "tools_count_insert",
            "tools_count_delete",
            "tools_count_visibility",
            "prose_stats_insert",
            "prose_stats_delete",
            "prose_stats_update",
            "session_stats_family",
            "tool_bound_update",
            "tool_bound_delete",
            "tool_word_dirty_insert",
            "tool_word_dirty_update",
            "tool_word_old_update",
            "tool_word_old_delete",
            "semantic_insert",
            "semantic_update",
            "semantic_delete",
        ] {
            c.execute_batch(&format!("DROP TRIGGER IF EXISTS {name}"))?;
        }
    }
    let old = {
        let _span = crate::semantic_profile::span("refresh_known_sources");
        crate::freshness::indexed(&c)?
    };
    let harness_body = match fs::read_to_string(root.path().join("harness_prefixes.txt")) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e.into()),
    };
    let harness = crate::row_class::HarnessPolicy::parse(&harness_body);
    let policy_digest = digest(&harness_body);
    let previous: Option<String> = c
        .query_row("SELECT value FROM state WHERE key='harness'", [], |r| {
            r.get(0)
        })
        .optional()?;
    let full = full || previous.as_deref() != Some(&policy_digest);
    let tick = Instant::now();
    let mut census = match census {
        Some(mut census) => {
            // A preceding writer may have ingested a newer stamp than this census saw.
            // Recheck only differing files, so its completed work is not parsed again.
            let mut indexed = old.iter().peekable();
            census.stamps.retain_mut(|s| {
                while indexed.peek().is_some_and(|old| old.0 < s.path) {
                    indexed.next();
                }
                if indexed.peek().is_some_and(|(path, size, mtime, token)| {
                    *path == s.path && s.size == *size && s.mtime_ns == *mtime && s.token == *token
                }) {
                    return true;
                }
                match stamp_present(&s.agent, Path::new(&s.path)) {
                    Some(current) => {
                        *s = current;
                        true
                    }
                    None => false,
                }
            });
            census
        }
        None => crate::freshness::scan(&c, false)?,
    };
    let mut stamps = std::mem::take(&mut census.stamps);
    if let Some(paths) = scope {
        stamps.retain(|source| paths.contains(&source.path));
    }
    let census_ms = tick.elapsed().as_secs_f64() * 1000.;
    let mut changed = HashSet::new();
    let mut removed = Vec::new();
    let mut next = 0;
    for (path, size, mtime, token) in &old {
        while next < stamps.len() && stamps[next].path < *path {
            changed.insert(PathBuf::from(&stamps[next].path));
            next += 1;
        }
        if let Some(stamp) = stamps.get(next).filter(|s| s.path == *path) {
            if full || stamp.size != *size || stamp.mtime_ns != *mtime || stamp.token != *token {
                changed.insert(PathBuf::from(path));
            }
            next += 1;
        } else if scope.is_none_or(|paths| paths.contains(path)) && gone(Path::new(path)) {
            removed.push(path.clone());
        }
    }
    changed.extend(stamps[next..].iter().map(|s| PathBuf::from(&s.path)));
    let mut info = Refresh {
        sources: stamps.len(),
        census_ms,
        open_ms,
        ..Default::default()
    };
    if changed.is_empty() && removed.is_empty() && !first {
        info.total_ms = start.elapsed().as_secs_f64() * 1000.;
        return Ok(info);
    }
    if first {
        // 0.4.9's notice (py/indexd_runtime.py:944-946): the size is unknown here.
        eprintln!("building the search db (a first build can take a while on a large history) …");
    }
    let changed_stamps: Vec<_> = stamps
        .iter()
        .filter(|s| changed.contains(Path::new(&s.path)))
        .collect();
    let mut active: HashSet<String> = changed_stamps.iter().map(|s| s.agent.clone()).collect();
    let mut removed_agent = c.prepare_cached("SELECT agent FROM sources WHERE path=?")?;
    for key in &removed {
        if let Some(agent) = removed_agent
            .query_row([key], |r| r.get::<_, String>(0))
            .optional()?
        {
            active.insert(agent);
        }
    }
    drop(removed_agent);
    let mut cursors = HashMap::new();
    if !full {
        let mut q = c.prepare("SELECT body FROM parser_cursors WHERE path=?")?;
        for path in &changed {
            if let Some(body) = q
                .query_row([path.to_string_lossy()], |r| r.get::<_, Vec<u8>>(0))
                .optional()?
            {
                cursors.insert(path.to_string_lossy().into_owned(), body);
            }
        }
    }
    let parse_start = Instant::now();
    static PARSERS: std::sync::LazyLock<rayon::ThreadPool> = std::sync::LazyLock::new(|| {
        rayon::ThreadPoolBuilder::new()
            .num_threads(
                std::thread::available_parallelism()
                    .map_or(1, usize::from)
                    .min(4),
            )
            .build()
            .expect("parser thread pool")
    });
    static BUILD_PARSERS: std::sync::LazyLock<rayon::ThreadPool> = std::sync::LazyLock::new(|| {
        rayon::ThreadPoolBuilder::new()
            .num_threads(
                std::thread::available_parallelism()
                    .map_or(1, usize::from)
                    .min(8),
            )
            .build()
            .expect("bulk parser thread pool")
    });
    let parsers = if first || full {
        &BUILD_PARSERS
    } else {
        &PARSERS
    };
    let parser_width = std::thread::available_parallelism()
        .map_or(1, usize::from)
        .min(if first || full { 8 } else { 4 });
    let source_paths: HashMap<&str, Vec<PathBuf>> =
        changed_stamps
            .iter()
            .fold(HashMap::new(), |mut paths, source| {
                paths
                    .entry(source.agent.as_str())
                    .or_default()
                    .push(PathBuf::from(&source.path));
                paths
            });
    let mut token_cache = HashMap::new();
    for adapter in ADAPTERS
        .iter()
        .filter(|a| active.contains(a.name()) && a.fingerprint() == registry::Fingerprint::Token)
    {
        let lower = format!("token:{}:", adapter.name());
        let upper = format!("token:{};", adapter.name());
        let mut query =
            c.prepare_cached("SELECT path,change_token FROM sources WHERE path>=?1 AND path<?2")?;
        let mut tokens = HashMap::new();
        for row in query.query_map(params![lower, upper], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, SourceToken>(1)?))
        })? {
            let (path, token) = row?;
            let SourceToken::Opaque(token) = token else {
                anyhow::bail!("invalid adapter token")
            };
            tokens.insert(path, token);
        }
        token_cache.insert(adapter.name(), tokens);
    }
    let collect_adapter = |a: &&dyn Adapter| {
        let adapter_start = Instant::now();
        let tokens = token_cache.get(a.name()).cloned().unwrap_or_default();
        let mut batch = IntakeBatch {
            changed: if a.name() == "gemini" {
                stamps
                    .iter()
                    .filter(|s| s.agent == "gemini")
                    .map(|s| PathBuf::from(&s.path))
                    .collect()
            } else {
                changed.clone()
            },
            force: full,
            cursors: cursors.clone(),
            database: Some(root.db()),
            defer: true,
            source_paths: source_paths.get(a.name()).cloned().unwrap_or_default(),
            tokens,
            ..Default::default()
        };
        crate::intake::take_diagnostics();
        let (m, e, o, issues) = a.collect_checked(&mut batch);
        for message in crate::intake::take_diagnostics() {
            batch.issues.push(crate::intake_batch::SourceReadIssue::new(
                a.name(),
                a.runtime_issue_root(),
                "source-read-failed",
                message,
            ));
        }
        batch.issues.extend(issues);
        if a.fingerprint() == registry::Fingerprint::Always {
            batch.batches.push(SourceBatch {
                key: format!("@{}", a.name()),
                agent: a.name().into(),
                messages: m,
                events: e,
                error: if matches!(
                    o,
                    crate::intake_batch::ReadOutcome::Complete
                        | crate::intake_batch::ReadOutcome::Undone
                ) && batch.issues.is_empty()
                {
                    None
                } else {
                    Some(format!("{} source parse incomplete", a.name()))
                },
                ..Default::default()
            })
        }
        (
            a.name().to_owned(),
            adapter_start.elapsed().as_secs_f64() * 1000.,
            batch,
        )
    };
    let batches: Vec<_> = if active.len() <= 1 {
        ADAPTERS
            .iter()
            .filter(|a| active.contains(a.name()))
            .map(collect_adapter)
            .collect()
    } else {
        parsers.install(|| {
            ADAPTERS
                .par_iter()
                .filter(|a| active.contains(a.name()))
                .map(collect_adapter)
                .collect()
        })
    };
    info.parse_ms = parse_start.elapsed().as_secs_f64() * 1000.;
    info.adapter_parse_ms = batches
        .iter()
        .map(|(name, ms, _)| (name.clone(), *ms))
        .collect();
    info.adapter_sources = batches
        .iter()
        .map(|(name, _, b)| (name.clone(), b.batches.len() + b.pending.len()))
        .collect();
    let batches = batches.into_iter().map(|(name, _, batch)| (name, batch));
    let write_start = Instant::now();
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    if scope.is_none() {
        crate::freshness::save(&tx, &census)?;
    }
    if rebuild {
        for t in ["prose", "tools"] {
            if t == "prose" {
                tx.execute("DELETE FROM prose", [])?;
            }
            tx.execute(&format!("DELETE FROM {t}_fts"), [])?;
            tx.execute(&format!("DELETE FROM {t}_case_fts"), [])?;
        }
        tx.execute("DELETE FROM event_rows", [])?;
        tx.execute("DELETE FROM sessions", [])?;
        tx.execute("DELETE FROM gram_postings", [])?;
        tx.execute("DELETE FROM block_grams", [])?;
        tx.execute("DELETE FROM blocks", [])?;
        tx.execute("DELETE FROM tool_blocks", [])?;
        for table in [
            "family_nodes",
            "family_alias_claims",
            "reply_shapes",
            "term_stats",
            "parser_cursors",
            "parser_graph",
            "row_stats",
            "family_token_counts",
            "stats_dirty",
            "session_sources",
            "tool_words",
            "tool_word_blocks",
            "tool_trigrams",
            "bound_rebuild",
            "gram_raises",
            "tool_lexicon",
            "tool_word_dirty",
            "tool_word_batch",
            "prose_terms",
            "tool_blob_sources",
            "tool_blobs",
            "blob_gc",
            "event_sessions",
            "symbols",
        ] {
            tx.execute(&format!("DELETE FROM {table}"), [])?;
        }
    }
    let mut changes = Changes {
        bulk: first || rebuild,
        ..Default::default()
    };
    let mut turnless_before = HashSet::new();
    let mut canonical_sessions = HashSet::new();
    let mut fast_sessions = HashSet::new();
    let mut forced_sessions = HashSet::new();
    let mut errors = HashMap::new();
    for key in &removed {
        source_sessions(&tx, key, &mut canonical_sessions)?;
        source_sessions(&tx, key, &mut forced_sessions)?;

        for t in ["prose", "tools"] {
            remove_source(&tx, t, key, &mut changes)?;
        }
        tx.execute("DELETE FROM sources WHERE path=?", [key])?;
    }
    let mut pages = crate::incremental::GraphPages::new(parsers);
    let sizes: HashMap<&str, u64> = changed_stamps
        .iter()
        .map(|s| (s.path.as_str(), s.size))
        .collect();
    let mut parse_wait = std::time::Duration::ZERO;
    for (adapter, b) in batches {
        let adapter_write_start = Instant::now();
        for key in &b.retired {
            source_sessions(&tx, key, &mut canonical_sessions)?;
            source_sessions(&tx, key, &mut forced_sessions)?;

            for table in ["prose", "tools"] {
                remove_source(&tx, table, key, &mut changes)?;
            }
        }

        if b.issues.is_empty() {
            for key in b
                .tokens
                .keys()
                .filter(|key| key.starts_with("token:") && !b.seen_tokens.contains(*key))
            {
                let agent = key.split(':').nth(1).unwrap_or("");
                if active.contains(agent)
                    && ADAPTERS.iter().any(|a| {
                        a.name() == agent && a.fingerprint() == registry::Fingerprint::Token
                    })
                {
                    source_sessions(&tx, key, &mut canonical_sessions)?;
                    source_sessions(&tx, key, &mut forced_sessions)?;

                    for table in ["prose", "tools"] {
                        remove_source(&tx, table, key, &mut changes)?;
                    }
                    tx.execute("DELETE FROM sources WHERE path=?", [key])?;
                }
            }
        }
        for issue in b.issues {
            errors.insert(issue.path.to_string_lossy().into_owned(), issue.reason);
        }
        let tokens = &b.tokens;
        let mut source_parse_ms = 0.;
        let mut write = |(mut s, prepared): Parsed| -> Result<()> {
            source_parse_ms += s.parse_ms;
            s.messages =
                crate::row_class::normalize_messages(std::mem::take(&mut s.messages), &harness);
            if s.key.starts_with('@') {
                s.messages = crate::intake_batch::canonicalize_messages(s.messages);
            }
            info.parsed += 1;
            if let Some(error) = s.error {
                if s.key.starts_with('@') || s.key.starts_with("token:") {
                    let token =
                        SourceToken::Opaque(tokens.get(&s.key).cloned().unwrap_or_default());
                    tx.execute("INSERT INTO sources VALUES(?1,?2,0,0,?3,?4) ON CONFLICT(path) DO UPDATE SET change_token=?3,last_error=?4", params![s.key,s.agent,token,error])?;
                }
                errors.insert(s.key.clone(), error);
                return Ok(());
            }
            if !s.append {
                source_sessions(&tx, &s.key, &mut canonical_sessions)?;
                source_sessions(&tx, &s.key, &mut forced_sessions)?;
            }
            let changed_sessions: HashSet<_> = s
                .messages
                .iter()
                .map(|m| (m.agent.to_string(), m.session.to_string()))
                .chain(
                    s.events
                        .iter()
                        .map(|e| (e.agent.to_string(), e.session.clone())),
                )
                .collect();
            if !first {
                for (agent, session) in &changed_sessions {
                    if !tx.prepare_cached("SELECT EXISTS(SELECT 1 FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2)")?.query_row(params![agent,session], |r| r.get::<_,bool>(0))? {
                        turnless_before.insert((agent.clone(),session.clone()));
                    }
                }
            }
            if s.append {
                fast_sessions.extend(changed_sessions);
            } else {
                forced_sessions.extend(changed_sessions);
            }
            canonical_sessions.extend(
                s.messages
                    .iter()
                    .map(|m| (m.agent.to_string(), m.session.to_string())),
            );
            canonical_sessions.extend(
                s.events
                    .iter()
                    .map(|e| (e.agent.to_string(), e.session.clone())),
            );
            let changed_rows = replace(&tx, &s, prepared, &mut changes, first)?;
            info.changed_rows += changed_rows;
            info.bytes_read += s.bytes_read;
            info.bytes_parsed += s.bytes_parsed;
            info.resumed_sources += usize::from(s.append);
            if let Some(cursor) = &s.cursor {
                tx.execute("INSERT INTO parser_cursors VALUES(?1,?2) ON CONFLICT(path) DO UPDATE SET body=excluded.body",params![s.key,cursor])?;
                let source: i64 =
                    tx.query_row("SELECT rowid FROM sources WHERE path=?", [&s.key], |r| {
                        r.get(0)
                    })?;
                let phase = Instant::now();
                let session = s
                    .messages
                    .first()
                    .map(|m| (m.agent, m.session.as_ref()))
                    .or_else(|| s.events.first().map(|e| (e.agent, e.session.as_str())));
                crate::incremental::save_nodes(
                    &tx,
                    source,
                    std::mem::take(&mut s.nodes),
                    s.reset_nodes,
                    session,
                    &s.events,
                    &mut pages,
                )?;
                info.node_chunks_ms += phase.elapsed().as_secs_f64() * 1000.;
            }
            if s.key.starts_with('@') || s.key.starts_with("token:") {
                let token = SourceToken::Opaque(tokens.get(&s.key).cloned().unwrap_or_default());
                tx.execute("INSERT INTO sources VALUES(?1,?2,0,0,?3,NULL) ON CONFLICT(path) DO UPDATE SET change_token=?3,last_error=NULL",params![s.key,s.agent,token])?;
            }
            Ok(())
        };
        for s in b.batches {
            write((s, Vec::new()))?;
        }
        parse_wait += parse_ahead(parser_width, b.pending, &sizes, &mut write)?;
        info.adapter_source_parse_ms
            .insert(adapter.clone(), source_parse_ms);
        info.adapter_write_ms
            .insert(adapter, adapter_write_start.elapsed().as_secs_f64() * 1000.);
    }
    let phase = Instant::now();
    pages.finish(&tx)?;
    info.node_chunks_ms += phase.elapsed().as_secs_f64() * 1000.;
    for s in stamps {
        let error = errors.get(&s.path).cloned().or(s.error).or_else(|| {
            errors.iter().find_map(|(path, error)| {
                Path::new(&s.path)
                    .starts_with(Path::new(path))
                    .then(|| error.clone())
            })
        });
        if !changed.contains(Path::new(&s.path)) {
            if let Some(error) = error {
                tx.prepare_cached(
                    "UPDATE sources SET last_error=?2 WHERE path=?1 AND last_error IS NOT ?2",
                )?
                .execute(params![s.path, error])?;
            }
            continue;
        }
        tx.prepare_cached("INSERT INTO sources VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(path) DO UPDATE SET size=?3,mtime_ns=?4,change_token=?5,last_error=?6")?.execute(params![s.path,s.agent,s.size,s.mtime_ns,s.token,error])?;
    }
    for (path, error) in &errors {
        tx.execute(
            "UPDATE sources SET last_error=?2 WHERE path=?1",
            params![path, error],
        )?;
    }
    crate::tool_store::collect(&tx)?;
    if first {
        let phase = Instant::now();
        search_indexes(&tx, true)?;
        info.secondary_indexes_ms = phase.elapsed().as_secs_f64() * 1000.;
    }
    fast_sessions.retain(|s| !forced_sessions.contains(s));
    let phase = Instant::now();
    canonicalize(
        &tx,
        &canonical_sessions,
        &fast_sessions,
        &mut changes,
        first,
    )?;
    settle_turnless(
        &tx,
        &turnless_before,
        &canonical_sessions,
        &mut changes,
        first,
    )?;
    info.canonical_ms = phase.elapsed().as_secs_f64() * 1000.;
    changes.fts.apply(&tx)?;
    let presence = if first { build_fts(&tx)? } else { None };
    let phase = Instant::now();
    crate::tool_index::refresh(&tx, first || rebuild, presence)?;
    info.dictionary_ms = phase.elapsed().as_secs_f64() * 1000.;
    // Sources parse while earlier ones are written; only the writer's waits for a
    // parse count as parsing.
    info.parse_ms += parse_wait.as_secs_f64() * 1000.;
    info.write_ms = write_start
        .elapsed()
        .saturating_sub(parse_wait)
        .as_secs_f64()
        * 1000.;
    let phase = Instant::now();
    if first || rebuild {
        update_blocks(&tx, &changes.blocks, None)?;
    } else {
        raise_changed_bounds(&tx, &mut changes)?;
    }
    tx.execute("DELETE FROM bound_updates", [])?;
    tx.execute("DELETE FROM tool_bound_updates", [])?;
    info.bounds_ms = phase.elapsed().as_secs_f64() * 1000.;
    let phase = Instant::now();
    if first {
        tx.execute("DELETE FROM sessions WHERE NOT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=sessions.agent AND p.session=sessions.id)",[])?;
    } else {
        let mut q = tx.prepare_cached("DELETE FROM sessions WHERE agent=?1 AND id=?2 AND NOT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=?1 AND p.session=?2)")?;
        for (agent, session) in &canonical_sessions {
            q.execute(params![agent, session])?;
        }
    }
    resolve_families(&tx, &canonical_sessions)?;
    crate::prose_index::refresh(&tx, first || rebuild)?;
    if first {
        crate::row_stats::build(&tx)?;
    } else {
        crate::row_stats::refresh(&tx)?;
    }
    if first {
        schema(&tx)?;
        let (rows, user_rows): (i64, i64) = tx.query_row(
            "SELECT count(*),coalesce(sum(who!='agent'),0) FROM prose WHERE visible=1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        for (key, value) in [("prose_count", rows), ("prose_user_count", user_rows)] {
            tx.execute("INSERT INTO state(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", params![key, value])?;
        }
        tx.execute("INSERT INTO state(key,value) SELECT 'tools_count',count(*) FROM tools WHERE visible=1 ON CONFLICT(key) DO UPDATE SET value=excluded.value",[])?;
        tx.execute(
            "INSERT INTO semantic_queue SELECT id,digest,ts FROM prose WHERE visible=1",
            [],
        )?;
    }
    info.stats_ms = phase.elapsed().as_secs_f64() * 1000.;
    // 0.4.9 listed a chat only through its turns (py/corpusdb.py:2221-2234).
    tx.execute("INSERT INTO state VALUES('refreshed',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[chrono::Utc::now().timestamp_millis().to_string()])?;
    if scope.is_none() {
        tx.execute("INSERT INTO state VALUES('harness',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[policy_digest])?;
    }
    let phase = Instant::now();
    tx.commit()?;
    if first {
        c.execute_batch("PRAGMA journal_mode=WAL;")?;
        c.close().map_err(|(_, e)| e)?;
        // A connection that has not used the FTS tables caches none of their structures.
        let mut sealing = open_writer(&initial, true)?;
        let tx = sealing.transaction()?;
        crate::fts_segments::seal(&tx)?;
        tx.commit()?;
        sealing.close().map_err(|(_, e)| e)?;
        fs::rename(&initial, root.db())?;
        #[cfg(unix)]
        fs::File::open(root.path())?.sync_all()?;
    } else {
        let _span = crate::semantic_profile::span("refresh_connection_close");
        c.close().map_err(|(_, e)| e)?;
    }
    info.commit_ms = phase.elapsed().as_secs_f64() * 1000.;
    let (row_ns, fts_ns) = WRITE_TIMES.get();
    info.row_diff_ms = row_ns as f64 / 1_000_000.;
    info.fts_ms = fts_ns as f64 / 1_000_000.;
    info.errors = errors.len();
    info.total_ms = start.elapsed().as_secs_f64() * 1000.;
    drop(lock);
    Ok(info)
}
type FtsInput = Vec<(i64, Vec<u8>)>;
type FtsPrepared = Vec<(i64, String, bool)>;
type FtsWords = Vec<(i64, Vec<Box<[u8]>>)>;
/// A prepared row: its id, indexed text and distinct dictionary words.
type FtsRow = (i64, String, Vec<Box<[u8]>>, bool);
fn read_fts_batch(rows: &mut rusqlite::Rows<'_>) -> Result<FtsInput> {
    let mut input = Vec::with_capacity(1024);
    for _ in 0..1024 {
        let Some(row) = rows.next()? else { break };
        input.push((row.get(0)?, row.get(1)?));
    }
    Ok(input)
}
fn prepare_fts_batch(
    table: &str,
    input: FtsInput,
    tokenizers: Option<&crate::tool_index::Tokenizers<'_>>,
) -> Result<(FtsPrepared, FtsWords)> {
    let rows: Vec<FtsRow> = input
        .into_par_iter()
        .map_init(
            || tokenizers.map(|t| t.tokenizer()),
            |tokenizer, (id, bytes)| {
                let (text, aliases) = search_key(&decode(table, bytes)?);
                if table != "tools" {
                    return Ok((id, text, Vec::new(), aliases));
                }
                let text = crate::tool_index::index_text(&text).into_owned();
                let words = match tokenizer {
                    Some(Ok(tokenizer)) => crate::tool_index::row_words(tokenizer, &text)?,
                    Some(Err(error)) => anyhow::bail!("{error:#}"),
                    None => Vec::new(),
                };
                Ok((id, text, words, aliases))
            },
        )
        .collect::<Result<_>>()?;
    let mut prepared = Vec::with_capacity(rows.len());
    let mut words = Vec::with_capacity(rows.len());
    for (id, text, row_words, aliases) in rows {
        prepared.push((id, text, aliases));
        words.push((id, row_words));
    }
    Ok((prepared, words))
}
/// Returns tool word presence, gathered beside the inserts from the same text.
fn build_fts(tx: &Transaction<'_>) -> Result<Option<crate::tool_index::Presence>> {
    let _timer = WriteTimer::new(true);
    let tokenizers = crate::tool_index::Tokenizers::new(tx)?;
    let mut collected = None;
    for table in ["prose", "tools"] {
        tx.execute_batch(&format!("INSERT INTO {table}_fts({table}_fts,rank) VALUES('automerge',0);INSERT INTO {table}_fts({table}_fts,rank) VALUES('crisismerge',64);INSERT INTO {table}_fts({table}_fts,rank) VALUES('hashsize',16777216);"))?;
        let mut source = tx.prepare(&format!(
            "SELECT id,text FROM {table} WHERE visible=1 ORDER BY id"
        ))?;
        let mut rows = source.query([])?;
        // SQLite remains exclusively on this thread. Two bounded batches let
        // decode/normalization overlap inserts, without a second writer or an
        // unbounded copy of the corpus.
        std::thread::scope(|scope| -> Result<()> {
            let (send, input) = std::sync::mpsc::sync_channel::<FtsInput>(1);
            let (output, receive) = std::sync::mpsc::sync_channel::<Result<FtsPrepared>>(1);
            let tokenizers = (table == "tools").then_some(&tokenizers);
            // Word presence is merged here, after the batch is handed to the
            // writer, so it overlaps the FTS inserts.
            let worker = scope.spawn(move || -> Result<Option<crate::tool_index::Presence>> {
                let mut presence = tokenizers.map(|_| crate::tool_index::new_presence());
                while let Ok(batch) = input.recv() {
                    match prepare_fts_batch(table, batch, tokenizers) {
                        Ok((prepared, words)) => {
                            if output.send(Ok(prepared)).is_err() {
                                break;
                            }
                            if let Some(presence) = presence.as_mut() {
                                crate::tool_index::add_rows(presence, words)?;
                            }
                        }
                        Err(error) => {
                            let _ = output.send(Err(error));
                            break;
                        }
                    }
                }
                Ok(presence)
            });
            let result = (|| -> Result<()> {
                let mut queued = 0;
                let mut ended = false;
                for _ in 0..2 {
                    let batch = read_fts_batch(&mut rows)?;
                    if batch.is_empty() {
                        ended = true;
                        break;
                    }
                    send.send(batch)
                        .map_err(|_| anyhow::anyhow!("FTS preparation ended"))?;
                    queued += 1;
                }
                while queued > 0 {
                    let prepared = receive
                        .recv()
                        .map_err(|_| anyhow::anyhow!("FTS preparation ended"))??;
                    queued -= 1;
                    if !ended {
                        let batch = read_fts_batch(&mut rows)?;
                        if batch.is_empty() {
                            ended = true;
                        } else {
                            send.send(batch)
                                .map_err(|_| anyhow::anyhow!("FTS preparation ended"))?;
                            queued += 1;
                        }
                    }
                    // A multi-row statement opens a savepoint, and FTS5 flushes its
                    // pending terms to a new segment at every savepoint.
                    let mut insert = tx.prepare_cached(&format!(
                        "INSERT INTO {table}_fts(rowid,text) VALUES(?1,?2)"
                    ))?;
                    let mut mark = tx.prepare_cached(&format!(
                        "INSERT INTO {table}_case_fts(rowid,text) VALUES(?1,?2)"
                    ))?;
                    for (id, text, aliases) in &prepared {
                        insert.execute(params![id, text])?;
                        if *aliases {
                            mark.execute(params![id, text])?;
                        }
                    }
                }
                Ok(())
            })();
            drop(send);
            drop(receive);
            let presence = worker
                .join()
                .map_err(|_| anyhow::anyhow!("FTS preparation panicked"))??;
            result?;
            if presence.is_some() {
                collected = presence;
            }
            Ok(())
        })?;
        tx.execute(
            &format!("INSERT INTO {table}_fts({table}_fts) VALUES('optimize')"),
            [],
        )?;
        tx.execute(
            &format!("INSERT INTO {table}_case_fts({table}_case_fts) VALUES('optimize')"),
            [],
        )?;
        crate::fts_segments::configure(tx, &format!("{table}_fts"))?;
    }
    Ok(collected)
}
/// What one transaction's row writes leave for derived structures to catch up on.
#[derive(Default)]
struct Changes {
    /// Prose block ids, tool blocks as `-1 - id`.
    blocks: BTreeSet<i64>,
    bounds: [BTreeSet<i64>; 2],
    // A first build summarizes whole blocks and needs no per-row delta set.
    bulk: bool,
    fts: FtsWrites,
}
impl Changes {
    fn touch(&mut self, table: &str, id: i64) {
        let lane = usize::from(table == "tools");
        if !self.bulk {
            self.bounds[lane].insert(id);
        }
        self.blocks.insert(if lane == 1 {
            -1 - id / BLOCK
        } else {
            id / BLOCK
        });
    }
}
/// The FTS rows a transaction touched, per lane: whether each was indexed when the
/// transaction began and whether it is indexed at its end.
///
/// FTS5 flushes its pending terms into a new segment whenever a write's rowid is not
/// above the previous write's, and at every statement savepoint, which each row
/// statement with a trigger opens. Writing the net change once, in rowid order and
/// after the row statements, leaves one new segment per table.
#[derive(Default)]
struct FtsWrites([BTreeMap<i64, (bool, bool)>; 2]);
impl FtsWrites {
    fn insert(&mut self, table: &str, id: i64) {
        self.0[usize::from(table == "tools")]
            .entry(id)
            .or_insert((false, false))
            .1 = true;
    }
    fn delete(&mut self, table: &str, id: i64) {
        self.0[usize::from(table == "tools")]
            .entry(id)
            .or_insert((true, false))
            .1 = false;
    }
    /// Indexed text is read back from the rows, as the first build reads it.
    fn apply(&mut self, tx: &Transaction<'_>) -> Result<()> {
        let _timer = WriteTimer::new(true);
        for (table, rows) in ["prose", "tools"].into_iter().zip(&mut self.0) {
            let mut delete =
                tx.prepare_cached(&format!("DELETE FROM {table}_fts WHERE rowid=?"))?;
            let mut insert = tx.prepare_cached(&format!(
                "INSERT INTO {table}_fts(rowid,text) VALUES(?1,?2)"
            ))?;
            let mut text = tx.prepare_cached(&format!("SELECT text FROM {table} WHERE id=?"))?;
            for (id, (before, after)) in std::mem::take(rows) {
                if before {
                    delete.execute([id])?;
                    tx.execute(&format!("DELETE FROM {table}_case_fts WHERE rowid=?"), [id])?;
                }
                if after {
                    let (normalized, aliases) =
                        search_key(&decode(table, text.query_row([id], |r| r.get(0))?)?);
                    if aliases {
                        tx.execute(
                            &format!("INSERT INTO {table}_case_fts(rowid,text) VALUES(?1,?2)"),
                            params![
                                id,
                                if table == "tools" {
                                    crate::tool_index::index_text(&normalized)
                                } else {
                                    std::borrow::Cow::Borrowed(normalized.as_str())
                                }
                            ],
                        )?;
                    }
                    if table == "tools" {
                        insert.execute(params![
                            id,
                            crate::tool_index::index_text(&normalized).as_ref()
                        ])?;
                    } else {
                        insert.execute(params![id, normalized])?;
                    }
                }
            }
        }
        Ok(())
    }
}
fn reply_metadata(m: &crate::model::Message) -> Result<String> {
    let mut value = serde_json::to_value(&*m.reply_shape)?;
    value["original_reply_chars"] = serde_json::json!(m.reply_chars);
    value["source_parent"] = serde_json::json!(m.parent);
    value["source_side"] = serde_json::json!(m.side);
    Ok(serde_json::to_string(&value)?)
}
fn source_sessions(
    tx: &Transaction<'_>,
    path: &str,
    out: &mut HashSet<(String, String)>,
) -> Result<()> {
    let mut stmt=tx.prepare_cached("SELECT agent,session FROM prose WHERE source=(SELECT rowid FROM sources WHERE path=?1) UNION SELECT agent,session FROM tools WHERE source=(SELECT rowid FROM sources WHERE path=?1)")?;
    for row in stmt.query_map([path], |r| Ok((r.get(0)?, r.get(1)?)))? {
        out.insert(row?);
    }
    Ok(())
}
struct ReplayRow {
    id: i64,
    source: i64,
    turn: i64,
    position: i64,
    visible: bool,
    who: String,
    ts: i64,
    text: String,
    project: String,
    model: String,
    model_source: String,
}
struct ReplayMessage {
    key_text: String,
    row: usize,
    reply: Option<usize>,
    reply_chars: usize,
    parent: String,
    side: bool,
}
fn replay_cmp(
    a: &ReplayMessage,
    b: &ReplayMessage,
    rows: &[ReplayRow],
    agent: &str,
) -> std::cmp::Ordering {
    let x = &rows[a.row];
    let y = &rows[b.row];
    let reply = |m: &ReplayMessage| m.reply.map_or("", |i| rows[i].text.as_str());
    a.reply_chars
        .cmp(&b.reply_chars)
        .then_with(|| reply(a).len().cmp(&reply(b).len()))
        .then_with(|| (!x.model.is_empty()).cmp(&(!y.model.is_empty())))
        .then_with(|| (!x.project.is_empty()).cmp(&(!y.project.is_empty())))
        .then_with(|| (x.project != agent).cmp(&(y.project != agent)))
        .then_with(|| (!a.parent.is_empty()).cmp(&(!b.parent.is_empty())))
        .then(a.side.cmp(&b.side))
        .then_with(|| match (x.ts > 0, y.ts > 0) {
            (true, false) => std::cmp::Ordering::Greater,
            (false, true) => std::cmp::Ordering::Less,
            (true, true) => y.ts.cmp(&x.ts),
            _ => std::cmp::Ordering::Equal,
        })
        .then_with(|| reply(a).cmp(reply(b)))
        .then(x.model.cmp(&y.model))
        .then(x.project.cmp(&y.project))
        .then(a.parent.cmp(&b.parent))
        .then(x.who.cmp(&y.who))
        .then(x.model_source.cmp(&y.model_source))
}
fn set_visibility(
    tx: &Transaction<'_>,
    table: &str,
    id: i64,
    old: (bool, i64),
    new: (bool, i64),
    changes: &mut Changes,
    bulk: bool,
) -> Result<()> {
    let (old_visible, old_position) = old;
    let (visible, position) = new;
    if old_visible == visible && old_position == position {
        return Ok(());
    }
    if old_visible != visible && !bulk {
        if visible {
            changes.fts.insert(table, id);
        } else {
            changes.fts.delete(table, id);
        }
    }
    // The tools view's INSTEAD OF trigger makes the same event_rows write, after
    // materializing the whole view row, tool text included.
    let sql = if table == "tools" {
        "UPDATE event_rows SET visible=?2,position=?3 WHERE tool_id=?1 AND active=1"
    } else {
        "UPDATE prose SET visible=?2,position=?3 WHERE id=?1"
    };
    tx.prepare_cached(sql)?
        .execute(params![id, visible, position])?;
    changes.touch(table, id);
    Ok(())
}
fn session_title(text: &str) -> String {
    crate::history::one_line(text, 120)
}

fn refresh_session_bounds(tx: &Transaction<'_>, agent: &str, session: &str) -> Result<()> {
    // 0.4.9 bounded a chat by its turns alone: a tool row never moves first_ts/last_ts,
    // and a turn without a timestamp (0) is skipped rather than taken as the minimum.
    // Left to itself the planner answers min/max(ts) by walking prose_scope through the
    // agent's whole history, testing each row's session.
    let first: Option<Vec<u8>> = tx.prepare_cached("SELECT text FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2 AND visible=1 AND who NOT IN ('agent','recap') ORDER BY position,id LIMIT 1")?.query_row(params![agent, session], |r| r.get(0)).optional()?;
    let title = first.map_or_else(String::new, |text| {
        session_title(&String::from_utf8_lossy(&text))
    });
    tx.prepare_cached("UPDATE sessions SET first_ts=coalesce((SELECT min(ts) FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2 AND visible=1 AND ts>0),0),last_ts=coalesce((SELECT max(ts) FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2 AND visible=1 AND ts>0),0),title=?3 WHERE agent=?1 AND id=?2")?.execute(params![agent,session,title])?;
    Ok(())
}
/// Chats in `table` (with its session id in `column`) that hold no turn.
fn turnless(tx: &Transaction<'_>, table: &str, column: &str) -> Result<HashSet<(String, String)>> {
    let sql = format!("SELECT agent,{column} FROM {table} t WHERE NOT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.session=t.{column} AND p.agent=t.agent)");
    let found = tx
        .prepare(&sql)?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(found)
}
/// 0.4.9 searched a chat's tool calls only beside its turns, under its first turn's project
/// (py/corpusdb.py:2221-2234): a listed chat without a turn has its calls hidden, and a
/// chat that was turnless when this refresh began shows its calls again once a turn
/// arrives, one copy per call id as in canonicalize.
fn settle_turnless(
    tx: &Transaction<'_>,
    before: &HashSet<(String, String)>,
    affected: &HashSet<(String, String)>,
    changes: &mut Changes,
    bulk: bool,
) -> Result<()> {
    let hidden = if bulk {
        turnless(tx, "sessions", "id")?
    } else {
        let mut hidden = HashSet::new();
        let mut q = tx.prepare_cached("SELECT EXISTS(SELECT 1 FROM sessions WHERE agent=?1 AND id=?2) AND NOT EXISTS(SELECT 1 FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2)")?;
        for (agent, session) in affected {
            if q.query_row(params![agent, session], |r| r.get::<_, bool>(0))? {
                hidden.insert((agent.clone(), session.clone()));
            }
        }
        hidden
    };
    let turned = before
        .iter()
        .filter(|s| !hidden.contains(*s))
        .filter_map(|(agent, session)| {
            tx.prepare_cached("SELECT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.session=?2 AND p.agent=?1)")
                .and_then(|mut q| q.query_row(params![agent, session], |r| r.get::<_, bool>(0)))
                .map(|turns| turns.then(|| (agent.clone(), session.clone())))
                .transpose()
        })
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for ((agent, session), turns) in hidden
        .iter()
        .map(|s| (s, false))
        .chain(turned.iter().map(|s| (s, true)))
    {
        let tools:Vec<(i64,i64,bool,String)>=tx.prepare_cached("SELECT t.id,t.position,t.visible,t.extra FROM tools t JOIN sources s ON s.rowid=t.source WHERE t.agent=?1 AND t.session=?2 ORDER BY s.path,t.id")?.query_map(params![agent,session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
        let mut seen = HashSet::new();
        for (id, position, visible, key) in tools {
            let retained = turns && (key.is_empty() || seen.insert(key));
            set_visibility(
                tx,
                "tools",
                id,
                (visible, position),
                (retained, position),
                changes,
                bulk,
            )?;
        }
        if turns {
            tx.prepare_cached("INSERT OR IGNORE INTO symbols(value) SELECT project FROM sessions WHERE agent=?1 AND id=?2")?.execute(params![agent, session])?;
            tx.prepare_cached("UPDATE event_rows SET project_key=(SELECT y.id FROM symbols y JOIN sessions s ON y.value=s.project WHERE s.agent=?1 AND s.id=?2) WHERE active=1 AND session_key=(SELECT id FROM event_sessions WHERE agent=?1 AND session=?2)")?.execute(params![agent, session])?;
        }
    }
    Ok(())
}
/// Keep source contributions for incremental recovery, but index only the canonical replay.
fn canonicalize(
    tx: &Transaction<'_>,
    sessions: &HashSet<(String, String)>,
    fast: &HashSet<(String, String)>,
    changes: &mut Changes,
    bulk: bool,
) -> Result<()> {
    let mut pi_order: HashMap<i64, Vec<usize>> = HashMap::new();
    let mut native_order = crate::refresh_order::Order::new(
        ADAPTERS
            .iter()
            .find(|a| a.name() == "pi")
            .unwrap()
            .store_roots(),
    );
    for (agent, session) in sessions {
        if bulk {
            let copies: usize = tx.query_row(
                "SELECT count(*) FROM session_sources WHERE agent=?1 AND session=?2",
                params![agent, session],
                |r| r.get(0),
            )?;
            // Initial metadata was aggregated while inserting this source. Only
            // sessions shared by sources need replay selection and row scans.
            if copies <= 1 {
                continue;
            }
        }
        if fast.contains(&(agent.clone(), session.clone())) {
            let n: i64 = tx.query_row(
                "SELECT count(*) FROM session_sources WHERE agent=?1 AND session=?2",
                params![agent, session],
                |r| r.get(0),
            )?;
            if n <= 1 {
                continue;
            }
        }
        let (copies,visible,positioned):(i64,i64,i64)=tx.query_row("SELECT count(DISTINCT source),coalesce(min(visible),1),coalesce(min(position=turn),1) FROM (SELECT source,visible,position,turn FROM prose WHERE agent=?1 AND session=?2 UNION ALL SELECT source,visible,position,turn FROM tools WHERE agent=?1 AND session=?2)",params![agent,session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
        refresh_session_bounds(tx, agent, session)?;
        if copies <= 1 && visible != 0 && positioned != 0 {
            continue;
        }
        let rows:Vec<ReplayRow>=tx.prepare("SELECT id,source,turn,position,visible,who,ts,text,project,model,model_source FROM prose WHERE agent=?1 AND session=?2 ORDER BY id")?.query_map(params![agent,session],|r|Ok(ReplayRow{id:r.get(0)?,source:r.get(1)?,turn:r.get(2)?,position:r.get(3)?,visible:r.get(4)?,who:r.get(5)?,ts:r.get(6)?,text:String::from_utf8_lossy(&r.get::<_,Vec<u8>>(7)?).into_owned(),project:r.get(8)?,model:r.get(9)?,model_source:r.get(10)?}))?.collect::<rusqlite::Result<_>>()?;
        let shapes: HashMap<(i64, i64), serde_json::Value> = tx
            .prepare("SELECT source,turn,body FROM reply_shapes INDEXED BY reply_shapes_session WHERE session=?")?
            .query_map([session], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })?
            .map(|r| {
                let (s, t, b) = r?;
                Ok(((s, t), serde_json::from_str(&b)?))
            })
            .collect::<Result<_>>()?;
        let replies: HashMap<_, _> = rows
            .iter()
            .enumerate()
            .filter(|(_, r)| r.who == "agent")
            .map(|(i, r)| ((r.source, r.turn), i))
            .collect();
        let mut messages = Vec::new();
        let prompts: HashSet<_> = rows
            .iter()
            .filter(|r| r.who != "agent")
            .map(|r| (r.source, r.turn))
            .collect();
        for (i, r) in rows
            .iter()
            .enumerate()
            .filter(|(_, r)| r.who != "agent" || !prompts.contains(&(r.source, r.turn)))
        {
            let reply = replies.get(&(r.source, r.turn)).copied();
            let shape = shapes.get(&(r.source, r.turn));
            messages.push(ReplayMessage {
                key_text: if r.who == "agent" {
                    String::new()
                } else {
                    r.text.clone()
                },
                row: i,
                reply,
                reply_chars: shape
                    .and_then(|v| v["original_reply_chars"].as_u64())
                    .map_or_else(
                        || reply.map_or(0, |j| rows[j].text.chars().count()),
                        |n| n as usize,
                    ),
                parent: shape
                    .and_then(|v| v["source_parent"].as_str())
                    .unwrap_or("")
                    .into(),
                side: shape
                    .and_then(|v| v["source_side"].as_bool())
                    .unwrap_or(false),
            });
        }
        // Equal-quality copies and session metadata follow 0.4.9 discovery order.
        if agent == "pi" {
            let sources: BTreeSet<_> = rows
                .iter()
                .map(|r| r.source)
                .filter(|id| !pi_order.contains_key(id))
                .collect();
            let mut ranks = Vec::new();
            let mut fallback = false;
            for id in sources {
                let path: String = tx
                    .prepare_cached("SELECT path FROM sources WHERE rowid=?")?
                    .query_row([id], |r| r.get(0))?;
                if let Some(rank) = native_order.rank(Path::new(&path)) {
                    ranks.push((id, rank));
                } else {
                    fallback = true;
                    break;
                }
            }
            if fallback {
                let paths = crate::ingest::pi::source_order();
                pi_order = tx
                    .prepare("SELECT rowid,path FROM sources WHERE agent='pi'")?
                    .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?
                    .map(|row| {
                        let (id, path) = row?;
                        Ok((
                            id,
                            vec![paths.get(Path::new(&path)).copied().unwrap_or(usize::MAX)],
                        ))
                    })
                    .collect::<rusqlite::Result<_>>()?;
            } else {
                pi_order.extend(ranks);
            }
        }
        let source_order = |m: &ReplayMessage| {
            (
                pi_order
                    .get(&rows[m.row].source)
                    .map_or(&[usize::MAX][..], |rank| rank.as_slice()),
                m.row,
            )
        };
        let mut best: BTreeMap<(i64, String), usize> = BTreeMap::new();
        for (i, m) in messages.iter().enumerate() {
            let r = &rows[m.row];
            let key = (r.turn, m.key_text.clone());
            if best.get(&key).is_none_or(|j| {
                let prior = &messages[*j];
                let quality = replay_cmp(m, prior, &rows, agent);
                quality.is_gt() || (quality.is_eq() && source_order(m) < source_order(prior))
            }) {
                best.insert(key, i);
            }
        }
        let mut chosen: Vec<usize> = best.values().copied().collect();
        let collision = chosen
            .iter()
            .map(|i| rows[messages[*i].row].turn)
            .collect::<HashSet<_>>()
            .len()
            < chosen.len();
        if collision {
            chosen.sort_by_key(|i| {
                let r = &rows[messages[*i].row];
                (r.ts <= 0, r.ts, r.turn, r.text.as_str())
            });
        }
        let positions: HashMap<usize, i64> = chosen
            .iter()
            .enumerate()
            .map(|(n, i)| {
                (
                    *i,
                    if collision {
                        n as i64
                    } else {
                        rows[messages[*i].row].turn
                    },
                )
            })
            .collect();
        let mut keep = HashMap::new();
        let mut mapping = HashMap::new();
        let mut paired = HashSet::new();
        for m in &messages {
            let r = &rows[m.row];
            let winner = best[&(r.turn, m.key_text.clone())];
            mapping.insert((r.source, r.turn), positions[&winner]);
            paired.insert(r.id);
            if let Some(i) = m.reply {
                paired.insert(rows[i].id);
            }
        }
        for ((source, native), position) in &mapping {
            if bulk && native == position {
                // Fresh event rows already carry their native position.
                // Only actual replay collisions need a remapping write.
                continue;
            }
            tx.prepare_cached("UPDATE event_rows SET position=?4 WHERE source=?1 AND session_key IN (SELECT id FROM event_sessions WHERE session=?2) AND native_turn=?3 AND position IS NOT ?4")?.execute(params![source,session,native,position])?;
        }
        for i in &chosen {
            let m = &messages[*i];
            let position = positions[i];
            keep.insert(rows[m.row].id, position);
            if let Some(j) = m.reply {
                keep.insert(rows[j].id, position);
            }
        }
        for r in &rows {
            let position = keep.get(&r.id).copied();
            let retained = position.is_some() || !paired.contains(&r.id);
            set_visibility(
                tx,
                "prose",
                r.id,
                (r.visible, r.position),
                (retained, position.unwrap_or(r.turn)),
                changes,
                bulk,
            )?;
        }
        let tools:Vec<(i64,i64,i64,i64,bool,String)>=tx.prepare("SELECT t.id,t.source,t.turn,t.position,t.visible,t.extra FROM tools t JOIN sources s ON s.rowid=t.source WHERE t.agent=?1 AND t.session=?2 ORDER BY s.path,t.id")?.query_map(params![agent,session],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)))?.collect::<rusqlite::Result<_>>()?;
        let mut seen = HashSet::new();
        for (id, source, turn, old_position, old_visible, key) in tools {
            let retained = key.is_empty() || seen.insert(key);
            let position = mapping.get(&(source, turn)).copied().unwrap_or(turn);
            set_visibility(
                tx,
                "tools",
                id,
                (old_visible, old_position),
                (retained, position),
                changes,
                bulk,
            )?;
        }
        refresh_session_bounds(tx, agent, session)?;
        if let Some(m) = chosen
            .iter()
            .map(|i| &messages[*i])
            .min_by_key(|m| source_order(m))
        {
            let r = &rows[m.row];
            let claims: Vec<_> = chosen
                .iter()
                .map(|i| &messages[*i])
                .filter(|m| !m.parent.is_empty())
                .collect();
            let parent = claims
                .into_iter()
                .min_by_key(|m| source_order(m))
                .map_or("", |m| m.parent.as_str());
            let title = chosen
                .iter()
                .map(|i| &messages[*i])
                .filter(|m| !matches!(rows[m.row].who.as_str(), "agent" | "recap"))
                .filter(|m| !rows[m.row].text.trim().is_empty())
                .min_by_key(|m| source_order(m))
                .map(|m| session_title(&rows[m.row].text))
                .unwrap_or_default();
            tx.execute("UPDATE sessions SET project=?3,parent=?4,side=?5,title=?6 WHERE agent=?1 AND id=?2",params![agent,session,r.project,parent,m.side || !parent.is_empty(),title])?;
        }
    }
    Ok(())
}

fn remove_source(
    tx: &Transaction<'_>,
    table: &str,
    key: &str,
    changes: &mut Changes,
) -> Result<()> {
    let ids: Vec<i64> = tx
        .prepare(&format!(
            "SELECT id FROM {table} WHERE source=(SELECT rowid FROM sources WHERE path=?)"
        ))?
        .query_map([key], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    for id in ids {
        erase(tx, table, id, changes)?;
    }
    if table == "tools" {
        if let Some(source) = tx
            .query_row("SELECT rowid FROM sources WHERE path=?", [key], |r| {
                r.get::<_, i64>(0)
            })
            .optional()?
        {
            crate::tool_store::release_source(tx, source)?;
        }
        tx.execute(
            "DELETE FROM parser_graph WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
        tx.execute(
            "DELETE FROM event_rows WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
    } else {
        tx.execute(
            "DELETE FROM session_sources WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
        tx.execute(
            "DELETE FROM reply_shapes WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
    }
    Ok(())
}
fn erase(tx: &Transaction<'_>, t: &str, id: i64, changes: &mut Changes) -> Result<()> {
    changes.fts.delete(t, id);
    tx.execute(&format!("DELETE FROM {t} WHERE id=?"), [id])?;
    changes.touch(t, id);
    Ok(())
}
struct NewRow<'a> {
    agent: &'a str,
    session: &'a str,
    project: &'a str,
    turn: i64,
    who: &'a str,
    model: &'a str,
    model_source: &'a str,
    ts: i64,
    text: &'a str,
    extra: String,
    bulk: bool,
}
#[derive(Default)]
struct BulkRows {
    lanes: [Vec<Vec<rusqlite::types::Value>>; 2],
}
impl BulkRows {
    fn flush(&mut self, tx: &Transaction<'_>, lane: usize, changes: &mut Changes) -> Result<()> {
        let rows = &mut self.lanes[lane];
        if rows.is_empty() {
            return Ok(());
        }
        let _timer = WriteTimer::new(false);
        let table = if lane == 0 { "prose" } else { "tools" };
        let sql=format!("INSERT INTO {table}(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES {} ON CONFLICT(source,session,turn,who,extra) DO UPDATE SET project=excluded.project,model=excluded.model,model_source=excluded.model_source,ts=excluded.ts,text=excluded.text,digest=excluded.digest RETURNING id",vec!["(?,?,?,?,?,?,?,?,?,?,?,?,?)";rows.len()].join(","));
        let mut stmt = tx.prepare_cached(&sql)?;
        for id in stmt.query_map(rusqlite::params_from_iter(rows.iter().flatten()), |r| {
            r.get::<_, i64>(0)
        })? {
            let id = id?;
            changes.touch(table, id);
        }
        rows.clear();
        Ok(())
    }
    fn write(
        &mut self,
        tx: &Transaction<'_>,
        table: &str,
        source: i64,
        row: NewRow<'_>,
        changes: &mut Changes,
    ) -> Result<usize> {
        use rusqlite::types::Value;
        if !row.bulk {
            return upsert(tx, table, source, row, changes);
        }
        let lane = usize::from(table == "tools");
        {
            let _timer = WriteTimer::new(false);
            let encoded = if lane == 1 {
                lz4_flex::compress_prepend_size(row.text.as_bytes())
            } else {
                row.text.as_bytes().to_vec()
            };
            self.lanes[lane].push(vec![
                Value::Integer(source),
                row.agent.to_owned().into(),
                row.session.to_owned().into(),
                row.project.to_owned().into(),
                row.turn.into(),
                row.who.to_owned().into(),
                row.model.to_owned().into(),
                row.model_source.to_owned().into(),
                row.ts.into(),
                encoded.into(),
                digest(row.text).into(),
                row.extra.into(),
                row.turn.into(),
            ]);
        }
        if self.lanes[lane].len() >= 64 {
            self.flush(tx, lane, changes)?;
        }
        Ok(1)
    }
}
#[allow(clippy::too_many_arguments)]
fn store_event(
    tx: &Transaction<'_>,
    writer: &mut crate::tool_store::Writer,
    source: i64,
    event: &crate::model::Event,
    call_id: &str,
    turn: i64,
    separator: Option<usize>,
    project: &str,
    text: Option<crate::tool_store::Text<'_>>,
    bulk: bool,
    changes: &mut Changes,
) -> Result<usize> {
    let _timer = WriteTimer::new(false);
    let changed = writer.event(
        tx, source, event, call_id, turn, separator, project, text, bulk,
    )?;
    if changed.text_changed && !bulk {
        if changed.old_visible {
            changes.fts.delete("tools", changed.old_id.unwrap());
        }
        if changed.has_text {
            changes.fts.insert("tools", changed.id);
        }
    }
    if changed.dirty {
        changes.touch("tools", changed.id);
    }
    Ok(usize::from(changed.text_changed))
}
fn upsert(
    tx: &Transaction<'_>,
    table: &str,
    source: i64,
    r: NewRow<'_>,
    changes: &mut Changes,
) -> Result<usize> {
    let _timer = WriteTimer::new(false);
    let dg = digest(r.text);
    if r.bulk {
        let encoded = if table == "tools" {
            lz4_flex::compress_prepend_size(r.text.as_bytes())
        } else {
            r.text.as_bytes().to_vec()
        };
        let id=tx.prepare_cached(&format!("INSERT INTO {table}(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?5) ON CONFLICT(source,session,turn,who,extra) DO UPDATE SET project=excluded.project,model=excluded.model,model_source=excluded.model_source,ts=excluded.ts,text=excluded.text,digest=excluded.digest RETURNING id"))?.query_row(params![source,r.agent,r.session,r.project,r.turn,r.who,r.model,r.model_source,r.ts,encoded,dg,r.extra],|row|row.get::<_,i64>(0))?;
        changes.touch(table, id);
        return Ok(1);
    }
    type StoredRow = (i64, String, i64, String, String, String, bool);
    let old:Option<StoredRow>=tx.prepare_cached(&format!("SELECT id,digest,ts,project,model,model_source,visible FROM {table} WHERE source=?1 AND session=?2 AND turn=?3 AND who=?4 AND extra=?5"))?.query_row(params![source,r.session,r.turn,r.who,r.extra],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
    if let Some((id, ref old_dg, old_ts, ref project, ref model, ref model_source, visible)) = old {
        if *old_dg == dg {
            if old_ts == r.ts
                && project == r.project
                && model == r.model
                && model_source == r.model_source
            {
                return Ok(0);
            }
            if old_ts != r.ts {
                changes.touch(table, id);
            }
            tx.execute(
                &format!(
                    "UPDATE {table} SET project=?2,model=?3,model_source=?4,ts=?5 WHERE id=?1"
                ),
                params![id, r.project, r.model, r.model_source, r.ts],
            )?;
            return Ok(0);
        }
        let encoded = if table == "tools" {
            lz4_flex::compress_prepend_size(r.text.as_bytes())
        } else {
            r.text.as_bytes().to_vec()
        };
        if visible {
            changes.fts.delete(table, id);
        }
        tx.prepare_cached(&format!("UPDATE {table} SET project=?2,model=?3,model_source=?4,ts=?5,text=?6,digest=?7 WHERE id=?1"))?.execute(params![id,r.project,r.model,r.model_source,r.ts,encoded,dg])?;
        if visible {
            changes.fts.insert(table, id);
        }
        changes.touch(table, id);
        return Ok(1);
    }
    let encoded = if table == "tools" {
        lz4_flex::compress_prepend_size(r.text.as_bytes())
    } else {
        r.text.as_bytes().to_vec()
    };
    tx.prepare_cached(&format!("INSERT INTO {table}(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?5)"))?.execute(params![source,r.agent,r.session,r.project,r.turn,r.who,r.model,r.model_source,r.ts,encoded,dg,r.extra])?;
    let id = tx.last_insert_rowid();
    changes.fts.insert(table, id);
    changes.touch(table, id);
    Ok(1)
}
/// `prepared` holds the texts of `s.events` by index, where they were prepared off the
/// writer's thread; the rest are formatted here.
fn replace(
    tx: &Transaction<'_>,
    s: &SourceBatch,
    mut prepared: Vec<Option<crate::tool_store::Prepared>>,
    changes: &mut Changes,
    bulk: bool,
) -> Result<usize> {
    tx.execute(
        "INSERT OR IGNORE INTO sources(path,agent,size,mtime_ns,change_token) VALUES(?1,?2,0,0,x'02')",
        params![s.key, s.agent],
    )?;
    let source: i64 = tx.query_row("SELECT rowid FROM sources WHERE path=?", [&s.key], |r| {
        r.get(0)
    })?;
    if !s.append && (s.agent != "pi" || s.reset_nodes) {
        crate::tool_store::release_source(tx, source)?;
    }
    if !s.append {
        tx.execute("DELETE FROM session_sources WHERE source=?", [source])?;
    }
    let members: HashSet<_> = s
        .messages
        .iter()
        .map(|m| (m.agent, m.session.as_ref()))
        .chain(s.events.iter().map(|e| (e.agent, e.session.as_str())))
        .collect();
    let mut link = tx.prepare_cached("INSERT OR IGNORE INTO session_sources VALUES(?1,?2,?3)")?;
    for (agent, session) in members {
        link.execute(params![agent, session, source])?;
    }
    let mut keys: HashSet<(String, i64, String, String)> = HashSet::new();
    let mut count = 0;
    let mut bulk_rows = BulkRows::default();
    let mut event_writer = crate::tool_store::Writer::default();
    let mut meta: HashMap<&str, (&str, i64, bool, &str)> = HashMap::new();
    let mut initial_bounds: HashMap<&str, (i64, i64, u32, String)> = HashMap::new();
    if bulk {
        for m in &s.messages {
            if m.text.is_empty() && m.reply.is_empty() {
                continue;
            }
            let title_turn =
                if !m.text.trim().is_empty() && !matches!(m.who.as_ref(), "agent" | "recap") {
                    m.turn
                } else {
                    u32::MAX
                };
            let entry = initial_bounds.entry(&m.session).or_insert_with(|| {
                (
                    0,
                    0,
                    title_turn,
                    if title_turn == u32::MAX {
                        String::new()
                    } else {
                        session_title(&m.text)
                    },
                )
            });
            if m.ts > 0 {
                entry.0 = if entry.0 > 0 { entry.0.min(m.ts) } else { m.ts };
                entry.1 = entry.1.max(m.ts);
            }
            if title_turn < entry.2 {
                entry.2 = title_turn;
                entry.3 = session_title(&m.text);
            }
        }
    }
    let session_last: HashMap<&str, usize> = s
        .messages
        .iter()
        .enumerate()
        .map(|(i, m)| (m.session.as_ref(), i))
        .collect();
    let source_directory = source_cwd(&s.key);
    for (message_index, m) in s.messages.iter().enumerate() {
        meta.insert(&m.session, (&m.project, m.ts, m.side, &m.parent));
        let cwd = if m.project.starts_with('/') || m.project.contains(":\\") {
            m.project.to_string()
        } else {
            source_directory.clone()
        };
        if !bulk || session_last.get(m.session.as_ref()) == Some(&message_index) {
            session(tx, m, &cwd)?;
        }
        if s.append && !m.reply.is_empty() {
            let prior: Option<String> = tx
                .query_row(
                    "SELECT body FROM reply_shapes WHERE source=?1 AND session=?2 AND turn=?3",
                    params![source, m.session.as_ref(), m.turn],
                    |r| r.get(0),
                )
                .optional()?;
            let old_chars = prior
                .and_then(|v| serde_json::from_str::<serde_json::Value>(&v).ok())
                .and_then(|v| v.get("original_reply_chars").and_then(|v| v.as_u64()));
            if old_chars.is_some_and(|n| n < (m.reply_chars as u64)) {
                tx.execute("UPDATE event_rows SET meta=trim(replace(meta,'after_reply=1','')) WHERE source=?1 AND session_key IN (SELECT id FROM event_sessions WHERE session=?2) AND native_turn=?3 AND meta LIKE '%after_reply=1%'",params![source,m.session.as_ref(),m.turn])?;
            }
        }
        tx.prepare_cached("INSERT INTO reply_shapes VALUES(?1,?2,?3,?4) ON CONFLICT(source,session,turn) DO UPDATE SET body=excluded.body WHERE body!=excluded.body")?.execute(params![source,m.session.as_ref(),m.turn,reply_metadata(m)?])?;
        for (who, text) in [
            (m.who.as_ref(), m.text.as_ref()),
            ("agent", m.reply.as_ref()),
        ] {
            if text.is_empty() {
                continue;
            }
            let extra = String::new();
            keys.insert((
                m.session.to_string(),
                m.turn as i64,
                who.to_string(),
                extra.clone(),
            ));
            count += bulk_rows.write(
                tx,
                "prose",
                source,
                NewRow {
                    agent: m.agent,
                    session: &m.session,
                    project: &m.project,
                    turn: m.turn as i64,
                    who,
                    model: &m.model,
                    model_source: &m.model_source,
                    ts: m.ts,
                    text,
                    extra,
                    bulk,
                },
                changes,
            )?;
        }
    }
    let message_keys: HashSet<_> = s
        .messages
        .iter()
        .map(|m| (m.session.to_string(), m.turn as i64))
        .collect();
    let stale_shapes: Vec<(String, i64)> = tx
        .prepare_cached("SELECT session,turn FROM reply_shapes WHERE source=?1 AND NOT ?2")?
        .query_map(params![source, s.append], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (session, turn) in stale_shapes {
        if !s.append && !message_keys.contains(&(session.clone(), turn)) {
            tx.execute(
                "DELETE FROM reply_shapes WHERE source=?1 AND session=?2 AND turn=?3",
                params![source, session, turn],
            )?;
        }
    }
    let mut metadata_keys = HashSet::new();
    let mut event_keys = HashSet::new();
    let mut event_turn: HashMap<&str, i64> = HashMap::new();
    let mut turns: HashMap<&str, Vec<(i64, i64)>> = HashMap::new();
    for m in &s.messages {
        turns
            .entry(&m.session)
            .or_default()
            .push((m.ts, m.turn as i64));
    }
    for v in turns.values_mut() {
        v.sort_unstable();
        let mut highest = 0;
        for (_, t) in v {
            highest = highest.max(*t);
            *t = highest;
        }
    }

    let mut anonymous: HashMap<String, u32> = HashMap::new();
    for (index, e) in s.events.iter().enumerate() {
        let call_id = if e.call_id.trim().is_empty() {
            let identity = digest(
                &serde_json::json!([
                    e.agent,
                    e.session,
                    e.ts,
                    e.kind,
                    e.name,
                    e.input,
                    e.output,
                    e.ok,
                    e.child_session,
                    e.meta
                ])
                .to_string(),
            );
            let occurrence = anonymous.entry(identity.clone()).or_default();
            let id = format!("agrep-anon-{}-{}", &identity[..16], occurrence);
            *occurrence += 1;
            id
        } else {
            e.call_id.clone()
        };
        if !metadata_keys.insert((e.session.clone(), call_id.clone())) {
            continue;
        }
        let (project, _, _, _) = meta
            .get(e.session.as_str())
            .copied()
            .unwrap_or(("unknown", 0, false, ""));
        let turn = e.turn.map(i64::from).unwrap_or_else(|| {
            turns
                .get(e.session.as_str())
                .and_then(|v| {
                    v.partition_point(|(ts, _)| *ts <= e.ts)
                        .checked_sub(1)
                        .map(|i| v[i].1)
                })
                .unwrap_or_else(|| *event_turn.get(e.session.as_str()).unwrap_or(&0))
        });
        event_turn.insert(&e.session, turn);
        if e.kind == "control" {
            store_event(
                tx,
                &mut event_writer,
                source,
                e,
                &call_id,
                turn,
                None,
                project,
                None,
                bulk,
                changes,
            )?;
            continue;
        }
        let event_call_id = call_id.clone();
        let extra = call_id;
        event_keys.insert((e.session.clone(), turn, "tool".into(), extra.clone()));
        let formatted;
        let (separator, text) = match prepared.get_mut(index).and_then(Option::take) {
            Some(p) => (p.separator, p.text),
            None => {
                let (text, separator) = crate::tool_text::format(e);
                formatted = text;
                (separator, crate::tool_store::Text::Plain(&formatted))
            }
        };
        count += store_event(
            tx,
            &mut event_writer,
            source,
            e,
            &event_call_id,
            turn,
            separator,
            project,
            Some(text),
            bulk,
            changes,
        )?;
        if !bulk || !meta.contains_key(e.session.as_str()) {
            tx.prepare_cached("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES(?1,?2,?3,'',?2,0,'',0,0) ON CONFLICT(agent,id) DO NOTHING")?.execute(params![e.agent,e.session,project])?;
        }
    }
    bulk_rows.flush(tx, 0, changes)?;
    bulk_rows.flush(tx, 1, changes)?;
    for (session, (first, last, _, title)) in initial_bounds {
        tx.prepare_cached(
            "UPDATE sessions SET first_ts=?3,last_ts=?4,title=?5 WHERE agent=?1 AND id=?2",
        )?
        .execute(params![s.agent, session, first, last, title])?;
    }
    if bulk {
        // The first build contains no previous source contributions. Besides
        // avoiding pointless diffs, this must precede inactive-row cleanup:
        // its source index is intentionally deferred until bulk loading ends.
        return Ok(count);
    }
    for (t, set) in [("prose", keys), ("tools", event_keys)] {
        let stale: Vec<i64> = tx
            .prepare(&format!(
                "SELECT id,session,turn,who,extra FROM {t} WHERE source=?1 AND NOT ?2"
            ))?
            .query_map(params![source, s.append], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?),
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?
            .into_iter()
            .filter(|(_, k)| !s.append && !set.contains(k))
            .map(|(id, _)| id)
            .collect();
        for id in stale {
            erase(tx, t, id, changes)?;
            count += 1;
        }
    }
    let stale_events: Vec<(String, String)> = tx
        .prepare_cached("SELECT session,call_id FROM events WHERE source=?1 AND NOT ?2")?
        .query_map(params![source, s.append], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (session, call_id) in stale_events {
        if !s.append && !metadata_keys.contains(&(session.clone(), call_id.clone())) {
            tx.execute(
                "UPDATE event_rows SET active=0 WHERE source=?1 AND session_key IN (SELECT id FROM event_sessions WHERE session=?2) AND call_id=?3 AND active=1",
                params![source, session, call_id],
            )?;
        }
    }
    if !s.append && (s.agent != "pi" || s.reset_nodes) {
        tx.execute(
            "DELETE FROM event_rows WHERE source=? AND active=0",
            [source],
        )?;
    }
    Ok(count)
}
fn session(tx: &Transaction<'_>, m: &Message, cwd: &str) -> Result<()> {
    let title = if matches!(m.who.as_ref(), "agent" | "recap") {
        String::new()
    } else {
        session_title(&m.text)
    };
    let family = if m.parent.is_empty() {
        m.session.as_ref()
    } else {
        m.parent.as_ref()
    };
    tx.prepare_cached("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?8) ON CONFLICT(agent,id) DO UPDATE SET project=?3,parent=?4,side=?6,first_ts=CASE WHEN ?8=0 THEN first_ts WHEN first_ts=0 THEN ?8 ELSE MIN(first_ts,?8) END,last_ts=MAX(last_ts,?8),title=CASE WHEN ?9=0 OR title='' THEN ?7 ELSE title END")?.execute(params![m.agent,m.session.as_ref(),m.project.as_ref(),m.parent.as_ref(),family,m.side,title,m.ts.max(0),m.turn])?;
    tx.prepare_cached("UPDATE sessions SET cwd=?3 WHERE agent=?1 AND id=?2 AND ?3!=''")?
        .execute(params![m.agent, m.session.as_ref(), cwd])?;
    Ok(())
}
fn source_cwd(path: &str) -> String {
    use std::io::Read;
    let Ok(file) = fs::File::open(path) else {
        return String::new();
    };
    let mut bytes = Vec::new();
    if path.ends_with(".gz") {
        let _ = flate2::read::GzDecoder::new(file)
            .take(16384)
            .read_to_end(&mut bytes);
    } else {
        let _ = file.take(16384).read_to_end(&mut bytes);
    }
    for line in bytes.split(|b| *b == b'\n').take(8) {
        if let Ok(v) = serde_json::from_slice::<serde_json::Value>(line) {
            for cwd in [v.get("cwd"), v.pointer("/payload/cwd")]
                .into_iter()
                .flatten()
            {
                if let Some(s) = cwd.as_str() {
                    return s.to_string();
                }
            }
        }
    }
    String::new()
}
pub fn grams(text: &str) -> HashMap<String, u32> {
    let cs: Vec<_> = text.chars().collect();
    let mut g = HashMap::new();
    for width in 1..=3 {
        for w in cs.windows(width) {
            *g.entry(w.iter().collect()).or_default() += 1;
        }
    }
    g
}
type BoundRow = (Vec<u8>, i64, String, String, i64);
struct BuiltBounds {
    key: i64,
    max_ts: i64,
    user_ts: Option<i64>,
    weight: f64,
    min_session: String,
    min_turn: i64,
    grams: Vec<(u32, GramStat)>,
    max_gap: usize,
    all_ascii: bool,
    append: bool,
}
#[derive(Clone, Copy, Debug, Default)]
struct GramStat {
    frequency: u32,
    max_ts: i64,
    weight: u8,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Posting {
    pub block: u32,
    pub frequency: u32,
    pub max_ts: i64,
    pub weight: u8,
}
fn weight_code(who: &str) -> u8 {
    match who {
        "tool" => 0,
        "harness" | "synthetic" => 1,
        "recap" | "control" => 2,
        "subagent" => 4,
        "agent" => 5,
        "user" => 6,
        _ => 3,
    }
}
pub(crate) fn posting_weight(code: u8) -> f64 {
    [0.22, 0.3, 0.325, 0.5, 0.7225, 0.8, 1.][usize::from(code)]
}
fn gram_maxima(texts: impl Iterator<Item = (String, i64, String)>) -> HashMap<u32, GramStat> {
    // The dense key also records only metadata of rows containing this gram.
    // A recent non-match must not raise the recency ceiling of old matches.
    let mut counts = vec![(usize::MAX, 0u32, GramStat::default()); 47988];
    let mut touched = Vec::new();
    for (row, (text, ts, who)) in texts.enumerate() {
        let code = weight_code(&who);
        let mut observe = |key: usize| {
            let (previous, current, stat) = &mut counts[key];
            if stat.frequency == 0 {
                touched.push(key as u32);
            }
            if *previous != row {
                *previous = row;
                *current = 0;
                stat.max_ts = stat.max_ts.max(ts);
                stat.weight = stat.weight.max(code);
            }
            *current += 1;
            stat.frequency = stat.frequency.max(*current);
        };
        let (mut rolling, mut run) = (0usize, 0usize);
        for byte in text.bytes() {
            let digit = match byte {
                b'0'..=b'9' => usize::from(byte - b'0'),
                b'a'..=b'z' => 10 + usize::from(byte - b'a'),
                _ => {
                    rolling = 0;
                    run = 0;
                    continue;
                }
            };
            rolling = (rolling % 1296) * 36 + digit;
            run = (run + 1).min(3);
            observe(digit);
            if run >= 2 {
                observe(36 + rolling % 1296);
            }
            if run == 3 {
                observe(1332 + rolling);
            }
        }
    }
    touched
        .into_iter()
        .map(|key| (key, counts[key as usize].2))
        .collect()
}

fn build_bounds(key: i64, rows: Vec<BoundRow>) -> Result<BuiltBounds> {
    let table = if key < 0 { "tools" } else { "prose" };
    let mut max_ts = 0;
    let mut user_ts = None;
    let mut weight: f64 = 0.;
    let mut min_session = String::new();
    let mut min_turn = i64::MAX;
    let mut texts = Vec::with_capacity(rows.len());
    let mut max_gap = 0;
    let mut all_ascii = true;
    for (text, ts, who, session, turn) in rows {
        if min_session.is_empty() || session < min_session {
            min_session = session.clone();
            min_turn = turn;
        }
        if session == min_session {
            min_turn = min_turn.min(turn);
        }
        max_ts = max_ts.max(ts);
        weight = weight.max(crate::query::weight(&who));
        if who == "user" {
            user_ts = Some(user_ts.unwrap_or(0).max(ts));
        }
        let decoded = decode(table, text)?;
        all_ascii &= decoded.is_ascii();
        let text = search_fold(&decoded);
        max_gap = max_gap.max(crate::fts_span::gap_upper(&text));
        texts.push((text, ts, who));
    }
    let mut grams: Vec<_> = gram_maxima(texts.into_iter()).into_iter().collect();
    grams.sort_unstable_by_key(|a| a.0);
    Ok(BuiltBounds {
        key,
        max_ts,
        user_ts,
        weight,
        min_session,
        min_turn,
        grams,
        max_gap,
        all_ascii,
        append: false,
    })
}
/// Dense ASCII keys cover common keyword bounds; other text retains a
/// conservative universal ceiling and its complete FTS/verification path.
fn gram_code(gram: &str) -> Option<u32> {
    if !(1..=3).contains(&gram.len()) {
        return None;
    }
    let mut value = 0;
    for b in gram.bytes() {
        let digit = match b {
            b'0'..=b'9' => u32::from(b - b'0'),
            b'a'..=b'z' => 10 + u32::from(b - b'a'),
            _ => return None,
        };
        value = value * 36 + digit;
    }
    Some(
        value
            + match gram.len() {
                1 => 0,
                2 => 36,
                _ => 1332,
            },
    )
}
pub(crate) fn indexed_gram(gram: &str) -> bool {
    gram_code(gram).is_some()
}
#[cfg(test)]
fn gram_text(code: u32) -> Result<String> {
    let (mut n, len) = if code < 36 {
        (code, 1)
    } else if code < 1332 {
        (code - 36, 2)
    } else if code < 47988 {
        (code - 1332, 3)
    } else {
        anyhow::bail!("invalid gram key")
    };
    let mut out = vec![0; len];
    for byte in out.iter_mut().rev() {
        let d = (n % 36) as u8;
        *byte = if d < 10 { b'0' + d } else { b'a' + d - 10 };
        n /= 36;
    }
    Ok(String::from_utf8(out)?)
}
pub(crate) fn put_varint(mut n: u32, out: &mut Vec<u8>) {
    while n >= 128 {
        out.push(n as u8 | 128);
        n >>= 7;
    }
    out.push(n as u8);
}
pub(crate) fn take_varint(bytes: &[u8], at: &mut usize) -> Result<u32> {
    let mut n = 0;
    for shift in (0..=28).step_by(7) {
        let b = *bytes
            .get(*at)
            .ok_or_else(|| anyhow::anyhow!("truncated integer list"))?;
        *at += 1;
        anyhow::ensure!(shift != 28 || b < 16, "integer list overflow");
        n |= u32::from(b & 127) << shift;
        if b < 128 {
            return Ok(n);
        }
    }
    anyhow::bail!("invalid integer list")
}
const GRAM_BITMAP_BYTES: usize = 47988usize.div_ceil(8);
fn pack_grams(grams: &[(u32, GramStat)]) -> Vec<u8> {
    let mut bits = vec![0; GRAM_BITMAP_BYTES];
    for &(key, _) in grams {
        bits[key as usize / 8] |= 1 << (key % 8);
    }
    lz4_flex::compress_prepend_size(&bits)
}
fn gram_bitmap(bytes: &[u8]) -> Result<Vec<u8>> {
    let bits = lz4_flex::decompress_size_prepended(bytes)?;
    anyhow::ensure!(
        bits.len() == GRAM_BITMAP_BYTES && bits[GRAM_BITMAP_BYTES - 1] & 0xf0 == 0,
        "invalid gram bitmap"
    );
    Ok(bits)
}
fn bitmap_grams(bits: &[u8]) -> impl Iterator<Item = u32> + '_ {
    bits.iter().enumerate().flat_map(|(index, &byte)| {
        (0..8)
            .filter(move |bit| byte & (1 << bit) != 0)
            .map(move |bit| (index * 8 + bit) as u32)
    })
}
#[cfg(test)]
fn unpack_grams(bytes: &[u8]) -> Result<Vec<u32>> {
    Ok(bitmap_grams(&gram_bitmap(bytes)?).collect())
}
fn put_wide(mut n: u64, out: &mut Vec<u8>) {
    while n >= 128 {
        out.push(n as u8 | 128);
        n >>= 7;
    }
    out.push(n as u8);
}
fn take_wide(bytes: &[u8], at: &mut usize) -> Result<u64> {
    let mut n = 0;
    for shift in (0..=63).step_by(7) {
        let b = *bytes
            .get(*at)
            .ok_or_else(|| anyhow::anyhow!("truncated timestamp"))?;
        *at += 1;
        anyhow::ensure!(shift != 63 || b < 2, "timestamp overflow");
        n |= u64::from(b & 127) << shift;
        if b < 128 {
            return Ok(n);
        }
    }
    anyhow::bail!("invalid timestamp")
}
const POSTING_CHUNK: u32 = 64;
pub(crate) fn posting_list(c: &Connection, lane: i64, gram: &str) -> Result<Vec<Posting>> {
    let Some(key) = gram_code(gram) else {
        return Ok(vec![]);
    };
    // One statement keeps both halves on the same snapshot while a worker folds raises.
    let mut q = c.prepare_cached("SELECT 0,chunk,body,NULL,NULL,NULL FROM gram_postings WHERE lane=?1 AND gram=?2 UNION ALL SELECT 1,block,NULL,frequency,max_ts,weight FROM gram_raises WHERE lane=?1 AND gram=?2")?;
    let mut rows = q.query(params![lane, key])?;
    let mut base = Vec::new();
    let mut raises = Vec::new();
    while let Some(row) = rows.next()? {
        if row.get::<_, i64>(0)? == 0 {
            base.extend(decode_posting(row.get_ref(2)?.as_blob()?)?);
        } else {
            let posting = Posting {
                block: row.get(1)?,
                frequency: row.get(3)?,
                max_ts: row.get(4)?,
                weight: row.get(5)?,
            };
            anyhow::ensure!(
                posting.frequency > 0 && posting.max_ts >= 0 && posting.weight <= 6,
                "invalid gram raise"
            );
            raises.push(posting);
        }
    }
    if base.windows(2).any(|p| p[0].block > p[1].block) {
        base.sort_unstable_by_key(|p| p.block);
    }
    if raises.is_empty() {
        return Ok(base);
    }
    raises.sort_unstable_by_key(|p| p.block);
    let mut out = Vec::with_capacity(base.len() + raises.len());
    let mut base = base.into_iter().peekable();
    for mut raised in raises {
        while base.peek().is_some_and(|p| p.block < raised.block) {
            out.push(base.next().expect("posting before raise"));
        }
        if base.peek().is_some_and(|p| p.block == raised.block) {
            let prior = base.next().expect("same posting");
            raised.frequency = raised.frequency.max(prior.frequency);
            raised.max_ts = raised.max_ts.max(prior.max_ts);
            raised.weight = raised.weight.max(prior.weight);
        }
        out.push(raised);
    }
    out.extend(base);
    Ok(out)
}

fn posting_chunk(c: &Connection, lane: i64, key: u32, chunk: u32) -> Result<Vec<Posting>> {
    let bytes: Option<Vec<u8>> = c
        .prepare_cached("SELECT body FROM gram_postings WHERE lane=?1 AND gram=?2 AND chunk=?3")?
        .query_row(params![lane, key, chunk], |r| r.get(0))
        .optional()?;
    bytes.map_or_else(|| Ok(vec![]), |bytes| decode_posting(&bytes))
}
fn decode_posting(bytes: &[u8]) -> Result<Vec<Posting>> {
    let packed = lz4_flex::decompress_size_prepended(bytes)?;
    let data = packed
        .strip_prefix(b"R1")
        .ok_or_else(|| anyhow::anyhow!("invalid posting format"))?;
    let (mut at, mut last, mut timestamp, mut rows) = (0, 0u32, 0i64, Vec::new());
    while at < data.len() {
        last = last
            .checked_add(take_varint(data, &mut at)?)
            .ok_or_else(|| anyhow::anyhow!("block id overflow"))?;
        let frequency = take_varint(data, &mut at)?;
        anyhow::ensure!(frequency > 0, "zero gram frequency");
        let zigzag = take_wide(data, &mut at)?;
        let delta = (zigzag >> 1) as i64 ^ -((zigzag & 1) as i64);
        timestamp = timestamp
            .checked_add(delta)
            .ok_or_else(|| anyhow::anyhow!("timestamp overflow"))?;
        anyhow::ensure!(timestamp >= 0, "negative posting timestamp");
        let weight = *data
            .get(at)
            .ok_or_else(|| anyhow::anyhow!("missing posting weight"))?;
        at += 1;
        anyhow::ensure!(weight <= 6, "invalid posting weight");
        rows.push(Posting {
            block: last,
            frequency,
            max_ts: timestamp,
            weight,
        });
    }
    Ok(rows)
}
#[cfg(test)]
fn write_posting(tx: &Transaction<'_>, lane: i64, gram: &str, rows: &[Posting]) -> Result<()> {
    let mut at = 0;
    while at < rows.len() {
        let chunk = rows[at].block / POSTING_CHUNK;
        let end = at + rows[at..].partition_point(|r| r.block / POSTING_CHUNK == chunk);
        write_posting_chunk(
            tx,
            lane,
            gram_code(gram).expect("test gram"),
            chunk,
            &rows[at..end],
        )?;
        at = end;
    }
    Ok(())
}
fn write_posting_chunk(
    tx: &Transaction<'_>,
    lane: i64,
    key: u32,
    chunk: u32,
    rows: &[Posting],
) -> Result<()> {
    if rows.is_empty() {
        tx.execute(
            "DELETE FROM gram_postings WHERE lane=?1 AND gram=?2 AND chunk=?3",
            params![lane, key, chunk],
        )?;
        return Ok(());
    }
    let (mut body, mut last, mut timestamp) = (b"R1".to_vec(), 0, 0i64);
    for row in rows {
        put_varint(row.block - last, &mut body);
        put_varint(row.frequency, &mut body);
        let delta = row.max_ts - timestamp;
        put_wide(
            (delta as u64).wrapping_shl(1) ^ ((delta >> 63) as u64),
            &mut body,
        );
        body.push(row.weight);
        last = row.block;
        timestamp = row.max_ts;
    }
    tx.prepare_cached("INSERT INTO gram_postings VALUES(?1,?2,?3,?4) ON CONFLICT(lane,gram,chunk) DO UPDATE SET body=excluded.body")?.execute(params![lane,key,chunk,lz4_flex::compress_prepend_size(&body)])?;
    Ok(())
}

fn raise_changed_bounds(tx: &Transaction<'_>, changes: &mut Changes) -> Result<()> {
    for (table, dirty) in [("prose", "bound_updates"), ("tools", "tool_bound_updates")] {
        let mut query = tx.prepare_cached(&format!("SELECT id FROM {dirty}"))?;
        for id in query.query_map([], |r| r.get::<_, i64>(0))? {
            changes.touch(table, id?);
        }
    }
    let mut enqueue = tx.prepare_cached("INSERT OR IGNORE INTO bound_rebuild VALUES(?)")?;
    for block in &changes.blocks {
        enqueue.execute([block])?;
    }
    // Old contributions may disappear, but keeping their maxima cannot hide a hit.
    update_blocks(tx, &changes.blocks, Some(&changes.bounds))
}

pub(crate) fn tighten_bounds_step(root: &DataRoot, c: &mut Connection) -> Result<bool> {
    let _lock = root.lock_behind_writers()?;
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let block: Option<i64> = tx
        .query_row("SELECT block FROM bound_rebuild LIMIT 1", [], |r| r.get(0))
        .optional()?;
    let Some(block) = block else { return Ok(false) };
    let watch = root.clone();
    tx.progress_handler(1000, Some(move || watch.writer_queued().unwrap_or(true)));
    let result = update_blocks(&tx, &BTreeSet::from([block]), None);
    tx.progress_handler(0, None::<fn() -> bool>);
    if let Err(error) = result {
        if error.downcast_ref::<rusqlite::Error>().is_some_and(|e| matches!(e, rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::OperationInterrupted)) {
            return Ok(true);
        }
        return Err(error);
    }
    let (lane, id) = if block < 0 {
        (1, -1 - block)
    } else {
        (0, block)
    };
    tx.execute(
        "DELETE FROM gram_raises WHERE lane=?1 AND block=?2",
        params![lane, id],
    )?;
    tx.execute("DELETE FROM bound_rebuild WHERE block=?", [block])?;
    tx.commit()?;
    Ok(true)
}

struct PostingChange {
    rows: Vec<Posting>,
    dirty: bool,
}

fn update_blocks(
    tx: &Transaction<'_>,
    blocks: &BTreeSet<i64>,
    delta: Option<&[BTreeSet<i64>; 2]>,
) -> Result<()> {
    let first: bool = !tx.query_row("SELECT EXISTS(SELECT 1 FROM gram_postings)", [], |r| {
        r.get::<_, bool>(0)
    })?;
    let mut changed: HashMap<(i64, u32, u32), PostingChange> = HashMap::new();
    let keys: Vec<_> = blocks.iter().copied().collect();
    for chunk in keys.chunks(32) {
        let mut input = vec![];
        for &key in chunk {
            let (table, block) = if key < 0 {
                ("tools", -1 - key)
            } else {
                ("prose", key)
            };
            let lane = usize::from(key < 0);
            let rows = if let Some(delta) = delta {
                let mut query = tx.prepare_cached(&format!(
                    "SELECT text,ts,who,session,position FROM {table} WHERE visible=1 AND id>=?1 AND id<?2"
                ))?;
                let mut rows = Vec::new();
                let mut ids = delta[lane]
                    .range(block * BLOCK..(block + 1) * BLOCK)
                    .copied()
                    .peekable();
                while let Some(lo) = ids.next() {
                    let mut end = lo + 1;
                    while ids.peek() == Some(&end) {
                        ids.next();
                        end += 1;
                    }
                    let mut selected = query.query(params![lo, end])?;
                    while let Some(row) = selected.next()? {
                        rows.push((
                            row.get(0)?,
                            row.get(1)?,
                            row.get(2)?,
                            row.get(3)?,
                            row.get(4)?,
                        ));
                    }
                }
                rows
            } else {
                tx.prepare_cached(&format!("SELECT text,ts,who,session,position FROM {table} WHERE visible=1 AND id>=?1 AND id<?2"))?.query_map(params![block*BLOCK,(block+1)*BLOCK],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?.collect::<rusqlite::Result<Vec<BoundRow>>>()?
            };
            if delta.is_some() && rows.is_empty() {
                continue;
            }
            let append = delta.is_some();
            input.push((key, rows, append));
        }
        let small =
            input.len() <= 1 || input.iter().map(|(_, rows, _)| rows.len()).sum::<usize>() <= 16;
        let build = |(key, rows, append)| {
            let mut bounds = build_bounds(key, rows)?;
            bounds.append = append;
            Ok::<_, anyhow::Error>(bounds)
        };
        let built: Vec<_> = if small {
            input.into_iter().map(build).collect()
        } else {
            input.into_par_iter().map(build).collect()
        };
        for b in built {
            let mut b = b?;
            if b.append && b.min_turn == i64::MAX {
                continue;
            }
            let (lane, metadata, id) = if b.key < 0 {
                (1, "tool_blocks", -1 - b.key)
            } else {
                (0, "blocks", b.key)
            };
            let old: Option<Vec<u8>> = tx
                .query_row(
                    "SELECT body FROM block_grams WHERE lane=?1 AND block=?2",
                    params![lane, id],
                    |r| r.get(0),
                )
                .optional()?;
            let mut presence =
                old.map_or_else(|| Ok(vec![0; GRAM_BITMAP_BYTES]), |body| gram_bitmap(&body))?;
            let old: Vec<_> = if b.append {
                Vec::new()
            } else {
                bitmap_grams(&presence).collect()
            };
            let new: HashMap<_, _> = b.grams.iter().cloned().collect();
            let grams: HashSet<_> = if b.append {
                new.keys().cloned().collect()
            } else {
                old.iter().cloned().chain(new.keys().cloned()).collect()
            };
            for gram in grams {
                if b.append {
                    if let Some(tf) = new.get(&gram) {
                        tx.prepare_cached("INSERT INTO gram_raises VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(lane,gram,block) DO UPDATE SET frequency=max(frequency,excluded.frequency),max_ts=max(max_ts,excluded.max_ts),weight=max(weight,excluded.weight) WHERE frequency<excluded.frequency OR max_ts<excluded.max_ts OR weight<excluded.weight")?.execute(params![lane,gram,id,tf.frequency,tf.max_ts,tf.weight])?;
                    }
                    continue;
                }

                let key = (lane, gram, id as u32 / POSTING_CHUNK);
                let PostingChange { rows, dirty } = match changed.entry(key) {
                    std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
                    std::collections::hash_map::Entry::Vacant(entry) => {
                        let rows = if first {
                            Vec::new()
                        } else {
                            posting_chunk(tx, lane, key.1, key.2)?
                        };
                        entry.insert(PostingChange { rows, dirty: false })
                    }
                };
                if first {
                    if let Some(tf) = new.get(&key.1) {
                        *dirty = true;
                        rows.push(Posting {
                            block: id as u32,
                            frequency: tf.frequency,
                            max_ts: tf.max_ts,
                            weight: tf.weight,
                        });
                    }
                    continue;
                }
                match rows.binary_search_by_key(&(id as u32), |p| p.block) {
                    Ok(i) => {
                        if let Some(tf) = new.get(&key.1) {
                            let prior = rows[i];
                            let next = Posting {
                                block: id as u32,
                                frequency: if b.append {
                                    prior.frequency.max(tf.frequency)
                                } else {
                                    tf.frequency
                                },
                                max_ts: if b.append {
                                    prior.max_ts.max(tf.max_ts)
                                } else {
                                    tf.max_ts
                                },
                                weight: if b.append {
                                    prior.weight.max(tf.weight)
                                } else {
                                    tf.weight
                                },
                            };
                            *dirty |= next != prior;
                            rows[i] = next;
                        } else {
                            *dirty = true;
                            rows.remove(i);
                        }
                    }
                    Err(i) => {
                        if let Some(tf) = new.get(&key.1) {
                            *dirty = true;
                            rows.insert(
                                i,
                                Posting {
                                    block: id as u32,
                                    frequency: tf.frequency,
                                    max_ts: tf.max_ts,
                                    weight: tf.weight,
                                },
                            );
                        }
                    }
                }
            }
            if b.append {
                type PriorBound = (i64, Option<i64>, f64, String, i64, i64, bool);
                let prior: Option<PriorBound> = tx.query_row(&format!("SELECT max_ts,user_ts,weight,min_session,min_turn,max_gap,all_ascii FROM {metadata} WHERE id=?"),[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
                if let Some((ts, user_ts, weight, session, turn, gap, ascii)) = prior {
                    b.max_ts = b.max_ts.max(ts);
                    b.user_ts = b.user_ts.max(user_ts);
                    b.weight = b.weight.max(weight);
                    b.max_gap = b.max_gap.max(gap as usize);
                    b.all_ascii &= ascii;
                    if session < b.min_session {
                        b.min_session = session;
                        b.min_turn = turn;
                    } else if session == b.min_session {
                        b.min_turn = b.min_turn.min(turn);
                    }
                }
            }
            let packed = if b.append {
                for &(key, _) in &b.grams {
                    presence[key as usize / 8] |= 1 << (key % 8);
                }
                lz4_flex::compress_prepend_size(&presence)
            } else {
                pack_grams(&b.grams)
            };
            tx.prepare_cached("INSERT INTO block_grams VALUES(?1,?2,?3) ON CONFLICT(lane,block) DO UPDATE SET body=excluded.body")?.execute(params![lane,id,packed])?;
            tx.execute(&format!("DELETE FROM {metadata} WHERE id=?"), [id])?;
            if b.min_turn != i64::MAX {
                tx.prepare_cached(&format!(
                    "INSERT INTO {metadata} VALUES(?1,?2,?3,?4,?5,?6,?7,?8)"
                ))?
                .execute(params![
                    id,
                    b.max_ts,
                    b.user_ts,
                    b.weight,
                    b.min_session,
                    b.min_turn,
                    b.max_gap as i64,
                    b.all_ascii
                ])?;
            }
        }
    }
    let mut changed: Vec<_> = changed.into_iter().collect();
    changed.sort_unstable_by_key(|a| a.0);
    for ((lane, gram, chunk), PostingChange { mut rows, dirty }) in changed {
        if !dirty {
            continue;
        }
        if first {
            rows.sort_unstable_by_key(|r| r.block);
        }
        write_posting_chunk(tx, lane, gram, chunk, &rows)?;
    }
    Ok(())
}

pub fn counts(c: &Connection) -> Result<serde_json::Value> {
    Ok(
        serde_json::json!({"sessions":c.query_row("SELECT count(*) FROM sessions",[],|r|r.get::<_,i64>(0))?,"prose":c.query_row("SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_count'),0)",[],|r|r.get::<_,i64>(0))?,"tools":c.query_row("SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='tools_count'),0)",[],|r|r.get::<_,i64>(0))?,"source_errors":c.query_row("SELECT count(*) FROM sources WHERE last_error IS NOT NULL",[],|r|r.get::<_,i64>(0))?}),
    )
}

fn family_alias(tx: &Transaction<'_>, agent: &str, alias: &str) -> Result<Option<String>> {
    if tx.query_row("SELECT EXISTS(SELECT 1 FROM sessions s WHERE s.agent=?1 AND s.id=?2 AND EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=s.agent AND p.session=s.id))", params![agent,alias], |r| r.get::<_,bool>(0))? { return Ok(None); }
    let mut q = tx.prepare_cached(
        "SELECT session FROM family_alias_claims WHERE agent=?1 AND alias=?2 LIMIT 2",
    )?;
    let claims: Vec<String> = q
        .query_map(params![agent, alias], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    let [session] = claims.as_slice() else {
        return Ok(None);
    };
    // The original alias pass chose the first unambiguous filename per session.
    let first: Option<String> = tx.query_row("SELECT min(c.alias) FROM family_alias_claims c WHERE c.agent=?1 AND c.session=?2 AND NOT EXISTS(SELECT 1 FROM family_alias_claims d WHERE d.agent=c.agent AND d.alias=c.alias AND d.session!=c.session) AND NOT EXISTS(SELECT 1 FROM sessions s WHERE s.agent=c.agent AND s.id=c.alias AND EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=s.agent AND p.session=s.id))", params![agent,session], |r| r.get(0))?;
    Ok((first.as_deref() == Some(alias)).then(|| session.clone()))
}
fn resolve_families(tx: &Transaction<'_>, changed: &HashSet<(String, String)>) -> Result<()> {
    let mut affected = HashSet::new();
    for (agent, id) in changed {
        let parent: Option<String> = tx.query_row("SELECT parent FROM sessions WHERE agent=?1 AND id=?2 AND EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=sessions.agent AND p.session=sessions.id)", params![agent,id], |r| r.get(0)).optional()?;
        let previous: Option<String> = tx
            .query_row(
                "SELECT parent FROM family_nodes WHERE agent=?1 AND id=?2",
                params![agent, id],
                |r| r.get(0),
            )
            .optional()?;
        let old_aliases: BTreeSet<String> = tx
            .prepare_cached("SELECT alias FROM family_alias_claims WHERE agent=?1 AND session=?2")?
            .query_map(params![agent, id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        let mut aliases = BTreeSet::new();
        if parent.is_some() {
            let mut q = tx.prepare_cached("SELECT s.path FROM session_sources ss JOIN sources s ON s.rowid=ss.source WHERE ss.agent=?1 AND ss.session=?2")?;
            for path in q.query_map(params![agent, id], |r| r.get::<_, String>(0))? {
                if let Some(alias) = registry::session_alias(agent, Path::new(&path?), id) {
                    aliases.insert(alias);
                }
            }
        }
        if parent == previous && aliases == old_aliases {
            continue;
        }
        affected.insert((agent.clone(), id.clone()));
        affected.extend(
            aliases
                .iter()
                .chain(&old_aliases)
                .map(|alias| (agent.clone(), alias.clone())),
        );
        // Another claim can change which alias is the first for its target.
        for alias in aliases
            .iter()
            .chain(&old_aliases)
            .chain(std::iter::once(id))
        {
            let targets: Vec<String> = tx
                .prepare_cached(
                    "SELECT session FROM family_alias_claims WHERE agent=?1 AND alias=?2",
                )?
                .query_map(params![agent, alias], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            for target in targets {
                let names: Vec<String> = tx
                    .prepare_cached(
                        "SELECT alias FROM family_alias_claims WHERE agent=?1 AND session=?2",
                    )?
                    .query_map(params![agent, target], |r| r.get(0))?
                    .collect::<rusqlite::Result<_>>()?;
                affected.extend(names.into_iter().map(|name| (agent.clone(), name)));
            }
        }
        tx.execute(
            "DELETE FROM family_alias_claims WHERE agent=?1 AND session=?2",
            params![agent, id],
        )?;
        for alias in aliases {
            tx.prepare_cached("INSERT INTO family_alias_claims VALUES(?1,?2,?3)")?
                .execute(params![agent, alias, id])?;
        }
        if let Some(parent) = parent {
            tx.prepare_cached("INSERT INTO family_nodes VALUES(?1,?2,?3) ON CONFLICT(agent,id) DO UPDATE SET parent=excluded.parent")?.execute(params![agent,id,parent])?;
        } else {
            tx.execute(
                "DELETE FROM family_nodes WHERE agent=?1 AND id=?2",
                params![agent, id],
            )?;
        }
    }
    let mut queue: Vec<_> = affected.iter().cloned().collect();
    while let Some((agent, id)) = queue.pop() {
        let children: Vec<String> = tx
            .prepare_cached("SELECT id FROM sessions WHERE agent=?1 AND parent=?2")?
            .query_map(params![agent, id], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        for child in children {
            let key = (agent.clone(), child);
            if affected.insert(key.clone()) {
                queue.push(key);
            }
        }
    }
    let mut memo: HashMap<(String, String), String> = HashMap::new();
    for (agent, id) in affected {
        let mut seen: Vec<String> = vec![];
        let mut cur = id.clone();
        let root = loop {
            if let Some(root) = memo.get(&(agent.clone(), cur.clone())) {
                break root.clone();
            }
            if let Some(at) = seen.iter().position(|s| s == &cur) {
                break seen[at..].iter().min().cloned().unwrap_or(cur);
            }
            seen.push(cur.clone());
            let parent: Option<String> = tx
                .query_row(
                    "SELECT parent FROM sessions WHERE agent=?1 AND id=?2",
                    params![agent, cur],
                    |r| r.get(0),
                )
                .optional()?;
            match parent.filter(|s| !s.is_empty()) {
                Some(p) => cur = family_alias(tx, &agent, &p)?.unwrap_or(p),
                None => break cur,
            }
        };
        for member in seen {
            memo.insert((agent.clone(), member), root.clone());
        }
        tx.prepare_cached("UPDATE sessions SET family=?3 WHERE agent=?1 AND id=?2 AND family!=?3")?
            .execute(params![agent, id, root])?;
    }
    Ok(())
}

#[cfg(test)]
mod family_delta_tests {
    use super::*;
    #[test]
    fn canonical_names_reconsider_other_aliases_of_the_masked_target() {
        let mut c = Connection::open_in_memory().unwrap();
        schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        for (id, parent, family) in [
            ("root", "", "root"),
            ("child", "zzz", "zzz"),
            ("aaa", "", "aaa"),
        ] {
            tx.execute(
                "INSERT INTO sessions VALUES('pi',?1,'p','',?2,?3,0,'title',0,0)",
                params![id, parent, family],
            )
            .unwrap();
            tx.execute("INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra) VALUES(0,'pi',?1,'p',0,'user','m',0,1,x'78','digest','')",[id]).unwrap();
        }
        tx.execute_batch("INSERT INTO family_nodes VALUES('pi','root',''),('pi','child','zzz'); INSERT INTO family_alias_claims VALUES('pi','aaa','root'),('pi','zzz','root');").unwrap();
        let changed = HashSet::from([("pi".into(), "aaa".into())]);
        resolve_families(&tx, &changed).unwrap();
        assert_eq!(
            tx.query_row("SELECT family FROM sessions WHERE id='child'", [], |r| {
                r.get::<_, String>(0)
            })
            .unwrap(),
            "root"
        );
        tx.execute("DELETE FROM prose WHERE session='aaa'", [])
            .unwrap();
        tx.execute("DELETE FROM sessions WHERE id='aaa'", [])
            .unwrap();
        resolve_families(&tx, &changed).unwrap();
        assert_eq!(
            tx.query_row("SELECT family FROM sessions WHERE id='child'", [], |r| {
                r.get::<_, String>(0)
            })
            .unwrap(),
            "zzz"
        );
    }
}

#[cfg(test)]
mod bound_tests {
    #[test]
    fn integer_bound_keys_and_postings_round_trip() {
        for code in 0..47988 {
            assert_eq!(
                super::gram_code(&super::gram_text(code).unwrap()),
                Some(code)
            );
        }
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        let rows = [
            (0, 1, 0, 0),
            (127, 4, 1_700_000_000_123, 6),
            (128, 130, 1, 3),
            (65_000, 2, i64::MAX, 5),
            (u32::MAX, 30, 0, 1),
        ]
        .map(|(block, frequency, max_ts, weight)| super::Posting {
            block,
            frequency,
            max_ts,
            weight,
        })
        .to_vec();
        super::write_posting(&tx, 0, "abc", &rows).unwrap();
        assert_eq!(super::posting_list(&tx, 0, "abc").unwrap(), rows);
        let grams = ["a", "00", "zzz"]
            .map(|s| {
                (
                    super::gram_code(s).unwrap(),
                    super::GramStat {
                        frequency: 1,
                        ..Default::default()
                    },
                )
            })
            .to_vec();
        let mut got: Vec<_> = super::unpack_grams(&super::pack_grams(&grams))
            .unwrap()
            .into_iter()
            .map(|code| super::gram_text(code).unwrap())
            .collect();
        got.sort();
        assert_eq!(got, vec!["00", "a", "zzz"]);
    }
    #[test]
    fn raised_postings_merge_in_block_order_and_keep_each_maximum() {
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        let base = [
            super::Posting {
                block: 0,
                frequency: 4,
                max_ts: 200,
                weight: 3,
            },
            super::Posting {
                block: 128,
                frequency: 1,
                max_ts: 500,
                weight: 6,
            },
        ];
        super::write_posting(&tx, 0, "a", &base).unwrap();
        let gram = super::gram_code("a").unwrap();
        for (block, frequency, ts, weight) in [(0, 3, 300, 6), (1, 2, 600, 0)] {
            tx.execute(
                "INSERT INTO gram_raises VALUES(0,?1,?2,?3,?4,?5)",
                rusqlite::params![gram, block, frequency, ts, weight],
            )
            .unwrap();
        }
        tx.execute_batch("PRAGMA reverse_unordered_selects=ON")
            .unwrap();
        assert_eq!(
            super::posting_list(&tx, 0, "a").unwrap(),
            vec![
                super::Posting {
                    block: 0,
                    frequency: 4,
                    max_ts: 300,
                    weight: 6
                },
                super::Posting {
                    block: 1,
                    frequency: 2,
                    max_ts: 600,
                    weight: 0
                },
                base[1],
            ]
        );
        tx.execute("INSERT INTO gram_raises VALUES(1,?1,8,2,100,0)", [gram])
            .unwrap();
        assert_eq!(
            super::posting_list(&tx, 1, "a").unwrap(),
            vec![super::Posting {
                block: 8,
                frequency: 2,
                max_ts: 100,
                weight: 0
            }]
        );
    }

    fn bound_row(text: &str, turn: i64, ts: i64) -> super::NewRow<'_> {
        super::NewRow {
            agent: "pi",
            session: "s",
            project: "p",
            turn,
            who: "user",
            model: "",
            model_source: "unknown",
            ts,
            text,
            extra: String::new(),
            bulk: false,
        }
    }

    #[test]
    fn raising_bounds_reads_only_changed_rows_without_rewriting_base_postings() {
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let tx = c.transaction().unwrap();
        let mut changes = super::Changes::default();
        super::upsert(&tx, "prose", 1, bound_row("aaaa", 0, 200), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("old", 1, 100), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("ccc", 2, 200), &mut changes).unwrap();
        super::update_blocks(&tx, &changes.blocks, None).unwrap();
        tx.commit().unwrap();
        // An unrelated row must never be decoded to raise one changed row's bounds.
        c.execute("UPDATE prose SET text=x'ff' WHERE turn=1", [])
            .unwrap();
        c.execute("DELETE FROM bound_updates", []).unwrap();
        c.execute_batch("CREATE TRIGGER reject_unchanged_posting BEFORE INSERT ON gram_postings BEGIN SELECT RAISE(FAIL,'unchanged posting write'); END;").unwrap();
        let tx = c.transaction().unwrap();
        let mut changes = super::Changes::default();
        super::upsert(&tx, "prose", 1, bound_row("aaa", 0, 100), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("c", 2, 100), &mut changes).unwrap();
        super::raise_changed_bounds(&tx, &mut changes).unwrap();
        let a = super::posting_list(&tx, 0, "a").unwrap();
        assert_eq!((a[0].frequency, a[0].max_ts), (4, 200));
        tx.execute_batch("DROP TRIGGER reject_unchanged_posting")
            .unwrap();
        assert_eq!(
            tx.query_row("SELECT count(*) FROM bound_rebuild", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
        super::upsert(&tx, "prose", 1, bound_row("bbbb Ω", 0, 300), &mut changes).unwrap();
        super::raise_changed_bounds(&tx, &mut changes).unwrap();
        let b = super::posting_list(&tx, 0, "b").unwrap();
        assert_eq!((b[0].frequency, b[0].max_ts), (4, 300));
        assert_eq!(super::posting_list(&tx, 0, "a").unwrap(), a);
        assert!(!tx
            .query_row("SELECT all_ascii FROM blocks WHERE id=0", [], |r| r
                .get::<_, bool>(0))
            .unwrap());
    }

    #[test]
    fn background_tightening_removes_deleted_contributions() {
        let temp = tempfile::tempdir().unwrap();
        let root = crate::data::DataRoot::open(temp.path().join("data"), true).unwrap();
        let mut c = super::writer(&root).unwrap();
        let tx = c.transaction().unwrap();
        let mut changes = super::Changes::default();
        super::upsert(&tx, "prose", 1, bound_row("aaaa", 0, 200), &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("bbb", 1, 100), &mut changes).unwrap();
        super::update_blocks(&tx, &changes.blocks, None).unwrap();
        let mut changes = super::Changes::default();
        super::erase(&tx, "prose", 1, &mut changes).unwrap();
        super::upsert(&tx, "prose", 1, bound_row("bbbb", 1, 300), &mut changes).unwrap();
        super::raise_changed_bounds(&tx, &mut changes).unwrap();
        assert_eq!(super::posting_list(&tx, 0, "a").unwrap()[0].frequency, 4);
        tx.commit().unwrap();
        assert!(super::tighten_bounds_step(&root, &mut c).unwrap());
        assert!(!super::tighten_bounds_step(&root, &mut c).unwrap());
        assert!(super::posting_list(&c, 0, "a").unwrap().is_empty());
        assert_eq!(super::posting_list(&c, 0, "b").unwrap()[0].frequency, 4);
        assert_eq!(
            c.query_row("SELECT count(*) FROM gram_raises", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            c.query_row("SELECT max_ts FROM blocks WHERE id=0", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            300
        );
    }

    #[test]
    fn counts_are_per_row_not_summed_across_a_block() {
        let got = super::gram_maxima(
            ["aaaa", "aa", "ééé"]
                .into_iter()
                .map(|s| (s.to_owned(), 1, "user".to_owned())),
        );
        assert_eq!(got[&super::gram_code("a").unwrap()].frequency, 4);
        assert_eq!(got[&super::gram_code("aa").unwrap()].frequency, 3);
        assert_eq!(got[&super::gram_code("aaa").unwrap()].frequency, 2);
        assert_eq!(got.len(), 3);
    }
    #[test]
    fn dense_counts_match_literal_grams_across_unicode_and_punctuation() {
        let rows = [
            "abc abc0 000 zzzzzéabc a🦀bc １２３xyz",
            "bcabc a_b-a.b a\nabc 0123456789abcdefghijklmnopqrstuvwxyz",
            "é界Σ🦀",
            "9za9za9za",
        ];
        let mut expected = std::collections::HashMap::<String, u32>::new();
        for text in rows {
            for (gram, count) in super::grams(text) {
                if super::indexed_gram(&gram) {
                    expected
                        .entry(gram)
                        .and_modify(|max| *max = (*max).max(count))
                        .or_insert(count);
                }
            }
        }
        assert_eq!(
            super::gram_maxima(
                rows.into_iter()
                    .map(|s| (s.to_owned(), 1, "user".to_owned()))
            )
            .into_iter()
            .map(|(code, v)| (super::gram_text(code).unwrap(), v.frequency))
            .collect::<std::collections::HashMap<_, _>>(),
            expected
        );
    }
}

#[cfg(test)]
mod row_diff_tests {
    #[test]
    fn initial_source_writes_do_not_scan_preceding_sources() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let mut counts = Vec::new();
        for n in [128, 512] {
            let mut c = rusqlite::Connection::open_in_memory().unwrap();
            super::schema(&c).unwrap();
            super::search_indexes(&c, false).unwrap();
            let operations = Arc::new(AtomicUsize::new(0));
            let recorded = operations.clone();
            c.progress_handler(
                100,
                Some(move || {
                    recorded.fetch_add(100, Ordering::Relaxed);
                    false
                }),
            );
            let tx = c.transaction().unwrap();
            let mut changes = super::Changes::default();
            for i in 0..n {
                let source = crate::intake_batch::SourceBatch {
                    key: format!("source-{i}"),
                    agent: "pi".into(),
                    events: vec![crate::model::Event {
                        agent: "pi",
                        session: format!("session-{i}"),
                        ts: 1,
                        turn: Some(0),
                        kind: "tool",
                        name: "echo".into(),
                        input: "hello".into(),
                        output: String::new(),
                        input_chars: 5,
                        output_chars: 0,
                        output_bytes: 0,
                        ok: Some(true),
                        call_id: "call".into(),
                        child_session: String::new(),
                        meta: String::new(),
                        state: String::new(),
                    }],
                    ..Default::default()
                };
                assert_eq!(
                    super::replace(&tx, &source, Vec::new(), &mut changes, true).unwrap(),
                    1
                );
            }
            counts.push(operations.load(Ordering::Relaxed));
        }
        assert!(
            counts[1] < counts[0] * 6,
            "initial writes grew faster than rows: {counts:?}"
        );
    }

    #[test]
    fn unchanged_payload_and_metadata_issue_no_row_update() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let mut c = rusqlite::Connection::open_in_memory().unwrap();
        super::schema(&c).unwrap();
        let row = |model| super::NewRow {
            agent: "pi",
            session: "s",
            project: "p",
            turn: 0,
            who: "user",
            model,
            model_source: "explicit",
            ts: 1,
            text: "unchanged",
            extra: String::new(),
            bulk: false,
        };
        let mut changes = super::Changes::default();
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("a"), &mut changes).unwrap();
        tx.commit().unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let seen = count.clone();
        c.update_hook(Some(
            move |_: rusqlite::hooks::Action, _: &str, table: &str, _: i64| {
                if table == "prose" {
                    seen.fetch_add(1, Ordering::Relaxed);
                }
            },
        ));
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("a"), &mut changes).unwrap();
        tx.commit().unwrap();
        assert_eq!(count.load(Ordering::Relaxed), 0);
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("b"), &mut changes).unwrap();
        tx.commit().unwrap();
        assert_eq!(count.load(Ordering::Relaxed), 1);
    }
}

#[cfg(test)]
mod event_contract_tests {
    use super::*;
    #[test]
    fn control_timing_and_multiline_tool_separator_are_stored() {
        let mut c = Connection::open_in_memory().unwrap();
        schema(&c).unwrap();
        let event = |kind, name: &str, input: &str, output: &str| crate::model::Event {
            agent: "pi",
            session: "fixture-session".into(),
            ts: 1234,
            turn: Some(3),
            kind,
            name: name.into(),
            input: input.into(),
            output: output.into(),
            input_chars: input.chars().count(),
            output_chars: output.chars().count(),
            output_bytes: output.len(),
            ok: None,
            call_id: name.into(),
            child_session: String::new(),
            meta: String::new(),
            state: String::new(),
        };
        let tx = c.transaction().unwrap();
        let mut changes = Changes::default();
        let source = SourceBatch {
            key: "fixture".into(),
            agent: "pi".into(),
            events: vec![
                event("control", "compaction", "", ""),
                event("tool", "shell", "line one\nline two", "réponse"),
            ],
            ..Default::default()
        };
        replace(&tx, &source, Vec::new(), &mut changes, false).unwrap();
        for name in ["compaction", "shell"] {
            let body: String = tx
                .query_row("SELECT body FROM events WHERE call_id=?", [name], |r| {
                    r.get(0)
                })
                .unwrap();
            let body: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(body["ts"], 1234);
            assert_eq!(body["turn"], 3);
            assert_eq!(body["position"], 3);
            if name == "compaction" {
                assert!(body["input_end"].is_null());
            } else {
                let text: Vec<u8> = tx
                    .query_row("SELECT text FROM tools", [], |r| r.get(0))
                    .unwrap();
                let text = decode("tools", text).unwrap();
                let end = body["input_end"].as_u64().unwrap() as usize;
                let start = body["output_start"].as_u64().unwrap() as usize;
                assert_eq!(text.chars().nth(end), Some('\n'));
                assert_eq!(text.chars().skip(start).collect::<String>(), "réponse");
            }
        }
        assert_eq!(
            tx.query_row("SELECT count(*) FROM tools", [], |r| r.get::<_, usize>(0))
                .unwrap(),
            1
        );
    }

    #[test]
    fn user_side_count_follows_inserts_visibility_and_deletes() {
        let mut c = Connection::open_in_memory().unwrap();
        schema(&c).unwrap();
        let mut changes = Changes::default();
        let tx = c.transaction().unwrap();
        for (turn, who) in [
            (0, "user"),
            (0, "agent"),
            (1, "user"),
            (1, "agent"),
            (2, "recap"),
        ] {
            let row = NewRow {
                agent: "pi",
                session: "s",
                project: "p",
                turn,
                who,
                model: "m",
                model_source: "explicit",
                ts: 1,
                text: "body",
                extra: String::new(),
                bulk: false,
            };
            upsert(&tx, "prose", 1, row, &mut changes).unwrap();
        }
        let counted = |tx: &rusqlite::Transaction| -> (i64, i64) {
            let kept: i64 = tx
                .query_row(
                    "SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_user_count'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            let scanned: i64 = tx
                .query_row(
                    "SELECT count(*) FROM prose WHERE visible=1 AND who!='agent'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            (kept, scanned)
        };
        assert_eq!(counted(&tx), (3, 3));
        tx.execute("UPDATE prose SET visible=0 WHERE turn=1", [])
            .unwrap();
        assert_eq!(counted(&tx), (2, 2));
        tx.execute("UPDATE prose SET visible=1 WHERE who='user'", [])
            .unwrap();
        assert_eq!(counted(&tx), (3, 3));
        tx.execute("DELETE FROM prose WHERE who IN ('recap','agent')", [])
            .unwrap();
        assert_eq!(counted(&tx), (2, 2));
    }
}

/// Recompute only the cheap positional separator bound for an isolated profile
/// of an existing index. Ordinary refresh updates it together with each block.
#[doc(hidden)]
pub fn refresh_phrase_gaps(root: &DataRoot) -> Result<usize> {
    let _work = root.work_lock(true)?;
    let _lock = root.lock(true)?;
    let mut c = writer(root)?;
    let tx = c.transaction()?;
    let mut blocks = HashMap::<i64, usize>::new();
    {
        let mut q = tx.prepare("SELECT id,text FROM tools WHERE visible=1 ORDER BY id")?;
        let mut rows = q.query([])?;
        while let Some(row) = rows.next()? {
            let id: i64 = row.get(0)?;
            let text = decode("tools", row.get(1)?)?;
            let gap = crate::fts_span::gap_upper(&search_fold(&text));
            blocks
                .entry(id / BLOCK)
                .and_modify(|n| *n = (*n).max(gap))
                .or_insert(gap);
        }
    }
    for (&id, &gap) in &blocks {
        tx.execute(
            "UPDATE tool_blocks SET max_gap=?2 WHERE id=?1",
            params![id, gap],
        )?;
    }
    tx.commit()?;
    Ok(blocks.len())
}

/// Rebuild private retrieval bounds on an isolated benchmark index.
#[doc(hidden)]
pub fn rebuild_bounds(root: &DataRoot) -> Result<usize> {
    let _work = root.work_lock(true)?;
    let _lock = root.lock(true)?;
    let mut c = writer(root)?;
    let tx = c.transaction()?;
    let mut blocks = BTreeSet::new();
    for (table, tools) in [("prose", false), ("tools", true)] {
        let mut query = tx.prepare(&format!(
            "SELECT DISTINCT id / ? FROM {table} WHERE visible=1"
        ))?;
        for block in query.query_map([BLOCK], |r| r.get::<_, i64>(0))? {
            let block = block?;
            blocks.insert(if tools { -1 - block } else { block });
        }
    }
    tx.execute_batch("DELETE FROM gram_postings; DELETE FROM block_grams; DELETE FROM blocks; DELETE FROM tool_blocks; DELETE FROM bound_rebuild; DELETE FROM gram_raises;")?;
    update_blocks(&tx, &blocks, None)?;
    tx.commit()?;
    Ok(blocks.len())
}
