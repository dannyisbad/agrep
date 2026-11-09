use crate::data::DataRoot;
use anyhow::Result;
use rusqlite::{Connection, OpenFlags};
use std::path::Path;

pub fn reader(root: &DataRoot) -> Result<Connection> {
    loop {
        let c = {
            let _span = crate::semantic_profile::span("reader_open");
            match settled_unwritable(root) {
                Some(uri) => Connection::open_with_flags(
                    uri,
                    OpenFlags::SQLITE_OPEN_READ_ONLY
                        | OpenFlags::SQLITE_OPEN_URI
                        | OpenFlags::SQLITE_OPEN_NO_MUTEX,
                )?,
                None => Connection::open_with_flags(
                    root.db(),
                    OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
                )?,
            }
        };
        // A read outside the transaction takes the shared lock a WAL connection keeps until
        // it closes, without starting the snapshot the command's first query starts.
        c.query_row("PRAGMA schema_version", [], |_| Ok(()))?;
        if !unmoved(&c) {
            continue;
        }
        {
            let _span = crate::semantic_profile::span("reader_pragmas");
            c.execute_batch("PRAGMA query_only=ON; BEGIN;")?;
        }
        if crate::semantic_profile::timing_enabled() {
            // Force the otherwise lazy schema read into its own diagnostic phase.
            // Ordinary commands still load it with their first real statement.
            let _span = crate::semantic_profile::span("reader_schema");
            let _statement = c.prepare("SELECT path FROM sources LIMIT 0")?;
        }
        return Ok(c);
    }
}
/// Whether the file `c` has open is still the one at its path. A reindex renames the
/// rebuilt index over `index.db` only while no connection holds a shared lock on it
/// (`refresh::rename_unshared`), so a connection asked after a read either holds that lock
/// on the current file, which keeps it current, or sees its file moved and must reopen it.
fn unmoved(c: &Connection) -> bool {
    let mut moved: std::os::raw::c_int = 0;
    // SAFETY: the handle is open for the call, and HAS_MOVED writes one int.
    let rc = unsafe {
        rusqlite::ffi::sqlite3_file_control(
            c.handle(),
            c"main".as_ptr(),
            rusqlite::ffi::SQLITE_FCNTL_HAS_MOVED,
            (&mut moved as *mut std::os::raw::c_int).cast(),
        )
    };
    rc != rusqlite::ffi::SQLITE_OK || moved == 0
}
/// A WAL reader must create or map `index.db-shm`, which a directory this user may not write
/// refuses (SQLITE_READONLY_DIRECTORY). Such a root has no agrep writer, so once its WAL holds
/// no frames the database file is the whole index and opens as immutable.
#[cfg(unix)]
fn settled_unwritable(root: &DataRoot) -> Option<String> {
    use std::os::unix::ffi::OsStrExt;
    let wal = root.path().join("index.db-wal");
    if !crate::data::unwritable() || wal.metadata().is_ok_and(|m| m.len() > 0) {
        return None;
    }
    let mut uri = String::from("file:");
    for &b in root.db().as_os_str().as_bytes() {
        if b.is_ascii_alphanumeric() || b"/-._~".contains(&b) {
            uri.push(b as char);
        } else {
            uri.push_str(&format!("%{b:02X}"));
        }
    }
    uri.push_str("?immutable=1");
    Some(uri)
}
#[cfg(not(unix))]
fn settled_unwritable(_: &DataRoot) -> Option<String> {
    None
}
pub fn writer(root: &DataRoot) -> Result<Connection> {
    // Setting the journal mode reads the file, so a moved file shows here (see `unmoved`).
    loop {
        let c = open_writer(&root.db(), true)?;
        if unmoved(&c) {
            return Ok(c);
        }
    }
}
pub(super) fn open_writer(path: &Path, wal: bool) -> Result<Connection> {
    let initialize = path.metadata().map_or(true, |m| m.len() == 0);
    let c = Connection::open(path)?;
    c.set_prepared_statement_cache_capacity(256);
    c.execute_batch(if wal {"PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-32000; PRAGMA wal_autocheckpoint=4096; PRAGMA journal_size_limit=0;"}else{"PRAGMA journal_mode=DELETE; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-131072;"})?;
    if initialize {
        schema(&c)?;
    }
    Ok(c)
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
pub(super) fn search_indexes(c: &Connection, create: bool) -> Result<()> {
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
