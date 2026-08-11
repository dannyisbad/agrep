//! cursor adapter: the editor's chat/agent history in `User/globalStorage/state.vscdb`
//! (a sqlite key-value store). `cursorDiskKV` keys:
//!   composerData:<uuid>      one session: name, createdAt ms, modelConfig.modelName,
//!                            fullConversationHeadersOnly [{bubbleId, type}] in order
//!   bubbleId:<cid>:<uuid>    one message: type 1=user 2=ai, text, toolFormerData
//!                            {name, params/rawArgs, result, status}, createdAt RFC3339
//! The newer `agentKv:blob:*` family is the agent harness's content-addressed (partly
//! encrypted) request cache, not the transcript - skipped by design. Older inline
//! `conversation` generations are also skipped because the bubble generation is authoritative.
//! Project attribution joins global `composerHeaders.workspaceId` to
//! `User/workspaceStorage/<id>/workspace.json`. Older per-workspace allComposers rows
//! remain a fallback. Unmapped composers use project "cursor".
//!
//! One sqlite file contains many conversations, so staleness is tracked per session. Since
//! `composerData` timestamps need not move with bubble edits, the token hashes that row plus
//! its bubble rows in key order. New, edited, and deleted bubbles therefore change the token.

use std::collections::{HashMap, HashSet};

use rusqlite::Connection;

#[cfg(test)]
use crate::ingest::registry::fnv_token;
use crate::ingest::registry::{metadata_is_link, read_bounded_regular_file};
use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, project_name,
    summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::model::{Event, Message};

const TOKEN_SCAN_SQL: [&str; 2] = [
    "SELECT key, value FROM cursorDiskKV \
     WHERE key >= 'composerData:' AND key < 'composerData;' ORDER BY key",
    "SELECT key, value FROM cursorDiskKV \
     WHERE key >= 'bubbleId:' AND key < 'bubbleId;' ORDER BY key",
];
const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const CENSUS_SESSION: &str = "\0census\0";
const WORKSPACE_JSON_MAX_BYTES: u64 = 4 * 1024 * 1024;
/// Reserved non-conversation session (like CENSUS_SESSION) marking an unenumerable store,
/// so its zero conversations can never read as a complete, issue-free census of zero.
const SCHEMA_ABSENT_SESSION: &str = "\0schema-absent\0";
const SCHEMA_ABSENT_TOKEN: &str = "unenumerable";

fn fnv_extend(hash: &mut u64, bytes: &[u8]) {
    for &byte in bytes {
        *hash ^= u64::from(byte);
        *hash = hash.wrapping_mul(0x100000001b3);
    }
}

fn fnv_finish(hash: u64) -> String {
    format!("h:{hash:016x}")
}

fn fnv_sql_value(hash: &mut u64, value: &rusqlite::types::Value) {
    match value {
        rusqlite::types::Value::Null => fnv_extend(hash, b"\0null\0"),
        rusqlite::types::Value::Integer(value) => {
            fnv_extend(hash, b"\0integer\0");
            fnv_extend(hash, &value.to_le_bytes());
        }
        rusqlite::types::Value::Real(value) => {
            fnv_extend(hash, b"\0real\0");
            fnv_extend(hash, &value.to_bits().to_le_bytes());
        }
        rusqlite::types::Value::Text(value) => {
            fnv_extend(hash, b"\0text\0");
            fnv_extend(hash, value.as_bytes());
        }
        rusqlite::types::Value::Blob(value) => {
            fnv_extend(hash, b"\0blob\0");
            fnv_extend(hash, value);
        }
    }
}

fn user_dir_candidates_at(
    home: &std::path::Path,
    appdata: Option<&std::path::Path>,
) -> [std::path::PathBuf; 3] {
    let windows = appdata
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| home.join("AppData").join("Roaming"))
        .join("Cursor")
        .join("User");
    let macos = home
        .join("Library")
        .join("Application Support")
        .join("Cursor")
        .join("User");
    let linux = home.join(".config").join("Cursor").join("User");
    #[cfg(target_os = "windows")]
    return [windows, macos, linux];
    #[cfg(target_os = "macos")]
    return [macos, windows, linux];
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    return [linux, windows, macos];
}

fn user_dir_candidates(home: &std::path::Path) -> [std::path::PathBuf; 3] {
    let appdata = if cfg!(target_os = "windows") {
        std::env::var_os("APPDATA")
            .filter(|value| !value.as_os_str().is_empty())
            .map(std::path::PathBuf::from)
    } else {
        None
    };
    user_dir_candidates_at(home, appdata.as_deref())
}

#[derive(Debug)]
struct UserDirError {
    path: std::path::PathBuf,
    source: std::io::Error,
}

impl std::fmt::Display for UserDirError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "cannot inspect {}: {}",
            self.path.display(),
            self.source
        )
    }
}

impl std::error::Error for UserDirError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

fn user_dir_checked_in(
    candidates: [std::path::PathBuf; 3],
) -> Result<Option<std::path::PathBuf>, UserDirError> {
    for user in candidates {
        let path = user.join("globalStorage").join("state.vscdb");
        match std::fs::symlink_metadata(&path) {
            Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => return Ok(Some(user)),
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(UserDirError {
                    path,
                    source: error,
                });
            }
        }
    }
    Ok(None)
}

#[cfg(test)]
fn user_dir_checked_at(home: &std::path::Path) -> Result<Option<std::path::PathBuf>, UserDirError> {
    user_dir_checked_in(user_dir_candidates_at(home, None))
}

/// The native Cursor profile wins; copied cross-OS profiles are migration fallback only.
fn user_dir_checked() -> Result<Option<std::path::PathBuf>, UserDirError> {
    user_dir_checked_in(user_dir_candidates(&crate::ingest::home()))
}

fn user_dir() -> Option<std::path::PathBuf> {
    user_dir_checked().ok().flatten()
}

/// Open Cursor without write authority; failures retain the last-good token generation.
fn open_ro(path: &std::path::Path) -> Option<crate::ingest::ReadOnlyConnection> {
    match crate::ingest::open_sqlite_ro(path) {
        Ok(c) => Some(c),
        Err(e) => {
            eprintln!(
                "  ! cursor: cannot open {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            );
            None
        }
    }
}

/// A row value's bytes. The column is declared BLOB but the stored class varies (TEXT in
/// real stores) - reading a fixed type errors on the other class, so match the value.
fn val_bytes(v: rusqlite::types::Value) -> Option<Vec<u8>> {
    match v {
        rusqlite::types::Value::Text(s) => Some(s.into_bytes()),
        rusqlite::types::Value::Blob(b) => Some(b),
        _ => None,
    }
}

fn kv_text_checked(conn: &Connection, table: &str, key: &str) -> rusqlite::Result<Option<String>> {
    match conn.query_row(
        &format!("SELECT value FROM {table} WHERE key = ?"),
        [key],
        |r| r.get::<_, rusqlite::types::Value>(0),
    ) {
        Ok(value) => Ok(val_bytes(value).map(|bytes| String::from_utf8_lossy(&bytes).into_owned())),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(error) => Err(error),
    }
}

fn has_item_table(conn: &Connection) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master \
         WHERE type = 'table' AND name = 'ItemTable' COLLATE NOCASE)",
        [],
        |row| row.get(0),
    )
}

fn has_cursor_table(conn: &Connection) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master \
         WHERE type = 'table' AND name = 'cursorDiskKV' COLLATE NOCASE)",
        [],
        |row| row.get(0),
    )
}

fn has_composer_headers(conn: &Connection) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master \
         WHERE type = 'table' AND name = 'composerHeaders' COLLATE NOCASE)",
        [],
        |row| row.get(0),
    )
}

fn overlay_composer_header_projects(
    conn: &Connection,
    projects_by_workspace: &HashMap<String, String>,
    projects: &mut HashMap<String, String>,
) -> rusqlite::Result<()> {
    if !has_composer_headers(conn)? {
        return Ok(());
    }
    let mut headers =
        conn.prepare("SELECT composerId, workspaceId FROM composerHeaders ORDER BY composerId")?;
    let rows = headers.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
    })?;
    for row in rows {
        let (composer_id, workspace_id) = row?;
        if let Some(project) = workspace_id
            .as_deref()
            .and_then(|workspace_id| projects_by_workspace.get(workspace_id))
        {
            projects.insert(composer_id, project.clone());
        }
    }
    Ok(())
}

fn has_conversations(conn: &Connection) -> rusqlite::Result<bool> {
    if !has_cursor_table(conn)? {
        return Ok(false);
    }
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM cursorDiskKV \
         WHERE key >= 'composerData:' AND key < 'composerData;' \
         AND value IS NOT NULL LIMIT 1)",
        [],
        |row| row.get(0),
    )
}

fn first_unmapped_conversation(
    conn: &Connection,
    projects: &HashMap<String, String>,
) -> rusqlite::Result<Option<String>> {
    if !has_cursor_table(conn)? {
        return Ok(None);
    }
    let mut statement = conn.prepare(
        "SELECT key FROM cursorDiskKV
         WHERE key >= 'composerData:' AND key < 'composerData;'
         AND typeof(value) IN ('text', 'blob') ORDER BY key",
    )?;
    let rows = statement.query_map([], |row| row.get::<_, String>(0))?;
    for key in rows {
        let key = key?;
        if let Some(session) = key.strip_prefix("composerData:") {
            if !projects.contains_key(session) {
                return Ok(Some(session.to_string()));
            }
        }
    }
    Ok(None)
}

/// Minimal percent-decode for workspace.json folder URIs (`file:///c%3A/Users/...`).
fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Ok(x) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(x);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Composer attribution is part of the session token, so a workspace move relabels cached rows.
fn workspace_projects(
    conn: &Connection,
    user: &std::path::Path,
) -> anyhow::Result<HashMap<String, String>> {
    let mut map = HashMap::new();
    let mut projects_by_workspace = HashMap::new();
    let mut incomplete_attribution = None;
    let root = user.join("workspaceStorage");
    let rd = match std::fs::read_dir(&root) {
        Ok(rd) => rd,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(map),
        Err(error) => anyhow::bail!("cannot enumerate {}: {error}", root.display()),
    };
    let mut dirs: Vec<std::path::PathBuf> = rd
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<Result<_, _>>()
        .map_err(|error| anyhow::anyhow!("cannot enumerate {}: {error}", root.display()))?;
    dirs.sort();
    for dir in dirs {
        let workspace = dir.join("workspace.json");
        let ws = match read_bounded_regular_file(&workspace, WORKSPACE_JSON_MAX_BYTES) {
            Ok(Some(ws)) => ws,
            Ok(None) => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("cursor", &workspace, &error);
                incomplete_attribution
                    .get_or_insert_with(|| format!("cannot read {}: {error}", workspace.display()));
                continue;
            }
        };
        let ws: serde_json::Value = match serde_json::from_slice(&ws) {
            Ok(value) => value,
            Err(error) => {
                crate::ingest::warn_source_skip("cursor", &workspace, &error);
                incomplete_attribution.get_or_insert_with(|| {
                    format!("cannot decode {}: {error}", workspace.display())
                });
                continue;
            }
        };
        let Some(folder) = ws.get("folder").and_then(|f| f.as_str()) else {
            continue;
        };
        let path = percent_decode(folder.trim_start_matches("file://").trim_start_matches('/'));
        let project = project_name(&path);
        if let Some(workspace_id) = dir.file_name().and_then(|name| name.to_str()) {
            projects_by_workspace.insert(workspace_id.to_string(), project.clone());
        }
        let db = dir.join("state.vscdb");
        match std::fs::metadata(&db) {
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("cursor", &db, &error);
                incomplete_attribution
                    .get_or_insert_with(|| format!("cannot inspect {}: {error}", db.display()));
                continue;
            }
        }
        let conn = match crate::ingest::open_sqlite_ro(&db) {
            Ok(c) => c,
            Err(error) => {
                crate::ingest::warn_source_skip("cursor", &db, &error);
                incomplete_attribution
                    .get_or_insert_with(|| format!("cannot open {}: {error}", db.display()));
                continue;
            }
        };
        match has_item_table(&conn) {
            Ok(true) => {}
            Ok(false) => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("cursor", &db, &error);
                incomplete_attribution
                    .get_or_insert_with(|| format!("cannot inspect {}: {error}", db.display()));
                continue;
            }
        }
        let v = match kv_text_checked(&conn, "ItemTable", "composer.composerData") {
            Ok(Some(value)) => value,
            Ok(None) => continue,
            Err(error) => {
                crate::ingest::warn_source_skip("cursor", &db, &error);
                incomplete_attribution
                    .get_or_insert_with(|| format!("cannot read {}: {error}", db.display()));
                continue;
            }
        };
        let v: serde_json::Value = match serde_json::from_str(&v) {
            Ok(value) => value,
            Err(error) => {
                crate::ingest::warn_source_skip("cursor", &db, &error);
                incomplete_attribution
                    .get_or_insert_with(|| format!("cannot decode {}: {error}", db.display()));
                continue;
            }
        };
        for c in v
            .get("allComposers")
            .and_then(|a| a.as_array())
            .into_iter()
            .flatten()
        {
            if let Some(id) = c.get("composerId").and_then(|i| i.as_str()) {
                map.insert(id.to_string(), project.clone());
            }
        }
    }
    if let Err(error) = overlay_composer_header_projects(conn, &projects_by_workspace, &mut map) {
        let db = user.join("globalStorage").join("state.vscdb");
        crate::ingest::warn_source_skip("cursor", &db, &error);
        incomplete_attribution
            .get_or_insert_with(|| format!("cannot read {}: {error}", db.display()));
    }
    if let (Some(error), Some(session)) = (
        incomplete_attribution,
        first_unmapped_conversation(conn, &map)?,
    ) {
        anyhow::bail!("workspace attribution for conversation {session} is incomplete: {error}");
    }
    Ok(map)
}

fn workspace_projects_if_conversations(
    conn: &Connection,
    user: &std::path::Path,
) -> anyhow::Result<HashMap<String, String>> {
    if !has_conversations(conn)? {
        return Ok(HashMap::new());
    }
    workspace_projects(conn, user)
}

#[derive(Debug, Eq, PartialEq)]
struct CursorCensus {
    token: String,
    composer_rows: u64,
    unreferenced_bubbles: u64,
}

struct TokenSnapshot {
    sessions: Vec<(String, String)>,
    census: Option<CursorCensus>,
    /// The store held no `cursorDiskKV` to enumerate, so nothing about conversations was
    /// observed. Unknown, not proven empty: an enumerable table with zero rows leaves this
    /// false and still converges as the genuine fresh-profile case.
    schema_absent: bool,
}

fn add_references(cid: &str, value: &[u8], references: &mut HashSet<String>) {
    let Ok(record): Result<serde_json::Value, _> = serde_json::from_slice(value) else {
        return;
    };
    for header in record
        .get("fullConversationHeadersOnly")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
    {
        if let Some(bubble) = header.get("bubbleId").and_then(serde_json::Value::as_str) {
            references.insert(format!("bubbleId:{cid}:{bubble}"));
        }
    }
}

/// Every session's token plus an optional raw-record census.
fn token_snapshot(
    conn: &Connection,
    projects: &HashMap<String, String>,
    census: bool,
) -> rusqlite::Result<TokenSnapshot> {
    if !has_cursor_table(conn)? {
        return Ok(TokenSnapshot {
            sessions: vec![(
                SCHEMA_ABSENT_SESSION.to_string(),
                SCHEMA_ABSENT_TOKEN.to_string(),
            )],
            census: None,
            schema_absent: true,
        });
    }
    let mut composers = conn.prepare(TOKEN_SCAN_SQL[0])?;
    let rows = composers.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, rusqlite::types::Value>(1)?,
        ))
    })?;
    let mut hashes = HashMap::new();
    let mut references = census.then(HashSet::new);
    let mut composer_rows = 0_u64;
    let mut bubble_rows = 0_u64;
    let mut unreferenced_bubbles = 0_u64;
    let mut detached_hash = FNV_OFFSET;
    for row in rows {
        let (key, value) = row?;
        let Some(cid) = key.strip_prefix("composerData:") else {
            continue;
        };
        composer_rows += 1;
        // NULL composerData rows exist in real stores; they carry no conversation.
        let Some(bytes) = val_bytes(value.clone()) else {
            if census {
                fnv_extend(&mut detached_hash, key.as_bytes());
                fnv_sql_value(&mut detached_hash, &value);
            }
            continue;
        };
        if let Some(references) = references.as_mut() {
            add_references(cid, &bytes, references);
        }
        let mut hash = FNV_OFFSET;
        fnv_extend(&mut hash, &bytes);
        hashes.insert(cid.to_string(), hash);
    }
    drop(composers);

    let mut bubbles = conn.prepare(TOKEN_SCAN_SQL[1])?;
    let rows = bubbles.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, rusqlite::types::Value>(1)?,
        ))
    })?;
    for row in rows {
        let (key, value) = row?;
        bubble_rows += 1;
        if references
            .as_ref()
            .is_some_and(|references| !references.contains(&key))
        {
            unreferenced_bubbles += 1;
        }
        let Some((cid, _)) = key
            .strip_prefix("bubbleId:")
            .and_then(|rest| rest.split_once(':'))
        else {
            if census {
                fnv_extend(&mut detached_hash, key.as_bytes());
                fnv_sql_value(&mut detached_hash, &value);
            }
            continue;
        };
        let Some(hash) = hashes.get_mut(cid) else {
            if census {
                fnv_extend(&mut detached_hash, key.as_bytes());
                fnv_sql_value(&mut detached_hash, &value);
            }
            continue;
        };
        fnv_extend(hash, key.as_bytes());
        if let Some(value) = val_bytes(value) {
            fnv_extend(hash, &value);
        }
    }
    drop(bubbles);

    let mut out: Vec<_> = hashes
        .into_iter()
        .map(|(cid, mut hash)| {
            fnv_extend(&mut hash, b"\0project\0");
            fnv_extend(
                &mut hash,
                projects
                    .get(&cid)
                    .map(String::as_str)
                    .unwrap_or("cursor")
                    .as_bytes(),
            );
            (cid, fnv_finish(hash))
        })
        .collect();
    out.sort_unstable_by(|left, right| left.0.cmp(&right.0));
    let census = references.map(|_| {
        let mut hash = FNV_OFFSET;
        fnv_extend(&mut hash, b"cursor-census-v1\0");
        for (session, token) in &out {
            fnv_extend(&mut hash, session.as_bytes());
            fnv_extend(&mut hash, b"\0");
            fnv_extend(&mut hash, token.as_bytes());
            fnv_extend(&mut hash, b"\0");
        }
        fnv_extend(&mut hash, &detached_hash.to_le_bytes());
        fnv_extend(&mut hash, &composer_rows.to_le_bytes());
        fnv_extend(&mut hash, &bubble_rows.to_le_bytes());
        fnv_extend(&mut hash, &unreferenced_bubbles.to_le_bytes());
        CursorCensus {
            token: fnv_finish(hash),
            composer_rows,
            unreferenced_bubbles,
        }
    });
    Ok(TokenSnapshot {
        sessions: out,
        census,
        schema_absent: false,
    })
}

#[cfg(test)]
fn session_tokens(
    conn: &Connection,
    projects: &HashMap<String, String>,
) -> rusqlite::Result<Vec<(String, String)>> {
    token_snapshot(conn, projects, false).map(|snapshot| snapshot.sessions)
}

fn stable_generation_read<T>(
    opened_generation: Option<&str>,
    path: &std::path::Path,
    read: impl FnOnce() -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let before = crate::ingest::sqlite_generation_token(path)?;
    anyhow::ensure!(
        opened_generation.is_none_or(|generation| generation == before),
        "SQLite generation moved before reading {}",
        path.display()
    );
    let value = read()?;
    let after = crate::ingest::sqlite_generation_token(path)?;
    anyhow::ensure!(
        before == after,
        "SQLite generation changed while reading {}",
        path.display()
    );
    Ok(value)
}

fn stable_token_snapshot(
    conn: &crate::ingest::ReadOnlyConnection,
    path: &std::path::Path,
    projects: &HashMap<String, String>,
    census: bool,
) -> anyhow::Result<TokenSnapshot> {
    stable_generation_read(Some(conn.source_generation()), path, || {
        let transaction = conn.unchecked_transaction()?;
        let snapshot = token_snapshot(&transaction, projects, census)?;
        transaction.commit()?;
        Ok(snapshot)
    })
}

fn record_census(path: &std::path::Path, census: &CursorCensus) {
    let tally = crate::intake::keyed_token("cursor", path, CENSUS_SESSION, census.token.clone());
    tally.seen_n(census.composer_rows + census.unreferenced_bubbles);
    tally.skip_n(crate::intake::Skip::Meta, census.composer_rows);
    tally.skip_n(
        crate::intake::Skip::Unreferenced,
        census.unreferenced_bubbles,
    );
}

/// One tool call bubble -> a tool Event. `params` is the resolved argument object when the
/// call completed; `rawArgs` is the model's raw string (both JSON-encoded strings).
fn tool_event(session: &str, bubble_id: &str, ts: i64, tf: &serde_json::Value) -> Event {
    let args = tf
        .get("params")
        .and_then(|p| p.as_str())
        .filter(|s| !s.trim().is_empty())
        .or_else(|| tf.get("rawArgs").and_then(|p| p.as_str()));
    let (input, input_chars) = args
        .map(|s| {
            serde_json::from_str::<serde_json::Value>(s)
                .map(|v| summarize_tool_input_with_chars(&v))
                .unwrap_or_else(|_| cap_str_with_chars(s, EVENT_CAP))
        })
        .unwrap_or_default();
    let (output, output_chars, output_bytes) =
        cap_event_output(tf.get("result").and_then(|r| r.as_str()).unwrap_or(""));
    let ok = match tf.get("status").and_then(|s| s.as_str()) {
        Some("completed") => Some(true),
        Some("error") | Some("errored") | Some("failed") | Some("cancelled") | Some("canceled")
        | Some("rejected") | Some("aborted") => Some(false),
        _ => None,
    };
    Event {
        agent: "cursor",
        session: session.to_string(),
        ts,
        kind: "tool",
        name: tf
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("?")
            .to_string(),
        input,
        output,
        input_chars,
        output_chars,
        output_bytes,
        ok,
        call_id: tf
            .get("toolCallId")
            .and_then(|i| i.as_str())
            .filter(|id| !id.trim().is_empty())
            .map(str::to_string)
            // bubbleId is Cursor's durable row identity when toolFormerData omits its correlation id.
            .unwrap_or_else(|| format!("cursor:{bubble_id}")),
        child_session: String::new(),
    }
}

/// Parse one composer into (user turns + replies, tool events), walking the header list in
/// conversation order and fetching each bubble row. Missing or NULL bubbles are skipped.
fn parse_session(
    conn: &Connection,
    db_path: &std::path::Path,
    projects: &HashMap<String, String>,
    cid: &str,
    token: &str,
) -> (Vec<Message>, Vec<Event>, bool) {
    // seen = bubble rows iterated within this conversation
    let tally = crate::intake::keyed_token("cursor", db_path, cid, token.to_string());
    let cd = match kv_text_checked(conn, "cursorDiskKV", &format!("composerData:{cid}")) {
        Ok(Some(cd)) => cd,
        Ok(None) => {
            tally.error("missing composerData row");
            return (Vec::new(), Vec::new(), false);
        }
        Err(error) => {
            tally.error(&format!("composerData query: {error}"));
            return (Vec::new(), Vec::new(), false);
        }
    };
    let Ok(cd): Result<serde_json::Value, _> = serde_json::from_str(&cd) else {
        tally.error(&format!(
            "bad composerData: {}",
            crate::intake::clip(&cd, 80)
        ));
        return (Vec::new(), Vec::new(), false);
    };
    let created = cd
        .get("createdAt")
        .and_then(|t| t.as_f64())
        .map(crate::ingest::parse_timestamp::epoch_number)
        .unwrap_or(0);
    let model = cd
        .pointer("/modelConfig/modelName")
        .and_then(|m| m.as_str())
        .unwrap_or("")
        .to_string();
    let project = projects
        .get(cid)
        .cloned()
        .unwrap_or_else(|| "cursor".to_string());
    let headers = cd
        .get("fullConversationHeadersOnly")
        .and_then(|h| h.as_array())
        .cloned()
        .unwrap_or_default();

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut healthy = true;
    let mut turn = 0u32;
    for h in &headers {
        let Some(bid) = h.get("bubbleId").and_then(|b| b.as_str()) else {
            continue;
        };
        let raw = match kv_text_checked(conn, "cursorDiskKV", &format!("bubbleId:{cid}:{bid}")) {
            Ok(Some(raw)) => raw,
            Ok(None) => continue,
            Err(error) => {
                tally.error(&format!("bubble query: {error}"));
                healthy = false;
                continue;
            }
        };
        tally.seen();
        let b: serde_json::Value = match serde_json::from_str(&raw) {
            Ok(b) => b,
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(&raw, 80)));
                healthy = false;
                continue;
            }
        };
        let ts = match b.get("createdAt").and_then(|t| t.as_str()) {
            Some(s) => crate::ingest::parse_timestamp::rfc3339(Some(s)),
            None => created,
        };
        let text = b.get("text").and_then(|t| t.as_str()).unwrap_or("");
        match b.get("type").and_then(|t| t.as_i64()) {
            Some(1) => {
                if text.trim().is_empty() {
                    tally.skip(crate::intake::Skip::EmptyText);
                    continue;
                }
                if is_wrapper(text) {
                    tally.skip(crate::intake::Skip::Wrapper);
                    continue;
                }
                tally.row();
                out.push(crate::model::RawMessage {
                    agent: "cursor",
                    project: project.clone(),
                    session: cid.to_string(),
                    ts: if ts > 0 { ts } else { created },
                    turn,
                    text: text.to_string(),
                    model: model.clone(),
                    reply: String::new(),
                    reply_chars: 0,
                    side: false,
                    parent: String::new(),
                });
                turn += 1;
            }
            Some(2) => {
                // isThought bubbles are hidden reasoning: indexing them would let search
                // hit chain-of-thought that never appears in chat.
                let visible_text = !b
                    .get("isThought")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false)
                    && !text.trim().is_empty();
                let mut consumed = visible_text;
                if let Some(tf) = b.get("toolFormerData") {
                    if tf.is_object() {
                        consumed = true;
                        tally.event();
                        events.push(tool_event(cid, bid, ts, tf));
                    }
                }
                if visible_text {
                    if let Some(last) = out.last_mut() {
                        let chars = crate::ingest::append_capped(
                            &mut last.reply,
                            text,
                            crate::ingest::REPLY_CAP,
                        );
                        last.reply_chars += chars;
                    }
                }
                if consumed {
                    tally.agent_row();
                } else {
                    tally.skip(crate::intake::Skip::NonHuman);
                }
            }
            _ => tally.skip(crate::intake::Skip::NonHuman),
        }
    }
    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        events,
        healthy,
    )
}

/// Open the store read-only and collect messages + tool events, reparsing only sessions
/// whose token moved. A store that cannot open serves the cached conversations.
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let discovery = user_dir_checked();
    if let Err(error) = &discovery {
        eprintln!(
            "  ! cursor: source discovery failed: {}",
            crate::ingest::terminal_safe(error)
        );
        cache.record_source_read_issue(
            "cursor",
            &error.path,
            "source-stat-failed",
            error.to_string(),
        );
    }
    let user = discovery.as_ref().ok().and_then(|user| user.clone());
    let db_path = user
        .as_ref()
        .map(|u| u.join("globalStorage").join("state.vscdb"));
    let conn = user
        .as_ref()
        .and_then(|u| open_ro(&u.join("globalStorage").join("state.vscdb")));
    let projects = match (&conn, &user) {
        (Some(conn), Some(user)) => match workspace_projects_if_conversations(conn, user) {
            Ok(projects) => Some(projects),
            Err(error) => {
                eprintln!(
                    "  ! cursor: cannot read {}: {}",
                    crate::ingest::terminal_safe(
                        db_path
                            .as_deref()
                            .expect("open connection has a database path")
                            .display(),
                    ),
                    crate::ingest::terminal_safe(&error)
                );
                None
            }
        },
        _ => None,
    };
    let snapshot = match (&discovery, &conn, &projects) {
        (Ok(None), _, _) => Some(TokenSnapshot {
            sessions: Vec::new(),
            census: None,
            schema_absent: false,
        }),
        (Ok(Some(_)), Some(conn), Some(projects)) => match stable_token_snapshot(
            conn,
            db_path
                .as_deref()
                .expect("open connection has a database path"),
            projects,
            true,
        ) {
            Ok(snapshot) => Some(snapshot),
            Err(error) => {
                eprintln!(
                    "  ! cursor: cannot read {}: {}",
                    crate::ingest::terminal_safe(
                        db_path
                            .as_deref()
                            .expect("open connection has a database path")
                            .display(),
                    ),
                    crate::ingest::terminal_safe(&error)
                );
                None
            }
        },
        _ => None,
    };
    if let (Some(path), Some(census)) = (
        db_path.as_deref(),
        snapshot
            .as_ref()
            .and_then(|snapshot| snapshot.census.as_ref()),
    ) {
        record_census(path, census);
    }
    // An unenumerable store lists no conversation, so its list may not be read as the whole
    // set: already-indexed conversations are preserved instead of pruned as deleted.
    let unenumerable = snapshot
        .as_ref()
        .is_some_and(|snapshot| snapshot.schema_absent);
    let has_readable_source = snapshot.is_some();
    let tokens = snapshot.map(|snapshot| snapshot.sessions);
    let token_by_session: HashMap<String, String> =
        tokens.as_ref().into_iter().flatten().cloned().collect();
    let empty_projects = HashMap::new();
    let projects = projects.as_ref().unwrap_or(&empty_projects);
    let pass = cache.collect_token_cached_keyed_partial(
        "cursor",
        tokens.map(|tokens| {
            tokens
                .into_iter()
                .map(|(session, token)| (session.clone(), session, token))
                .collect()
        }),
        &[],
        unenumerable,
        has_readable_source,
        |_, sid| match &conn {
            _ if sid == SCHEMA_ABSENT_SESSION => (Vec::new(), Vec::new(), true),
            Some(c) => parse_session(
                c,
                db_path
                    .as_deref()
                    .expect("open connection has a database path"),
                projects,
                sid,
                token_by_session.get(sid).map(String::as_str).unwrap_or(""),
            ),
            None => (Vec::new(), Vec::new(), false),
        },
    );
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). Token: per-conversation staleness via a
/// composer+bubbles content hash.
pub struct Cursor;

fn freshness_tokens_at(
    discovery: Result<Option<std::path::PathBuf>, UserDirError>,
) -> crate::ingest::registry::TokenAvailability {
    tokens_at(discovery, false)
}

fn intake_tokens_at(
    discovery: Result<Option<std::path::PathBuf>, UserDirError>,
) -> crate::ingest::registry::TokenAvailability {
    tokens_at(discovery, true)
}

fn tokens_at(
    discovery: Result<Option<std::path::PathBuf>, UserDirError>,
    intake_keys: bool,
) -> crate::ingest::registry::TokenAvailability {
    use crate::ingest::registry::{TokenAvailability, TokenReadIssue};

    let user = match discovery {
        Ok(Some(user)) => user,
        Ok(None) => return TokenAvailability::Empty,
        Err(error) => {
            let reason = format!("source discovery failed: {error}");
            return TokenAvailability::Unreadable(vec![TokenReadIssue::new(error.path, reason)]);
        }
    };
    let path = user.join("globalStorage").join("state.vscdb");
    let conn = match crate::ingest::open_sqlite_ro(&path) {
        Ok(conn) => conn,
        Err(error) => {
            return TokenAvailability::Unreadable(vec![TokenReadIssue::new(
                &path,
                format!("cannot open {}: {error}", path.display()),
            )]);
        }
    };
    let projects = match workspace_projects_if_conversations(&conn, &user) {
        Ok(projects) => projects,
        Err(error) => {
            return TokenAvailability::Unreadable(vec![TokenReadIssue::new(
                &path,
                format!("cannot read {}: {error}", path.display()),
            )]);
        }
    };
    match stable_token_snapshot(&conn, &path, &projects, intake_keys) {
        Ok(snapshot) => TokenAvailability::from_tokens(if intake_keys {
            let mut tokens: Vec<_> = snapshot
                .sessions
                .into_iter()
                .map(|(session, token)| (crate::intake::token_id(&path, &session), token))
                .collect();
            if let Some(census) = snapshot.census {
                tokens.push((crate::intake::token_id(&path, CENSUS_SESSION), census.token));
            }
            tokens
        } else {
            snapshot.sessions
        }),
        Err(error) => TokenAvailability::Unreadable(vec![TokenReadIssue::new(
            &path,
            format!("cannot read {}: {error}", path.display()),
        )]),
    }
}

impl crate::ingest::registry::Adapter for Cursor {
    fn name(&self) -> &'static str {
        "cursor"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Token
    }
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        user_dir()
            .map(|u| u.join("globalStorage").join("state.vscdb"))
            .into_iter()
            .collect()
    }
    fn freshness_tokens(&self) -> crate::ingest::registry::TokenAvailability {
        freshness_tokens_at(user_dir_checked())
    }
    fn intake_tokens(&self) -> crate::ingest::registry::TokenAvailability {
        intake_tokens_at(user_dir_checked())
    }
    fn runtime_issue_root(&self) -> std::path::PathBuf {
        let user =
            user_dir().unwrap_or_else(|| user_dir_candidates(&crate::ingest::home())[0].clone());
        user.join("globalStorage").join("state.vscdb")
    }
}

#[cfg(test)]
mod tests {
    use super::{
        fnv_token, freshness_tokens_at, intake_tokens_at, parse_session, session_tokens,
        stable_generation_read, token_snapshot, user_dir_candidates_at, user_dir_checked_at,
        workspace_projects, SCHEMA_ABSENT_SESSION, SCHEMA_ABSENT_TOKEN, TOKEN_SCAN_SQL,
    };
    use crate::ingest::registry::TokenAvailability;
    use rusqlite::{params, Connection};
    use std::collections::HashMap;

    #[test]
    fn native_cursor_profile_wins_over_copied_profiles() {
        let root =
            std::env::temp_dir().join(format!("agrep-cursor-profile-order-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let candidates = user_dir_candidates_at(&root, None);
        for user in &candidates {
            let storage = user.join("globalStorage");
            std::fs::create_dir_all(&storage).unwrap();
            std::fs::write(storage.join("state.vscdb"), b"fixture").unwrap();
        }
        assert_eq!(
            user_dir_checked_at(&root).unwrap(),
            Some(candidates[0].clone())
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn windows_candidate_uses_appdata_override() {
        let root = std::path::Path::new("home-fixture");
        let appdata = std::path::Path::new("roaming-fixture");
        let candidates = user_dir_candidates_at(root, Some(appdata));
        assert!(candidates.contains(&appdata.join("Cursor").join("User")));
        assert!(!candidates.contains(
            &root
                .join("AppData")
                .join("Roaming")
                .join("Cursor")
                .join("User")
        ));
    }

    #[cfg(unix)]
    #[test]
    fn workspace_registry_skips_special_files() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cursor-workspace-special-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        let workspace = root.join("workspaceStorage/hash/workspace.json");
        std::fs::create_dir_all(workspace.parent().unwrap()).unwrap();
        assert!(std::process::Command::new("mkfifo")
            .arg(&workspace)
            .status()
            .unwrap()
            .success());
        let conn = Connection::open_in_memory().unwrap();
        assert!(workspace_projects(&conn, &root).unwrap().is_empty());
        conn.execute_batch(
            "CREATE TABLE cursorDiskKV(
                 key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
             INSERT INTO cursorDiskKV VALUES('composerData:c', 'composer');
             CREATE TABLE composerHeaders(
                 composerId TEXT PRIMARY KEY, workspaceId TEXT);
             INSERT INTO composerHeaders VALUES('c', 'hash');",
        )
        .unwrap();
        let error = workspace_projects(&conn, &root).unwrap_err();
        assert!(error.to_string().contains("conversation c"), "{error}");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn composer_headers_attribute_workspaces_with_legacy_fallback() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cursor-composer-headers-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        let header_dir = root.join("workspaceStorage/header-workspace");
        let legacy_dir = root.join("workspaceStorage/legacy-workspace");
        std::fs::create_dir_all(&header_dir).unwrap();
        std::fs::create_dir_all(&legacy_dir).unwrap();
        std::fs::write(
            header_dir.join("workspace.json"),
            br#"{"folder":"file:///work/header-project"}"#,
        )
        .unwrap();
        std::fs::write(
            legacy_dir.join("workspace.json"),
            br#"{"folder":"file:///work/legacy-project"}"#,
        )
        .unwrap();
        let legacy = Connection::open(legacy_dir.join("state.vscdb")).unwrap();
        legacy
            .execute_batch("CREATE TABLE ItemTable(key TEXT PRIMARY KEY, value TEXT);")
            .unwrap();
        legacy
            .execute(
                "INSERT INTO ItemTable VALUES ('composer.composerData', ?1)",
                [r#"{"allComposers":[{"composerId":"shared"},{"composerId":"legacy"}]}"#],
            )
            .unwrap();
        drop(legacy);

        let global = Connection::open_in_memory().unwrap();
        global
            .execute_batch(
                "CREATE TABLE composerHeaders(\
                 composerId TEXT PRIMARY KEY, workspaceId TEXT);\
                 INSERT INTO composerHeaders VALUES ('shared', 'header-workspace');\
                 INSERT INTO composerHeaders VALUES ('modern', 'header-workspace');",
            )
            .unwrap();
        let projects = workspace_projects(&global, &root).unwrap();
        assert_eq!(
            projects.get("shared").map(String::as_str),
            Some("header-project")
        );
        assert_eq!(
            projects.get("modern").map(String::as_str),
            Some("header-project")
        );
        assert_eq!(
            projects.get("legacy").map(String::as_str),
            Some("legacy-project")
        );

        global.execute("DROP TABLE composerHeaders", []).unwrap();
        global
            .execute("CREATE TABLE composerHeaders(composerId TEXT)", [])
            .unwrap();
        let drifted = workspace_projects(&global, &root).unwrap();
        assert_eq!(
            drifted.get("shared").map(String::as_str),
            Some("legacy-project")
        );
        assert_eq!(
            drifted.get("legacy").map(String::as_str),
            Some("legacy-project")
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn thought_bubbles_are_not_searchable_replies() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute(
            "CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value TEXT)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?1,?2)",
            params![
                "composerData:c",
                r#"{"createdAt":1,"fullConversationHeadersOnly":[{"bubbleId":"u"},{"bubbleId":"t"}]}"#
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?1,?2)",
            params!["bubbleId:c:u", r#"{"type":1,"text":"real question"}"#],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?1,?2)",
            params![
                "bubbleId:c:t",
                r#"{"type":2,"text":"private reasoning trace","isThought":true}"#
            ],
        )
        .unwrap();

        let (messages, events, healthy) = parse_session(
            &conn,
            std::path::Path::new("cursor-test.db"),
            &HashMap::new(),
            "c",
            "token",
        );
        assert!(healthy);
        assert!(events.is_empty());
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "real question");
        assert!(messages[0].reply.is_empty());
    }

    #[test]
    fn schema_without_composer_storage_is_readable_but_unenumerable() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute(
            "CREATE TABLE ItemTable(key TEXT PRIMARY KEY, value TEXT)",
            [],
        )
        .unwrap();
        assert_eq!(
            session_tokens(&conn, &HashMap::new()).unwrap(),
            vec![(
                SCHEMA_ABSENT_SESSION.to_string(),
                SCHEMA_ABSENT_TOKEN.to_string()
            )]
        );

        // The present-but-empty table is the case that must still enumerate to zero.
        conn.execute("CREATE TABLE cursorDiskKV(key TEXT UNIQUE, value BLOB)", [])
            .unwrap();
        assert_eq!(
            session_tokens(&conn, &HashMap::new()).unwrap(),
            Vec::<(String, String)>::new()
        );
    }

    #[test]
    fn empty_main_store_ignores_unreadable_workspace_attribution() {
        let root =
            std::env::temp_dir().join(format!("agrep-cursor-empty-store-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let global = root.join("globalStorage");
        std::fs::create_dir_all(&global).unwrap();
        let conn = Connection::open(global.join("state.vscdb")).unwrap();
        conn.execute_batch(
            "CREATE TABLE cursorDiskKV(\
             key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);",
        )
        .unwrap();
        std::fs::write(root.join("workspaceStorage"), b"not a directory").unwrap();

        assert!(matches!(
            freshness_tokens_at(Ok(Some(root.clone()))),
            TokenAvailability::Empty
        ));
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?1, ?2)",
            params!["agentKv:blob:noise", b"not a conversation"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?1, NULL)",
            params!["composerData:null"],
        )
        .unwrap();
        drop(conn);
        assert!(matches!(
            freshness_tokens_at(Ok(Some(root.clone()))),
            TokenAvailability::Empty
        ));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn conversations_refuse_unreadable_workspace_attribution() {
        let root =
            std::env::temp_dir().join(format!("agrep-cursor-data-store-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let global = root.join("globalStorage");
        std::fs::create_dir_all(&global).unwrap();
        let conn = Connection::open(global.join("state.vscdb")).unwrap();
        conn.execute_batch(
            "CREATE TABLE cursorDiskKV(\
             key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?1, ?2)",
            params!["composerData:c#part", b"composer"],
        )
        .unwrap();
        drop(conn);
        std::fs::write(root.join("workspaceStorage"), b"not a directory").unwrap();

        assert!(matches!(
            freshness_tokens_at(Ok(Some(root.clone()))),
            TokenAvailability::Unreadable(_)
        ));
        assert!(matches!(
            intake_tokens_at(Ok(Some(root.clone()))),
            TokenAvailability::Unreadable(_)
        ));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn token_scan_is_two_index_ranges_and_byte_exact_at_scale() {
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE cursorDiskKV(\
             key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);",
        )
        .unwrap();
        let tx = conn.transaction().unwrap();
        for index in 0..512 {
            let cid = format!("c{index:04}");
            tx.execute(
                "INSERT INTO cursorDiskKV VALUES (?1, ?2)",
                params![format!("composerData:{cid}"), format!("composer-{index}")],
            )
            .unwrap();
            tx.execute(
                "INSERT INTO cursorDiskKV VALUES (?1, ?2)",
                params![
                    format!("bubbleId:{cid}:b{index:04}"),
                    format!("bubble-{index}")
                ],
            )
            .unwrap();
        }
        tx.execute(
            "INSERT INTO cursorDiskKV VALUES ('agentKv:blob:noise', 'ignored')",
            [],
        )
        .unwrap();
        tx.commit().unwrap();

        let projects = HashMap::from([("c0000".to_string(), "mapped".to_string())]);
        let tokens = session_tokens(&conn, &projects).unwrap();
        assert_eq!(tokens.len(), 512);
        let mut expected = b"composer-0".to_vec();
        expected.extend_from_slice(b"bubbleId:c0000:b0000bubble-0\0project\0mapped");
        assert_eq!(tokens[0], ("c0000".to_string(), fnv_token(&expected)));

        for sql in TOKEN_SCAN_SQL {
            let mut plan = conn.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap();
            let details: Vec<String> = plan
                .query_map([], |row| row.get(3))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap();
            let detail = details.join(" | ");
            assert!(
                detail.contains("SEARCH cursorDiskKV")
                    && detail.contains("key>?")
                    && detail.contains("key<?"),
                "token scan lost its indexed range plan: {detail}"
            );
        }

        conn.execute(
            "UPDATE cursorDiskKV SET value = 'Bubble-0' \
             WHERE key = 'bubbleId:c0000:b0000'",
            [],
        )
        .unwrap();
        let edited = session_tokens(&conn, &projects).unwrap();
        assert_ne!(edited[0].1, tokens[0].1);
        let relabeled = session_tokens(&conn, &HashMap::new()).unwrap();
        assert_ne!(relabeled[0].1, edited[0].1);
    }

    #[test]
    fn census_accounts_for_unlisted_and_orphan_bubbles() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE cursorDiskKV(\
             key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?1, ?2)",
            params![
                "composerData:c#part",
                r#"{"fullConversationHeadersOnly":[{"bubbleId":"listed"}]}"#
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES ('composerData:null', NULL)",
            [],
        )
        .unwrap();
        for (key, value) in [
            ("bubbleId:c#part:listed", "listed"),
            ("bubbleId:c#part:unlisted", "unlisted"),
            ("bubbleId:ghost:orphan", "orphan"),
        ] {
            conn.execute(
                "INSERT INTO cursorDiskKV VALUES (?1, ?2)",
                params![key, value],
            )
            .unwrap();
        }

        let before = token_snapshot(&conn, &HashMap::new(), true).unwrap();
        let census = before.census.as_ref().unwrap();
        assert_eq!(census.composer_rows, 2);
        assert_eq!(census.unreferenced_bubbles, 2);
        assert_eq!(before.sessions.len(), 1);
        assert_eq!(before.sessions[0].0, "c#part");

        conn.execute(
            "UPDATE cursorDiskKV SET value = 'changed' WHERE key = 'bubbleId:ghost:orphan'",
            [],
        )
        .unwrap();
        let after = token_snapshot(&conn, &HashMap::new(), true).unwrap();
        assert_eq!(before.sessions, after.sessions);
        assert_ne!(before.census.unwrap().token, after.census.unwrap().token);
    }

    #[test]
    fn token_snapshot_rejects_a_database_generation_that_moves_mid_read() {
        let root = std::env::temp_dir().join(format!(
            "agrep-cursor-token-generation-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("state.vscdb");
        let connection = Connection::open(&path).unwrap();
        connection
            .execute(
                "CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value BLOB)",
                [],
            )
            .unwrap();

        let error = stable_generation_read(None, &path, || {
            connection.execute(
                "INSERT INTO cursorDiskKV VALUES ('composerData:c', 'x')",
                [],
            )?;
            Ok(())
        })
        .unwrap_err();
        assert!(error.to_string().contains("generation changed"));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn composer_headers_attribution_survives_corrupt_per_workspace_db() {
        // A corrupt workspace DB must not suppress the global composerHeaders overlay.
        let root =
            std::env::temp_dir().join(format!("agrep-cursor-corrupt-ws-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let ws_dir = root.join("workspaceStorage/my-workspace");
        std::fs::create_dir_all(&ws_dir).unwrap();
        std::fs::write(
            ws_dir.join("workspace.json"),
            br#"{"folder":"file:///work/my-project"}"#,
        )
        .unwrap();
        std::fs::write(ws_dir.join("state.vscdb"), b"not a sqlite database").unwrap();

        let global = Connection::open_in_memory().unwrap();
        global
            .execute_batch(
                "CREATE TABLE cursorDiskKV(
                     key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
                 INSERT INTO cursorDiskKV VALUES(
                     'composerData:my-composer', 'composer');
                 CREATE TABLE composerHeaders(
                     composerId TEXT PRIMARY KEY, workspaceId TEXT);
                 INSERT INTO composerHeaders VALUES(
                     'my-composer', 'my-workspace');",
            )
            .unwrap();

        let projects = workspace_projects(&global, &root).unwrap();
        assert_eq!(
            projects.get("my-composer").map(String::as_str),
            Some("my-project"),
            "composerHeaders attribution must survive a corrupt per-workspace DB",
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn corrupt_legacy_workspace_refuses_unmapped_conversation() {
        let root =
            std::env::temp_dir().join(format!("agrep-cursor-unmapped-ws-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let ws_dir = root.join("workspaceStorage/my-workspace");
        std::fs::create_dir_all(&ws_dir).unwrap();
        std::fs::write(
            ws_dir.join("workspace.json"),
            br#"{"folder":"file:///work/my-project"}"#,
        )
        .unwrap();
        std::fs::write(ws_dir.join("state.vscdb"), b"not a sqlite database").unwrap();

        let global = Connection::open_in_memory().unwrap();
        global
            .execute_batch(
                "CREATE TABLE cursorDiskKV(
                     key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
                 INSERT INTO cursorDiskKV VALUES(
                     'composerData:legacy-composer', 'composer');",
            )
            .unwrap();

        let error = workspace_projects(&global, &root).unwrap_err();
        assert!(error.to_string().contains("legacy-composer"), "{error}");
        let _ = std::fs::remove_dir_all(root);
    }
}
