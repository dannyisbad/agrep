//! One WAL transaction is the visibility boundary for rows, indexes and fingerprints.
use crate::{
    data::DataRoot,
    ingest::registry::{self, Adapter, ADAPTERS},
    intake_batch::{IntakeBatch, SourceBatch},
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
    pub token: String,
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
}
pub fn reader(root: &DataRoot) -> Result<Connection> {
    let c = Connection::open_with_flags(
        root.db(),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    c.execute_batch("PRAGMA query_only=ON; BEGIN;")?;
    Ok(c)
}
pub fn writer(root: &DataRoot) -> Result<Connection> {
    open_writer(&root.db(), true)
}
fn open_writer(path: &Path, wal: bool) -> Result<Connection> {
    let c = Connection::open(path)?;
    c.execute_batch(if wal {"PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-32000; PRAGMA wal_autocheckpoint=4096; PRAGMA journal_size_limit=0;"}else{"PRAGMA journal_mode=DELETE; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-131072;"})?;
    schema(&c)?;
    Ok(c)
}
fn schema(c: &Connection) -> Result<()> {
    c.execute_batch("CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,agent TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,change_token TEXT NOT NULL,last_error TEXT);
CREATE TABLE IF NOT EXISTS sessions(agent TEXT NOT NULL,id TEXT NOT NULL,project TEXT NOT NULL,cwd TEXT NOT NULL DEFAULT '',parent TEXT NOT NULL,family TEXT NOT NULL,side INTEGER NOT NULL,title TEXT NOT NULL,first_ts INTEGER NOT NULL,last_ts INTEGER NOT NULL,PRIMARY KEY(agent,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS sessions_family ON sessions(agent,family);
CREATE TABLE IF NOT EXISTS events(source INTEGER NOT NULL,session TEXT NOT NULL,call_id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(source,session,call_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS reply_shapes(source INTEGER NOT NULL,session TEXT NOT NULL,turn INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(source,session,turn)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS reply_shapes_session ON reply_shapes(session,source,turn);
CREATE TABLE IF NOT EXISTS family_stats(agent TEXT NOT NULL,family TEXT NOT NULL,body BLOB NOT NULL,PRIMARY KEY(agent,family)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS term_stats(token TEXT PRIMARY KEY,n INTEGER NOT NULL,s INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bounds(gram TEXT NOT NULL,block INTEGER NOT NULL,tf INTEGER NOT NULL,PRIMARY KEY(gram,block)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS bounds_block ON bounds(block);
CREATE TABLE IF NOT EXISTS blocks(id INTEGER PRIMARY KEY,max_ts INTEGER NOT NULL,user_ts INTEGER,weight REAL NOT NULL,min_session TEXT NOT NULL,min_turn INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tool_bounds(gram TEXT NOT NULL,block INTEGER NOT NULL,tf INTEGER NOT NULL,PRIMARY KEY(gram,block)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tool_bounds_block ON tool_bounds(block);
CREATE TABLE IF NOT EXISTS tool_blocks(id INTEGER PRIMARY KEY,max_ts INTEGER NOT NULL,user_ts INTEGER,weight REAL NOT NULL,min_session TEXT NOT NULL,min_turn INTEGER NOT NULL);
")?;
    for table in ["prose", "tools"] {
        c.execute_batch(&format!("CREATE TABLE IF NOT EXISTS {table}(id INTEGER PRIMARY KEY,source INTEGER NOT NULL,agent TEXT NOT NULL,session TEXT NOT NULL,project TEXT NOT NULL,turn INTEGER NOT NULL,who TEXT NOT NULL,model TEXT NOT NULL,model_source INTEGER NOT NULL,ts INTEGER NOT NULL,text BLOB NOT NULL,digest TEXT NOT NULL,extra TEXT NOT NULL,position INTEGER NOT NULL DEFAULT 0,visible INTEGER NOT NULL DEFAULT 1,UNIQUE(source,session,turn,who,extra));
CREATE INDEX IF NOT EXISTS {table}_scope ON {table}(agent,ts DESC);
CREATE INDEX IF NOT EXISTS {table}_session ON {table}(session,turn);
CREATE INDEX IF NOT EXISTS {table}_time ON {table}(ts DESC);


"))?;
        c.execute_batch(&format!("CREATE TRIGGER IF NOT EXISTS {table}_count_insert AFTER INSERT ON {table} WHEN new.visible=1 BEGIN INSERT INTO state(key,value) VALUES('{table}_count',1) ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; END;
CREATE TRIGGER IF NOT EXISTS {table}_count_delete AFTER DELETE ON {table} WHEN old.visible=1 BEGIN UPDATE state SET value=CAST(value AS INTEGER)-1 WHERE key='{table}_count'; END;
CREATE TRIGGER IF NOT EXISTS {table}_count_visibility AFTER UPDATE OF visible ON {table} WHEN old.visible!=new.visible BEGIN UPDATE state SET value=CAST(value AS INTEGER)+new.visible-old.visible WHERE key='{table}_count'; END;"))?;
        let columns = if table == "prose" {
            "text,joined"
        } else {
            "text"
        };
        let detail = if table == "prose" { "full" } else { "none" };
        c.execute_batch(&format!("CREATE VIRTUAL TABLE IF NOT EXISTS {table}_fts USING fts5({columns},content='',contentless_delete=1,tokenize='trigram',detail={detail});"))?;
    }
    Ok(())
}
pub fn digest(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
pub fn normalize(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    crate::boundary_rank::normalize_token(text)
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
    let bytes = if table == "tools" {
        lz4_flex::decompress_size_prepended(&bytes)?
    } else {
        bytes
    };
    Ok(String::from_utf8(bytes)?)
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
    let result = (|| -> Result<(u64, i64, String)> {
        let m = fs::metadata(p)?;
        let t = registry::metadata_change_token(p, &m)?;
        let mut token = serde_json::to_string(&t)?;
        // A source SQLite WAL is an input too. Reading metadata never opens it writable.
        let wal = PathBuf::from(format!("{}-wal", p.to_string_lossy()));
        if let Ok(w) = fs::metadata(&wal) {
            token.push_str(&format!(
                ":{}:{:?}:{:?}",
                w.len(),
                w.modified()?,
                registry::metadata_change_token(&wal, &w)?
            ));
        }
        Ok((
            m.len(),
            crate::intake_batch::epoch_ns(m.modified()?).unwrap_or(0),
            token,
        ))
    })();
    match result {
        Ok((size, mtime_ns, token)) => Stamp {
            path: p.to_string_lossy().into(),
            agent: a.into(),
            size,
            mtime_ns,
            token,
            error: None,
        },
        Err(e) => Stamp {
            path: p.to_string_lossy().into(),
            agent: a.into(),
            size: 0,
            mtime_ns: 0,
            token: String::new(),
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
                        token: String::new(),
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
fn known(c: &Connection) -> Result<HashMap<String, Stamp>> {
    let mut q =
        c.prepare("SELECT path,agent,size,mtime_ns,change_token,last_error FROM sources")?;
    let rows = q
        .query_map([], |r| {
            let s = Stamp {
                path: r.get(0)?,
                agent: r.get(1)?,
                size: r.get(2)?,
                mtime_ns: r.get(3)?,
                token: r.get(4)?,
                error: r.get(5)?,
            };
            Ok((s.path.clone(), s))
        })?
        .collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}
pub fn refresh(root: &DataRoot, explicit: bool, full: bool) -> Result<Refresh> {
    refresh_inner(root, explicit, full, false)
}
pub fn reindex(root: &DataRoot) -> Result<Refresh> {
    refresh_inner(root, true, true, true)
}
fn refresh_inner(root: &DataRoot, explicit: bool, full: bool, rebuild: bool) -> Result<Refresh> {
    let start = Instant::now();
    let first = !root.db().exists();
    let lock = root.lock(explicit || first)?;
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
    let mut c = if first {
        open_writer(&initial, false)?
    } else {
        writer(root)?
    };
    let old = known(&c)?;
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
    let stamps = census()?;
    let census_ms = tick.elapsed().as_secs_f64() * 1000.;
    let changed: HashSet<PathBuf> = stamps
        .iter()
        .filter(|s| {
            full || old
                .get(&s.path)
                .is_none_or(|o| s.size != o.size || s.mtime_ns != o.mtime_ns || s.token != o.token)
        })
        .map(|s| PathBuf::from(&s.path))
        .collect();
    let discovered: HashSet<&str> = stamps.iter().map(|s| s.path.as_str()).collect();
    let removed: Vec<_> = old
        .values()
        .filter(|s| {
            !s.path.starts_with('@')
                && !s.path.starts_with("token:")
                && !discovered.contains(s.path.as_str())
                && matches!(fs::symlink_metadata(&s.path),Err(e) if e.kind()==std::io::ErrorKind::NotFound)
        })
        .map(|s| s.path.clone())
        .collect();
    let mut info = Refresh {
        sources: stamps.len(),
        census_ms,
        ..Default::default()
    };
    if changed.is_empty() && removed.is_empty() && !first {
        info.total_ms = start.elapsed().as_secs_f64() * 1000.;
        return Ok(info);
    }
    if first {
        eprintln!("agrep: indexing {} source files…", stamps.len());
    }
    let mut active: HashSet<String> = stamps
        .iter()
        .filter(|s| changed.contains(Path::new(&s.path)))
        .map(|s| s.agent.clone())
        .collect();
    for key in &removed {
        if let Some(stamp) = old.get(key) {
            active.insert(stamp.agent.clone());
        }
    }
    let parse_start = Instant::now();
    let batches: Vec<_> = ADAPTERS
        .par_iter()
        .filter(|a| active.contains(a.name()))
        .map(|a| {
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
                source_paths: stamps
                    .iter()
                    .filter(|s| s.agent == a.name())
                    .map(|s| PathBuf::from(&s.path))
                    .collect(),
                tokens: old
                    .iter()
                    .filter(|(k, _)| k.starts_with(&format!("token:{}:", a.name())))
                    .map(|(k, v)| (k.clone(), v.token.clone()))
                    .collect(),
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
                })
            }
            batch
        })
        .collect();
    info.parse_ms = parse_start.elapsed().as_secs_f64() * 1000.;
    let write_start = Instant::now();
    let tx = c.transaction()?;
    if rebuild {
        for t in ["prose", "tools"] {
            tx.execute(&format!("DELETE FROM {t}"), [])?;
            tx.execute(&format!("DELETE FROM {t}_fts"), [])?;
        }
        tx.execute("DELETE FROM sessions", [])?;
        tx.execute("DELETE FROM bounds", [])?;
        tx.execute("DELETE FROM blocks", [])?;
        tx.execute("DELETE FROM tool_blocks", [])?;
        tx.execute("DELETE FROM tool_bounds", [])?;
        for table in ["events", "reply_shapes", "family_stats", "term_stats"] {
            tx.execute(&format!("DELETE FROM {table}"), [])?;
        }
    }
    let mut dirty = BTreeSet::new();
    let mut family_dirty = HashSet::new();
    let mut canonical_sessions = HashSet::new();
    let mut errors = HashMap::new();
    for key in &removed {
        source_sessions(&tx, key, &mut canonical_sessions)?;
        source_families(&tx, key, &mut family_dirty)?;
        for t in ["prose", "tools"] {
            remove_source(&tx, t, key, &mut dirty)?;
        }
        tx.execute("DELETE FROM sources WHERE path=?", [key])?;
    }
    for b in batches {
        for key in &b.retired {
            source_sessions(&tx, key, &mut canonical_sessions)?;
            source_families(&tx, key, &mut family_dirty)?;
            for table in ["prose", "tools"] {
                remove_source(&tx, table, key, &mut dirty)?;
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
                    source_families(&tx, key, &mut family_dirty)?;
                    for table in ["prose", "tools"] {
                        remove_source(&tx, table, key, &mut dirty)?;
                    }
                    tx.execute("DELETE FROM sources WHERE path=?", [key])?;
                }
            }
        }
        for issue in b.issues {
            errors.insert(issue.path.to_string_lossy().into_owned(), issue.reason);
        }
        for mut s in b.batches {
            s.messages =
                crate::row_class::normalize_messages(std::mem::take(&mut s.messages), &harness);
            if s.key.starts_with('@') {
                s.messages = crate::intake_batch::canonicalize_messages(s.messages);
            }
            info.parsed += 1;
            if let Some(error) = s.error {
                if s.key.starts_with('@') || s.key.starts_with("token:") {
                    let token = b.tokens.get(&s.key).cloned().unwrap_or_default();
                    tx.execute("INSERT INTO sources VALUES(?1,?2,0,0,?3,?4) ON CONFLICT(path) DO UPDATE SET change_token=?3,last_error=?4", params![s.key,s.agent,token,error])?;
                }
                errors.insert(s.key.clone(), error);
                continue;
            }
            source_sessions(&tx, &s.key, &mut canonical_sessions)?;
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
            source_families(&tx, &s.key, &mut family_dirty)?;
            info.changed_rows += replace(&tx, &s, &mut dirty)?;
            source_families(&tx, &s.key, &mut family_dirty)?;
            if s.key.starts_with('@') || s.key.starts_with("token:") {
                let token = b.tokens.get(&s.key).cloned().unwrap_or_default();
                tx.execute("INSERT INTO sources VALUES(?1,?2,0,0,?3,NULL) ON CONFLICT(path) DO UPDATE SET change_token=?3,last_error=NULL",params![s.key,s.agent,token])?;
            }
        }
    }
    for s in stamps {
        let error = errors
            .get(&s.path)
            .cloned()
            .or(s.error)
            .or_else(|| {
                errors.iter().find_map(|(path, error)| {
                    Path::new(&s.path)
                        .starts_with(Path::new(path))
                        .then(|| error.clone())
                })
            })
            .or_else(|| {
                (!changed.contains(Path::new(&s.path)))
                    .then(|| old.get(&s.path).and_then(|o| o.error.clone()))
                    .flatten()
            });
        tx.execute("INSERT INTO sources VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(path) DO UPDATE SET size=?3,mtime_ns=?4,change_token=?5,last_error=?6",params![s.path,s.agent,s.size,s.mtime_ns,s.token,error])?;
    }
    for (path, error) in &errors {
        tx.execute(
            "UPDATE sources SET last_error=?2 WHERE path=?1",
            params![path, error],
        )?;
    }
    canonicalize(&tx, &canonical_sessions, &mut dirty)?;
    info.write_ms = write_start.elapsed().as_secs_f64() * 1000.;
    let phase = Instant::now();
    update_blocks(&tx, &dirty)?;
    info.bounds_ms = phase.elapsed().as_secs_f64() * 1000.;
    let phase = Instant::now();
    resolve_families(&tx, &mut family_dirty)?;
    update_stats(&tx, family_dirty)?;
    info.stats_ms = phase.elapsed().as_secs_f64() * 1000.;
    tx.execute("DELETE FROM sessions WHERE NOT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=sessions.agent AND p.session=sessions.id) AND NOT EXISTS(SELECT 1 FROM tools p INDEXED BY tools_session WHERE p.agent=sessions.agent AND p.session=sessions.id)",[])?;
    tx.execute("INSERT INTO state VALUES('refreshed',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[chrono::Utc::now().timestamp_millis().to_string()])?;
    tx.execute("INSERT INTO state VALUES('harness',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[policy_digest])?;
    tx.commit()?;
    if first {
        c.execute_batch("PRAGMA journal_mode=WAL;")?;
        c.close().map_err(|(_, e)| e)?;
        fs::rename(&initial, root.db())?;
        #[cfg(unix)]
        fs::File::open(root.path())?.sync_all()?;
    }
    info.errors = errors.len();
    info.total_ms = start.elapsed().as_secs_f64() * 1000.;
    drop(lock);
    Ok(info)
}
fn insert_fts(tx: &Transaction<'_>, table: &str, id: i64, text: &str) -> Result<()> {
    let normalized = normalize(text);
    if table == "prose" {
        tx.prepare_cached("INSERT INTO prose_fts(rowid,text,joined) VALUES(?1,?2,?3)")?
            .execute(params![id, normalized, joined(&normalized)])?;
    } else {
        tx.prepare_cached("INSERT INTO tools_fts(rowid,text) VALUES(?1,?2)")?
            .execute(params![id, normalized])?;
    }
    Ok(())
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
    dirty: &mut BTreeSet<i64>,
) -> Result<()> {
    let (old_visible, old_position) = old;
    let (visible, position) = new;
    if old_visible == visible && old_position == position {
        return Ok(());
    }
    if old_visible != visible {
        if visible {
            let text: Vec<u8> =
                tx.query_row(&format!("SELECT text FROM {table} WHERE id=?"), [id], |r| {
                    r.get(0)
                })?;
            insert_fts(tx, table, id, &decode(table, text)?)?;
        } else {
            tx.execute(&format!("DELETE FROM {table}_fts WHERE rowid=?"), [id])?;
        }
    }
    tx.execute(
        &format!("UPDATE {table} SET visible=?2,position=?3 WHERE id=?1"),
        params![id, visible, position],
    )?;
    dirty.insert(if table == "tools" {
        -1 - id / BLOCK
    } else {
        id / BLOCK
    });
    Ok(())
}
fn refresh_session_bounds(tx: &Transaction<'_>, agent: &str, session: &str) -> Result<()> {
    tx.prepare_cached("UPDATE sessions SET first_ts=coalesce((SELECT min(ts) FROM (SELECT ts FROM prose WHERE agent=?1 AND session=?2 AND visible=1 UNION ALL SELECT ts FROM tools WHERE agent=?1 AND session=?2 AND visible=1)),0),last_ts=coalesce((SELECT max(ts) FROM (SELECT ts FROM prose WHERE agent=?1 AND session=?2 AND visible=1 UNION ALL SELECT ts FROM tools WHERE agent=?1 AND session=?2 AND visible=1)),0),title=coalesce((SELECT substr(CAST(text AS TEXT),1,CASE WHEN instr(CAST(text AS TEXT),char(10))>0 THEN min(160,instr(CAST(text AS TEXT),char(10))-1) ELSE 160 END) FROM prose WHERE agent=?1 AND session=?2 AND visible=1 AND who!='agent' ORDER BY position,id LIMIT 1),'') WHERE agent=?1 AND id=?2")?.execute(params![agent,session])?;
    Ok(())
}
/// Keep source contributions for incremental recovery, but index only the canonical replay.
fn canonicalize(
    tx: &Transaction<'_>,
    sessions: &HashSet<(String, String)>,
    dirty: &mut BTreeSet<i64>,
) -> Result<()> {
    for (agent, session) in sessions {
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
        let mut best: BTreeMap<(i64, String), usize> = BTreeMap::new();
        for (i, m) in messages.iter().enumerate() {
            let r = &rows[m.row];
            let key = (r.turn, m.key_text.clone());
            if best
                .get(&key)
                .is_none_or(|j| replay_cmp(m, &messages[*j], &rows, agent).is_gt())
            {
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
                dirty,
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
                dirty,
            )?;
        }
        if let Some(m) = chosen
            .iter()
            .map(|i| &messages[*i])
            .max_by(|a, b| replay_cmp(a, b, &rows, agent))
        {
            let r = &rows[m.row];
            let title = chosen
                .iter()
                .min_by_key(|i| positions[i])
                .map(|i| {
                    rows[messages[*i].row]
                        .text
                        .lines()
                        .next()
                        .unwrap_or("")
                        .chars()
                        .take(160)
                        .collect::<String>()
                })
                .unwrap_or_default();
            tx.execute("UPDATE sessions SET project=?3,parent=?4,side=?5,title=?6 WHERE agent=?1 AND id=?2",params![agent,session,r.project,m.parent,m.side,title])?;
        }
        refresh_session_bounds(tx, agent, session)?;
    }
    Ok(())
}

fn remove_source(
    tx: &Transaction<'_>,
    table: &str,
    key: &str,
    dirty: &mut BTreeSet<i64>,
) -> Result<()> {
    let ids: Vec<i64> = tx
        .prepare(&format!(
            "SELECT id FROM {table} WHERE source=(SELECT rowid FROM sources WHERE path=?)"
        ))?
        .query_map([key], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    for id in ids {
        erase(tx, table, id, dirty)?;
    }
    if table == "tools" {
        tx.execute(
            "DELETE FROM events WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
    } else {
        tx.execute(
            "DELETE FROM reply_shapes WHERE source=(SELECT rowid FROM sources WHERE path=?)",
            [key],
        )?;
    }
    Ok(())
}
fn erase(tx: &Transaction<'_>, t: &str, id: i64, dirty: &mut BTreeSet<i64>) -> Result<()> {
    tx.execute(&format!("DELETE FROM {t}_fts WHERE rowid=?"), [id])?;
    tx.execute(&format!("DELETE FROM {t} WHERE id=?"), [id])?;
    dirty.insert(if t == "tools" {
        -1 - id / BLOCK
    } else {
        id / BLOCK
    });
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
}
fn upsert(
    tx: &Transaction<'_>,
    table: &str,
    source: i64,
    r: NewRow<'_>,
    dirty: &mut BTreeSet<i64>,
) -> Result<usize> {
    let dg = digest(r.text);
    type StoredRow = (i64, String, i64, String, String, String);
    let old:Option<StoredRow>=tx.prepare_cached(&format!("SELECT id,digest,ts,project,model,model_source FROM {table} WHERE source=?1 AND session=?2 AND turn=?3 AND who=?4 AND extra=?5"))?.query_row(params![source,r.session,r.turn,r.who,r.extra],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    if let Some((id, ref old_dg, old_ts, ref project, ref model, ref model_source)) = old {
        if *old_dg == dg {
            if old_ts == r.ts
                && project == r.project
                && model == r.model
                && model_source == r.model_source
            {
                return Ok(0);
            }
            if old_ts != r.ts {
                dirty.insert(if table == "tools" {
                    -1 - id / BLOCK
                } else {
                    id / BLOCK
                });
            }
            tx.execute(
                &format!(
                    "UPDATE {table} SET project=?2,model=?3,model_source=?4,ts=?5 WHERE id=?1"
                ),
                params![id, r.project, r.model, r.model_source, r.ts],
            )?;
            return Ok(0);
        }
        erase(tx, table, id, dirty)?;
    }
    let encoded = if table == "tools" {
        lz4_flex::compress_prepend_size(r.text.as_bytes())
    } else {
        r.text.as_bytes().to_vec()
    };
    tx.prepare_cached(&format!("INSERT INTO {table}(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?5)"))?.execute(params![source,r.agent,r.session,r.project,r.turn,r.who,r.model,r.model_source,r.ts,encoded,dg,r.extra])?;
    let id = tx.last_insert_rowid();
    insert_fts(tx, table, id, r.text)?;
    dirty.insert(if table == "tools" {
        -1 - id / BLOCK
    } else {
        id / BLOCK
    });
    Ok(1)
}
fn replace(tx: &Transaction<'_>, s: &SourceBatch, dirty: &mut BTreeSet<i64>) -> Result<usize> {
    tx.execute(
        "INSERT OR IGNORE INTO sources(path,agent,size,mtime_ns,change_token) VALUES(?1,?2,0,0,'')",
        params![s.key, s.agent],
    )?;
    let source: i64 = tx.query_row("SELECT rowid FROM sources WHERE path=?", [&s.key], |r| {
        r.get(0)
    })?;
    let mut keys: HashSet<(String, i64, String, String)> = HashSet::new();
    let mut count = 0;
    let mut meta: HashMap<&str, (&str, i64, bool, &str)> = HashMap::new();
    let source_directory = source_cwd(&s.key);
    for m in &s.messages {
        meta.insert(&m.session, (&m.project, m.ts, m.side, &m.parent));
        let cwd = if m.project.starts_with('/') || m.project.contains(":\\") {
            m.project.to_string()
        } else {
            source_directory.clone()
        };
        session(tx, m, &cwd)?;
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
            count += upsert(
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
                },
                dirty,
            )?;
        }
    }
    let message_keys: HashSet<_> = s
        .messages
        .iter()
        .map(|m| (m.session.to_string(), m.turn as i64))
        .collect();
    let stale_shapes: Vec<(String, i64)> = tx
        .prepare_cached("SELECT session,turn FROM reply_shapes WHERE source=?")?
        .query_map([source], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (session, turn) in stale_shapes {
        if !message_keys.contains(&(session.clone(), turn)) {
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
    for e in &s.events {
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
        tx.prepare_cached("INSERT INTO events VALUES(?1,?2,?3,?4) ON CONFLICT(source,session,call_id) DO UPDATE SET body=excluded.body WHERE body!=excluded.body")?.execute(params![source,e.session,call_id,serde_json::json!({"kind":e.kind,"name":e.name,"input_chars":e.input_chars,"output_chars":e.output_chars,"output_bytes":e.output_bytes,"ok":e.ok,"call_id":call_id,"child_session":e.child_session,"meta":e.meta,"state":e.state}).to_string()])?;
        let (project, _, _, _) = meta
            .get(e.session.as_str())
            .copied()
            .unwrap_or(("unknown", 0, false, ""));
        let turn = turns
            .get(e.session.as_str())
            .and_then(|v| {
                v.partition_point(|(ts, _)| *ts <= e.ts)
                    .checked_sub(1)
                    .map(|i| v[i].1)
            })
            .unwrap_or_else(|| *event_turn.get(e.session.as_str()).unwrap_or(&0));
        event_turn.insert(&e.session, turn);
        let extra = call_id;
        event_keys.insert((e.session.clone(), turn, "tool".into(), extra.clone()));
        let mut head = match e.kind {
            "subagent_start" => format!("subagent {}", e.name.trim()),
            "subagent_result" => format!("subagent result {}", e.name.trim()),
            "control" => continue,
            _ => e.name.trim().to_string(),
        };
        if e.ok == Some(false) {
            head.push_str(" [failed]")
        };
        let input = e.input.trim();
        let mut text = if head.is_empty() {
            input.to_string()
        } else if input.is_empty() {
            head
        } else {
            format!("{head}: {input}")
        };
        if !e.output.is_empty() {
            text.push('\n');
            text.push_str(&e.output);
        }
        let text = text.trim().to_string();
        count += upsert(
            tx,
            "tools",
            source,
            NewRow {
                agent: e.agent,
                session: &e.session,
                project,
                turn,
                who: "tool",
                model: "",
                model_source: "unknown",
                ts: e.ts,
                text: &text,
                extra,
            },
            dirty,
        )?;
        tx.prepare_cached("INSERT OR IGNORE INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES(?1,?2,?3,'',?2,0,'',?4,?4)")?.execute(params![e.agent,e.session,project,e.ts])?;
    }
    let stale_events: Vec<(String, String)> = tx
        .prepare_cached("SELECT session,call_id FROM events WHERE source=?")?
        .query_map([source], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (session, call_id) in stale_events {
        if !metadata_keys.contains(&(session.clone(), call_id.clone())) {
            tx.execute(
                "DELETE FROM events WHERE source=?1 AND session=?2 AND call_id=?3",
                params![source, session, call_id],
            )?;
        }
    }
    for (t, set) in [("prose", keys), ("tools", event_keys)] {
        let stale: Vec<i64> = tx
            .prepare(&format!(
                "SELECT id,session,turn,who,extra FROM {t} WHERE source=?"
            ))?
            .query_map([source], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?),
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?
            .into_iter()
            .filter(|(_, k)| !set.contains(k))
            .map(|(id, _)| id)
            .collect();
        for id in stale {
            erase(tx, t, id, dirty)?;
            count += 1;
        }
    }
    Ok(count)
}
fn session(tx: &Transaction<'_>, m: &Message, cwd: &str) -> Result<()> {
    let title = m
        .text
        .lines()
        .next()
        .unwrap_or("")
        .chars()
        .take(200)
        .collect::<String>();
    let family = if m.parent.is_empty() {
        m.session.as_ref()
    } else {
        m.parent.as_ref()
    };
    tx.prepare_cached("INSERT INTO sessions(agent,id,project,parent,family,side,title,first_ts,last_ts) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?8) ON CONFLICT(agent,id) DO UPDATE SET project=?3,parent=?4,family=?5,side=?6,first_ts=MIN(first_ts,?8),last_ts=MAX(last_ts,?8),title=CASE WHEN ?8<=first_ts THEN ?7 ELSE title END")?.execute(params![m.agent,m.session.as_ref(),m.project.as_ref(),m.parent.as_ref(),family,m.side,title,m.ts])?;
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
    grams: Vec<(String, u32)>,
}
fn gram_maxima(texts: impl Iterator<Item = String>) -> HashMap<String, u32> {
    let mut counts: HashMap<String, (usize, u32, u32)> = HashMap::new();
    for (row, text) in texts.enumerate() {
        let mut observe = |gram: &str| {
            if let Some((previous, current, max)) = counts.get_mut(gram) {
                if *previous != row {
                    *previous = row;
                    *current = 0;
                }
                *current += 1;
                *max = (*max).max(*current);
            } else {
                counts.insert(gram.to_owned(), (row, 1, 1));
            }
        };
        if text.is_ascii() {
            for width in 1..=3 {
                if text.len() >= width {
                    for start in 0..=text.len() - width {
                        observe(&text[start..start + width]);
                    }
                }
            }
        } else {
            let mut offsets: Vec<_> = text.char_indices().map(|(i, _)| i).collect();
            offsets.push(text.len());
            for width in 1..=3 {
                for window in offsets.windows(width + 1) {
                    observe(&text[window[0]..window[width]]);
                }
            }
        }
    }
    counts
        .into_iter()
        .map(|(g, (_, _, max))| (g, max))
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
        texts.push(normalize(&decode(table, text)?));
    }
    let mut grams: Vec<_> = gram_maxima(texts.into_iter()).into_iter().collect();
    grams.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    Ok(BuiltBounds {
        key,
        max_ts,
        user_ts,
        weight,
        min_session,
        min_turn,
        grams,
    })
}
fn update_blocks(tx: &Transaction<'_>, blocks: &BTreeSet<i64>) -> Result<()> {
    let keys: Vec<_> = blocks.iter().copied().collect();
    let batch_size = rayon::current_num_threads().max(1) * 2;
    for chunk in keys.chunks(batch_size) {
        let mut input = Vec::new();
        for &key in chunk {
            let (table, block) = if key < 0 {
                ("tools", -1 - key)
            } else {
                ("prose", key)
            };
            let mut q = tx.prepare_cached(&format!(
                "SELECT text,ts,who,session,position FROM {table} WHERE visible=1 AND id>=?1 AND id<?2"
            ))?;
            let rows = q
                .query_map(params![block * BLOCK, (block + 1) * BLOCK], |r| {
                    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
                })?
                .collect::<rusqlite::Result<Vec<BoundRow>>>()?;
            input.push((key, rows));
        }
        let built: Vec<Result<BuiltBounds>> = input
            .into_par_iter()
            .map(|(key, rows)| build_bounds(key, rows))
            .collect();
        for block in built {
            let b = block?;
            let (bounds, metadata, id) = if b.key < 0 {
                ("tool_bounds", "tool_blocks", -1 - b.key)
            } else {
                ("bounds", "blocks", b.key)
            };
            tx.prepare_cached(&format!("DELETE FROM {bounds} WHERE block=?"))?
                .execute([id])?;
            tx.prepare_cached(&format!("DELETE FROM {metadata} WHERE id=?"))?
                .execute([id])?;
            if !b.grams.is_empty() {
                tx.prepare_cached(&format!("INSERT INTO {metadata} VALUES(?1,?2,?3,?4,?5,?6)"))?
                    .execute(params![
                        id,
                        b.max_ts,
                        b.user_ts,
                        b.weight,
                        b.min_session,
                        b.min_turn
                    ])?;
                let mut insert =
                    tx.prepare_cached(&format!("INSERT INTO {bounds} VALUES(?1,?2,?3)"))?;
                for (gram, n) in b.grams {
                    insert.execute(params![gram, id, n])?;
                }
            }
        }
    }
    Ok(())
}

pub fn counts(c: &Connection) -> Result<serde_json::Value> {
    Ok(
        serde_json::json!({"sessions":c.query_row("SELECT count(*) FROM sessions",[],|r|r.get::<_,i64>(0))?,"prose":c.query_row("SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_count'),0)",[],|r|r.get::<_,i64>(0))?,"tools":c.query_row("SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='tools_count'),0)",[],|r|r.get::<_,i64>(0))?,"source_errors":c.query_row("SELECT count(*) FROM sources WHERE last_error IS NOT NULL",[],|r|r.get::<_,i64>(0))?}),
    )
}

fn source_families(
    tx: &Transaction<'_>,
    source: &str,
    out: &mut HashSet<(String, String)>,
) -> Result<()> {
    let mut q=tx.prepare_cached("SELECT DISTINCT s.agent,s.family FROM prose p JOIN sessions s ON p.agent=s.agent AND p.session=s.id WHERE p.source=(SELECT rowid FROM sources WHERE path=?)")?;
    for row in q.query_map([source], |r| Ok((r.get(0)?, r.get(1)?)))? {
        out.insert(row?);
    }
    Ok(())
}
fn update_stats(tx: &Transaction<'_>, families: HashSet<(String, String)>) -> Result<()> {
    let mut delta: HashMap<String, (i64, i64)> = HashMap::new();
    for (agent, family) in families {
        let old: Option<Vec<u8>> = tx
            .query_row(
                "SELECT body FROM family_stats WHERE agent=?1 AND family=?2",
                params![agent, family],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(old) = old {
            let old: (HashSet<String>, HashSet<String>) =
                serde_json::from_slice(&lz4_flex::decompress_size_prepended(&old)?)?;
            for t in old.0 {
                delta.entry(t).or_default().0 -= 1;
            }
            for t in old.1 {
                delta.entry(t).or_default().1 -= 1;
            }
        }
        let mut q=tx.prepare("SELECT p.text FROM sessions s INDEXED BY sessions_family CROSS JOIN prose p INDEXED BY prose_session WHERE s.agent=?1 AND s.family=?2 AND p.agent=s.agent AND p.session=s.id AND p.visible=1")?;
        let texts: Vec<String> = q
            .query_map(params![agent, family], |r| r.get::<_, Vec<u8>>(0))?
            .map(|v| Ok(String::from_utf8(v?)?))
            .collect::<Result<_>>()?;
        let (any, aligned) = crate::boundary_stats::text_tokens(texts.iter().map(String::as_str));
        for t in &any {
            delta.entry(t.clone()).or_default().0 += 1;
        }
        for t in &aligned {
            delta.entry(t.clone()).or_default().1 += 1;
        }
        let body = lz4_flex::compress_prepend_size(&serde_json::to_vec(&(any, aligned))?);
        tx.execute("INSERT INTO family_stats VALUES(?1,?2,?3) ON CONFLICT(agent,family) DO UPDATE SET body=excluded.body",params![agent,family,body])?;
    }
    let mut update=tx.prepare_cached("INSERT INTO term_stats VALUES(?1,?2,?3) ON CONFLICT(token) DO UPDATE SET n=n+excluded.n,s=s+excluded.s")?;
    for (t, (n, s)) in delta {
        if n != 0 || s != 0 {
            update.execute(params![t, n, s])?;
        }
    }
    tx.execute("DELETE FROM term_stats WHERE n<=0", [])?;
    Ok(())
}

fn resolve_families(tx: &Transaction<'_>, dirty: &mut HashSet<(String, String)>) -> Result<()> {
    let rows: Vec<(String, String, String, String)> = tx
        .prepare("SELECT agent,id,parent,family FROM sessions")?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let parents: HashMap<_, _> = rows
        .iter()
        .filter(|r| !r.2.is_empty())
        .map(|r| ((r.0.clone(), r.1.clone()), r.2.clone()))
        .collect();
    let mut memo: HashMap<(String, String), String> = HashMap::new();
    let mut update = tx.prepare_cached("UPDATE sessions SET family=?3 WHERE agent=?1 AND id=?2")?;
    for (agent, id, _, old) in rows {
        let mut seen = Vec::new();
        let mut cur = id.clone();
        let root = loop {
            let key = (agent.clone(), cur.clone());
            if let Some(root) = memo.get(&key) {
                break root.clone();
            }
            if seen.contains(&cur) {
                break seen.iter().min().cloned().unwrap_or(cur);
            }
            seen.push(cur.clone());
            match parents.get(&key) {
                Some(parent) => cur = parent.clone(),
                None => break cur,
            }
        };
        for member in seen {
            memo.insert((agent.clone(), member), root.clone());
        }
        if old != root {
            dirty.insert((agent.clone(), old));
            dirty.insert((agent.clone(), root.clone()));
            update.execute(params![agent, id, root])?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod bound_tests {
    #[test]
    fn counts_are_per_row_not_summed_across_a_block() {
        let got = super::gram_maxima(["aaaa", "aa", "ééé"].into_iter().map(str::to_owned));
        assert_eq!(got["a"], 4);
        assert_eq!(got["aa"], 3);
        assert_eq!(got["aaa"], 2);
        assert_eq!(got["é"], 3);
        assert_eq!(got["éé"], 2);
        assert_eq!(got["ééé"], 1);
    }
}

#[cfg(test)]
mod row_diff_tests {
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
        };
        let mut dirty = std::collections::BTreeSet::new();
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("a"), &mut dirty).unwrap();
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
        super::upsert(&tx, "prose", 1, row("a"), &mut dirty).unwrap();
        tx.commit().unwrap();
        assert_eq!(count.load(Ordering::Relaxed), 0);
        let tx = c.transaction().unwrap();
        super::upsert(&tx, "prose", 1, row("b"), &mut dirty).unwrap();
        tx.commit().unwrap();
        assert_eq!(count.load(Ordering::Relaxed), 1);
    }
}
