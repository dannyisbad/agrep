//! crush adapter: per-project crush.db stores discovered through projects.json, with legacy
//! global databases retained for older Crush releases.
//!
//! format documented from crush's own source (charmbracelet/crush: internal/db/migrations
//! initial schema + internal/message/content.go part types) - read as format knowledge,
//! no code ported; the fixture is hand-written synthetic. Schema:
//!   sessions(id, parent_session_id, title, updated_at ms, created_at ms, ...)
//!   messages(id, session_id, role, parts TEXT JSON, model, created_at ms, updated_at ms)
//! `parts` is a JSON array of {type, data} wrappers: type "text" (data.text), "tool_call"
//! (data.{id,name,input:JSON-string}), "tool_result" (data.{tool_call_id,name,content,is_error}),
//! plus "reasoning"/"finish"/etc. which are not the user or the reply and are skipped.
//!
//! Fingerprint::Token: crush is one sqlite file with many conversations, so a
//! stat fingerprint would normally reparse everything on any write. Session timestamps keep
//! the common path incremental; the SQLite generation closes the WAL/body-edit blind spot.

use rusqlite::Connection;
use std::collections::HashMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use crate::ingest::registry::{
    metadata_is_link, read_bounded_regular_file, relative_path, stat_proves_absent,
    token_fingerprint,
};
use crate::ingest::{
    cap_event_output, cap_str_with_chars, is_wrapper, project_name,
    summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::model::{Event, Message};

#[derive(Clone, Debug, Eq, PartialEq)]
struct Database {
    path: PathBuf,
    project: String,
}

#[derive(Clone, Debug)]
struct Discovery {
    databases: Vec<Database>,
    /// registered database paths a stat error hid: unavailable, never absent
    unavailable: Vec<(PathBuf, Unenumerable)>,
    /// registered database paths a stat proves gone (a volume unmounted, a project renamed):
    /// store roots whose absence two observations must confirm before their chats go
    absent: Vec<PathBuf>,
    incomplete: bool,
    issues: Vec<(PathBuf, String)>,
}

const PROJECTS_JSON_MAX_BYTES: u64 = 4 * 1024 * 1024;

fn data_roots_at(
    home: &Path,
    test_home: bool,
    global_data: Option<OsString>,
    xdg_data: Option<OsString>,
    local_app_data: Option<OsString>,
    windows: bool,
) -> Vec<PathBuf> {
    let legacy = home.join(".local").join("share").join("crush");
    let primary = if test_home {
        legacy.clone()
    } else if let Some(path) = global_data.filter(|value| !value.is_empty()) {
        PathBuf::from(path)
    } else if let Some(path) = xdg_data.filter(|value| !value.is_empty()) {
        PathBuf::from(path).join("crush")
    } else if windows {
        local_app_data
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Local"))
            .join("crush")
    } else {
        legacy.clone()
    };
    if primary == legacy {
        vec![primary]
    } else {
        vec![primary, legacy]
    }
}

fn data_roots() -> Vec<PathBuf> {
    data_roots_at(
        &crate::ingest::home(),
        std::env::var_os("AGREP_HOME").is_some(),
        std::env::var_os("CRUSH_GLOBAL_DATA"),
        std::env::var_os("XDG_DATA_HOME"),
        std::env::var_os("LOCALAPPDATA"),
        cfg!(target_os = "windows"),
    )
}

/// Whether `path` is a plain file (or directory): a symlink, the wrong type or a stat error that
/// proves absence is absent; any other failure may hide a store that still holds conversations.
fn presence(path: &Path, directory: bool) -> Result<bool, Unenumerable> {
    match std::fs::symlink_metadata(path) {
        Ok(meta) if metadata_is_link(&meta) => {
            crate::ingest::warn_source_skip("crush", path, "symlink sources are not followed");
            Ok(false)
        }
        Ok(meta) => Ok(if directory {
            meta.is_dir()
        } else {
            meta.is_file()
        }),
        Err(error) if stat_proves_absent(path, &error) => Ok(false),
        Err(error) => {
            crate::ingest::warn_source_skip("crush", path, &error);
            Err(Unenumerable::uninspectable(path, &error))
        }
    }
}

fn discovered_plain(path: &Path, directory: bool) -> bool {
    presence(path, directory).unwrap_or(false)
}

fn dir_below(path: &Path, anchor: &Path) -> Option<Result<bool, Unenumerable>> {
    let relative = relative_path(path, anchor)?;
    let walk = || {
        if !presence(anchor, true)? {
            return Ok(false);
        }
        let mut current = anchor.to_path_buf();
        for component in relative.components() {
            match component {
                std::path::Component::CurDir => continue,
                std::path::Component::Normal(component) => current.push(component),
                _ => return Ok(false),
            }
            if !presence(&current, true)? {
                return Ok(false);
            }
        }
        Ok(true)
    };
    Some(walk())
}

fn data_dir_present(
    path: &Path,
    registry_root: &Path,
    project_path: &Path,
) -> Result<bool, Unenumerable> {
    dir_below(path, registry_root)
        .or_else(|| dir_below(path, project_path))
        .unwrap_or_else(|| presence(path, true))
}

/// What discovery learned of the databases it looked for.
#[derive(Default)]
struct Found {
    databases: Vec<Database>,
    unavailable: Vec<(PathBuf, Unenumerable)>,
    absent: Vec<PathBuf>,
}

impl Found {
    /// The database at `path` when its data dir and it are plain; a stat failure on either names
    /// the database unavailable, so its conversations are retained rather than pruned, and one
    /// that proves it gone names a `registered` database absent, for two observations to confirm.
    fn observe(
        &mut self,
        path: PathBuf,
        data_dir_present: Result<bool, Unenumerable>,
        project: String,
        registered: bool,
    ) {
        match data_dir_present.and_then(|present| {
            if present {
                presence(&path, false)
            } else {
                Ok(false)
            }
        }) {
            Ok(true) => self.databases.push(Database { path, project }),
            Ok(false) if registered => self.absent.push(path),
            Ok(false) => {}
            Err(failure) => self.unavailable.push((path, failure)),
        }
    }
}

fn add_project_record(
    value: &serde_json::Value,
    fallback_path: Option<&str>,
    registry_root: &Path,
    found: &mut Found,
) {
    let Some(record) = value.as_object() else {
        return;
    };
    let Some(project_raw) = record
        .get("path")
        .and_then(serde_json::Value::as_str)
        .or(fallback_path)
        .filter(|value| !value.is_empty())
    else {
        return;
    };
    let Some(data_raw) = record
        .get("data_dir")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return;
    };
    let project_path = PathBuf::from(project_raw);
    let raw_data = PathBuf::from(data_raw);
    let data_dir = if raw_data.is_absolute() {
        raw_data
    } else if project_path.is_absolute() {
        project_path.join(raw_data)
    } else {
        return;
    };
    found.observe(
        data_dir.join("crush.db"),
        data_dir_present(&data_dir, registry_root, &project_path),
        project_name(project_raw),
        true,
    );
}

fn read_projects(path: &Path, registry_root: &Path, found: &mut Found) -> std::io::Result<()> {
    let Some(bytes) = read_bounded_regular_file(path, PROJECTS_JSON_MAX_BYTES)? else {
        return Ok(());
    };
    let value: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    if let Some(records) = value.as_array() {
        for record in records {
            add_project_record(record, None, registry_root, found);
        }
        return Ok(());
    }
    let Some(object) = value.as_object() else {
        return Ok(());
    };
    if let Some(projects) = object.get("projects") {
        if let Some(records) = projects.as_array() {
            for record in records {
                add_project_record(record, None, registry_root, found);
            }
        } else if let Some(records) = projects.as_object() {
            for (project, record) in records {
                add_project_record(record, Some(project), registry_root, found);
            }
        }
    } else {
        for (project, record) in object {
            add_project_record(record, Some(project), registry_root, found);
        }
    }
    Ok(())
}

/// Crush databases sitting under a root that projects.json never registers.
///
/// Crush writes every store it opens into the registry, so an unregistered database
/// is at best a stale leftover - it is named, never ingested. The scan is shallow
/// (one level of subdirectories, bounded entries) because this is a courtesy, not a
/// second discovery path, and symlinked directories are not descended.
fn unregistered_databases(roots: &[PathBuf], databases: &[Database]) -> Vec<PathBuf> {
    const SCAN_ENTRY_CAP: usize = 64;
    let mut found = Vec::new();
    for root in roots {
        let Ok(entries) = std::fs::read_dir(root) else {
            continue;
        };
        for entry in entries.take(SCAN_ENTRY_CAP).flatten() {
            if !entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                continue;
            }
            let candidate = entry.path().join("crush.db");
            if discovered_plain(&candidate, false)
                && !databases.iter().any(|database| database.path == candidate)
                && !found.contains(&candidate)
            {
                found.push(candidate);
            }
        }
    }
    found.sort();
    found
}

/// One line per unregistered store per process: a single index pass rediscovers through
/// the token and census lanes several times. Never recorded as a source-read issue - the
/// database is readable, it is simply not registered, so a read remedy could not resolve it.
fn warn_unregistered(paths: &[PathBuf]) {
    static WARNED: std::sync::Mutex<std::collections::BTreeSet<PathBuf>> =
        std::sync::Mutex::new(std::collections::BTreeSet::new());
    let Ok(mut warned) = WARNED.lock() else {
        return;
    };
    for path in paths {
        if warned.insert(path.clone()) {
            crate::intake::diagnostic(format_args!(
                "  ! crush: {} exists but is not referenced by projects.json; not ingested",
                crate::ingest::terminal_safe(path.display())
            ));
        }
    }
}

fn discover_at(roots: Vec<PathBuf>, home: &Path) -> std::io::Result<Discovery> {
    let mut found = Found::default();
    let mut incomplete = false;
    let mut issues = Vec::new();
    for root in &roots {
        match presence(root, true) {
            Ok(true) => {}
            Ok(false) => continue,
            // The registry below it is unread: every database it names stays unlisted.
            Err(failure) => {
                incomplete = true;
                issues.push((root.clone(), failure.reason));
                continue;
            }
        }
        let registry = root.join("projects.json");
        if let Err(error) = read_projects(&registry, root, &mut found) {
            incomplete = true;
            crate::intake::diagnostic(format_args!(
                "  ! crush: cannot read {}: {}",
                crate::ingest::terminal_safe(registry.display()),
                crate::ingest::terminal_safe(&error)
            ));
            issues.push((registry, error.to_string()));
        }
        found.observe(root.join("crush.db"), Ok(true), "crush".to_string(), false);
    }
    let old = home.join(".crush").join("crush.db");
    let old_dir_present = old
        .parent()
        .map(|parent| presence(parent, true))
        .unwrap_or(Ok(false));
    found.observe(old, old_dir_present, "crush".to_string(), false);
    let Found {
        mut databases,
        mut unavailable,
        mut absent,
    } = found;
    let unregistered = unregistered_databases(&roots, &databases);
    warn_unregistered(&unregistered);
    databases.sort_by(|left, right| {
        left.path
            .cmp(&right.path)
            .then_with(|| left.project.cmp(&right.project))
    });
    databases.dedup_by(|left, right| left.path == right.path);
    unavailable.sort_by(|left, right| left.0.cmp(&right.0));
    unavailable.dedup_by(|left, right| left.0 == right.0);
    unavailable.retain(|(path, _)| !databases.iter().any(|database| database.path == *path));
    absent.sort();
    absent.dedup();
    absent.retain(|path| {
        !databases.iter().any(|database| database.path == *path)
            && !unavailable
                .iter()
                .any(|(unavailable, _)| unavailable == path)
    });
    Ok(Discovery {
        databases,
        unavailable,
        absent,
        incomplete,
        issues,
    })
}

fn discover() -> std::io::Result<Discovery> {
    let home = crate::ingest::home();
    discover_at(data_roots(), &home)
}

#[cfg(unix)]
fn path_identity(path: &Path) -> String {
    use std::os::unix::ffi::OsStrExt;
    path.as_os_str()
        .as_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(windows)]
fn path_identity(path: &Path) -> String {
    use std::fmt::Write;
    use std::os::windows::ffi::OsStrExt;
    let mut out = String::new();
    for unit in path.as_os_str().encode_wide() {
        write!(&mut out, "{unit:04x}").expect("writing to String cannot fail");
    }
    out
}

#[cfg(not(any(unix, windows)))]
fn path_identity(path: &Path) -> String {
    path.to_string_lossy()
        .as_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn cache_namespace(path: &Path) -> String {
    format!("{}\0", path_identity(path))
}

fn cache_id(namespace: &str, session: &str) -> String {
    format!("{namespace}{session}")
}

/// Why the census could not list a database's conversations, as the source issue kind the
/// publication guard reads: only a defect no retry heals lets the other stores publish past it.
#[derive(Clone, Debug)]
struct Unenumerable {
    kind: &'static str,
    reason: String,
}

impl Unenumerable {
    /// Locks, torn snapshots and I/O errors: the fail-closed retry may clear them.
    fn transient() -> Self {
        Self {
            kind: "source-unreadable",
            reason: "database could not be opened or enumerated".to_string(),
        }
    }

    fn foreign(error: &rusqlite::Error) -> Self {
        Self {
            kind: "unsupported-file-type",
            reason: format!("not a crush database: {error}"),
        }
    }

    /// A stat failure on a registered path or a directory above it; denial is durable.
    fn uninspectable(path: &Path, error: &std::io::Error) -> Self {
        Self {
            kind: if error.kind() == std::io::ErrorKind::PermissionDenied {
                "permission-denied"
            } else {
                "source-unreadable"
            },
            reason: format!("cannot inspect {}: {error}", path.display()),
        }
    }

    fn durable(&self) -> bool {
        matches!(
            self.kind,
            "permission-denied" | "unsupported-file-type" | "corrupt-database" | "needs-recovery"
        )
    }
}

fn open_failure(path: &Path, error: &rusqlite::Error) -> Unenumerable {
    match crate::ingest::sqlite_open_defect(path, error) {
        Some("unsupported-file-type") => Unenumerable::foreign(error),
        Some(kind) => Unenumerable {
            kind,
            reason: error.to_string(),
        },
        // A journal whose writer cannot be probed is named, with how its agent recovers it.
        None if error.to_string().contains(crate::ingest::JOURNAL_PRESENT) => Unenumerable {
            kind: "source-unreadable",
            reason: error.to_string(),
        },
        None => Unenumerable::transient(),
    }
}

fn open_ro(path: &Path) -> Result<crate::ingest::ReadOnlyConnection, Unenumerable> {
    crate::ingest::open_sqlite_ro(path).map_err(|error| {
        crate::intake::diagnostic(format_args!(
            "  ! crush: cannot open {}: {}",
            crate::ingest::terminal_safe(path.display()),
            crate::ingest::terminal_safe(&error)
        ));
        open_failure(path, &error)
    })
}

/// A part wrapper's text, if it is a text part.
fn part_text(part: &serde_json::Value) -> Option<&str> {
    if part.get("type").and_then(|t| t.as_str()) == Some("text") {
        part.get("data")
            .and_then(|d| d.get("text"))
            .and_then(|t| t.as_str())
    } else {
        None
    }
}

/// A digest of one conversation's stored message rows, None when they cannot all be read.
fn conversation_digest(conn: &Connection, session: &str) -> Option<String> {
    let mut stmt = conn
        .prepare("SELECT id, role, parts, model, created_at FROM messages WHERE session_id = ?1 ORDER BY id")
        .ok()?;
    let mut rows = stmt.query([session]).ok()?;
    let mut hash = 0xcbf29ce484222325_u64;
    while let Some(row) = rows.next().ok()? {
        for column in 0..5 {
            let value: rusqlite::types::Value = row.get(column).ok()?;
            let text = format!("{value:?}\0");
            for byte in text.bytes() {
                hash ^= u64::from(byte);
                hash = hash.wrapping_mul(0x100000001b3);
            }
        }
    }
    Some(format!("rows:{hash:016x}"))
}

/// Parse one session's messages into (user turns + replies, events). Tool results pair to
/// their tool_call by id (they may share a message or arrive in a later one).
fn parse_session(
    conn: &Connection,
    db_path: &Path,
    project: &str,
    session: &str,
    token: &str,
) -> (Vec<Message>, Vec<Event>, bool) {
    // seen = message rows in this conversation
    let tally = crate::intake::keyed_token("crush", db_path, session, token.to_string());
    let mut stmt = match conn.prepare(MESSAGES_QUERY) {
        Ok(s) => s,
        Err(e) => {
            tally.error(&format!("message query: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };
    let rows = stmt.query_map([session], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, Option<String>>(2)?,
            r.get::<_, i64>(3)?,
        ))
    });
    let rows = match rows {
        Ok(r) => r,
        Err(e) => {
            tally.error(&format!("message rows: {e}"));
            return (Vec::new(), Vec::new(), false);
        }
    };

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut pending: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    let mut healthy = true;
    let parent = match conn.query_row(PARENT_QUERY, [session], |r| r.get::<_, String>(0)) {
        Ok(parent) => parent,
        Err(error) => {
            tally.error(&format!("session parent: {error}"));
            healthy = false;
            String::new()
        }
    };
    let mut turn = 0u32;
    // parts are in order: a tool_call sets the flag the next text consumes
    let mut worked = false;
    for (row_ordinal, row) in rows.enumerate() {
        tally.seen();
        let row = match row {
            Ok(row) => row,
            Err(e) => {
                tally.error(&format!("message row: {e}"));
                healthy = false;
                continue;
            }
        };
        let (role, parts_json, model, created_at) = row;
        let parts: Vec<serde_json::Value> = match serde_json::from_str(&parts_json) {
            Ok(parts) => parts,
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(&parts_json, 80)));
                healthy = false;
                continue;
            }
        };

        if role == "user" {
            let mut text = String::new();
            for p in &parts {
                if let Some(t) = part_text(p) {
                    if !text.is_empty() {
                        text.push('\n');
                    }
                    text.push_str(t);
                }
            }
            if text.trim().is_empty() {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
            if is_wrapper(&text) {
                tally.skip(crate::intake::Skip::Wrapper);
                continue;
            }
            tally.row();
            worked = false;
            out.push(crate::model::RawMessage {
                agent: "crush",
                project: project.to_string(),
                session: session.to_string(),
                ts: created_at,
                turn,
                text,
                model: String::new(),
                reply: String::new(),
                reply_chars: 0,
                side: !parent.is_empty(),
                parent: parent.clone(),
                reply_shape: Default::default(),
            });
            turn += 1;
        } else if role == "assistant" {
            let mut reply = String::new();
            // the row's text parts make one reply block: its join is what came before the
            // first of them, calls after that join it to the next row
            let mut worked_before = None;
            for (part_ordinal, p) in parts.iter().enumerate() {
                match p.get("type").and_then(|t| t.as_str()) {
                    Some("text") => {
                        if let Some(t) = p
                            .get("data")
                            .and_then(|d| d.get("text"))
                            .and_then(|t| t.as_str())
                        {
                            if worked_before.is_none() {
                                worked_before = Some(std::mem::take(&mut worked));
                            }
                            if !reply.is_empty() {
                                reply.push('\n');
                            }
                            reply.push_str(t);
                        }
                    }
                    Some("tool_call") => {
                        let d = p.get("data").cloned().unwrap_or(serde_json::Value::Null);
                        let name = d
                            .get("name")
                            .and_then(|n| n.as_str())
                            .unwrap_or("?")
                            .to_string();
                        worked |= !crate::ingest::is_bookkeeping_tool(&name);
                        let call_id = d
                            .get("id")
                            .and_then(|i| i.as_str())
                            .filter(|id| !id.trim().is_empty())
                            .map(str::to_string)
                            .unwrap_or_else(|| {
                                format!("crush:{session}:{row_ordinal}:{part_ordinal}")
                            });
                        // input is a JSON-encoded string; summarize the parsed object
                        let (input, input_chars) = d
                            .get("input")
                            .and_then(|i| i.as_str())
                            .map(|s| {
                                serde_json::from_str::<serde_json::Value>(s)
                                    .map(|v| summarize_tool_input_with_chars(&v))
                                    .unwrap_or_else(|_| cap_str_with_chars(s, EVENT_CAP))
                            })
                            .unwrap_or_default();
                        if d.get("id")
                            .and_then(|i| i.as_str())
                            .is_some_and(|id| !id.is_empty())
                        {
                            pending.insert(call_id.clone(), events.len());
                        }
                        tally.event();
                        events.push(Event {
                            turn: None,
                            agent: "crush",
                            session: session.to_string(),
                            ts: created_at,
                            kind: "tool",
                            name,
                            input,
                            output: String::new(),
                            input_chars,
                            output_chars: 0,
                            output_bytes: 0,
                            ok: None,
                            call_id,
                            child_session: String::new(),
                            meta: String::new(),
                            state: String::new(),
                        });
                    }
                    Some("tool_result") => {
                        let d = p.get("data").cloned().unwrap_or(serde_json::Value::Null);
                        let id = d.get("tool_call_id").and_then(|i| i.as_str()).unwrap_or("");
                        if let Some(&i) = pending.get(id) {
                            let content = d.get("content").and_then(|c| c.as_str()).unwrap_or("");
                            (
                                events[i].output,
                                events[i].output_chars,
                                events[i].output_bytes,
                            ) = cap_event_output(content);
                            events[i].ok = d.get("is_error").and_then(|e| e.as_bool()).map(|e| !e);
                            pending.remove(id);
                        }
                    }
                    _ => {} // reasoning / finish / image / binary: not the reply text
                }
            }
            if !reply.trim().is_empty() {
                if let Some(last) = out.last_mut() {
                    crate::ingest::append_reply(last, &reply);
                    crate::ingest::ReplyOrder::joined_known(
                        &mut last.reply_shape,
                        worked_before.unwrap_or(false),
                    );

                    if last.model.is_empty() {
                        if let Some(m) = &model {
                            if !m.is_empty() {
                                last.model = m.clone();
                            }
                        }
                    }
                }
            }
            tally.agent_row();
        } else {
            tally.skip(crate::intake::Skip::NonHuman);
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

fn stable_generation_read<T>(
    path: &Path,
    opened_generation: Option<&str>,
    read: impl FnOnce(&str) -> Option<T>,
) -> Option<T> {
    let before = crate::ingest::sqlite_generation_token(path).ok()?;
    if opened_generation.is_some_and(|generation| generation != before) {
        return None;
    }
    let value = read(&before)?;
    let after = crate::ingest::sqlite_generation_token(path).ok()?;
    (before == after).then_some(value)
}

const TOKENS_QUERY: &str = "SELECT id, updated_at FROM sessions";
const PARENT_QUERY: &str = "SELECT COALESCE(parent_session_id,'') FROM sessions WHERE id = ?1";
const MESSAGES_QUERY: &str = "SELECT role, parts, model, created_at FROM messages \
     WHERE session_id = ? ORDER BY created_at, id";

/// A census failure no retry heals: a schema crush never wrote, or pages SQLite reads as corrupt.
fn schema_rejection(error: &rusqlite::Error) -> Option<Unenumerable> {
    if crate::ingest::sqlite_corrupt(error)
        || crate::ingest::sqlite_code(error) == Some(rusqlite::ErrorCode::NotADatabase)
    {
        return Some(Unenumerable {
            kind: "corrupt-database",
            reason: format!("crush database is corrupt: {error}"),
        });
    }
    (crate::ingest::sqlite_code(error) == Some(rusqlite::ErrorCode::Unknown))
        .then(|| Unenumerable::foreign(error))
}

/// Every session's exact generation-qualified staleness token. SQLite rejecting the census or a
/// parse statement means a schema crush never wrote; every other failure may clear on retry.
fn session_tokens(
    conn: &crate::ingest::ReadOnlyConnection,
    path: &Path,
    project: &str,
) -> Result<Vec<(String, String)>, Unenumerable> {
    let project_token = crate::ingest::registry::fnv_token(project.as_bytes());
    let mut rejected = None;
    let tokens = stable_generation_read(path, Some(conn.source_generation()), |generation| {
        let mut stmt = match conn.prepare(TOKENS_QUERY) {
            Ok(stmt) => stmt,
            Err(error) => {
                rejected = schema_rejection(&error);
                return None;
            }
        };
        for query in [PARENT_QUERY, MESSAGES_QUERY] {
            if let Err(error) = conn.prepare(query) {
                rejected = schema_rejection(&error);
                return None;
            }
        }
        let rows = match stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))) {
            Ok(rows) => rows,
            Err(error) => {
                rejected = schema_rejection(&error);
                return None;
            }
        };
        let mut out = Vec::new();
        for r in rows {
            let r = match r {
                Ok(r) => r,
                Err(error) => {
                    rejected = schema_rejection(&error);
                    return None;
                }
            };
            // updated_at present -> "u:<ms>" (see registry::token_fingerprint).
            let token = format!(
                "{}:{generation}:{project_token}",
                token_fingerprint(&serde_json::json!({ "updatedAt": r.1 }))
            );
            out.push((r.0, token));
        }
        out.sort();
        Some(out)
    });
    tokens.ok_or_else(|| rejected.unwrap_or_else(Unenumerable::transient))
}

struct Opened {
    databases: Vec<(Database, crate::ingest::ReadOnlyConnection)>,
    tokens: Vec<(String, String, String)>,
    locations: HashMap<String, usize>,
    unavailable: Vec<(String, PathBuf, Unenumerable)>,
}

fn open_with_tokens(discovery: &Discovery) -> Opened {
    let mut opened = Vec::with_capacity(discovery.databases.len());
    let mut tokens = Vec::new();
    let mut locations = HashMap::new();
    let mut unavailable = Vec::new();
    for database in &discovery.databases {
        let namespace = cache_namespace(&database.path);
        let connection = match open_ro(&database.path) {
            Ok(connection) => connection,
            Err(failure) => {
                unavailable.push((namespace, database.path.clone(), failure));
                continue;
            }
        };
        let sessions = match session_tokens(&connection, &database.path, &database.project) {
            Ok(sessions) => sessions,
            Err(failure) => {
                crate::intake::diagnostic(format_args!(
                    "  ! crush: cannot read sessions from {}",
                    crate::ingest::terminal_safe(database.path.display())
                ));
                unavailable.push((namespace, database.path.clone(), failure));
                continue;
            }
        };
        let index = opened.len();
        for (session, token) in sessions {
            let id = cache_id(&namespace, &session);
            locations.insert(id.clone(), index);
            tokens.push((id, session, token));
        }
        opened.push((database.clone(), connection));
    }
    for (path, failure) in &discovery.unavailable {
        unavailable.push((cache_namespace(path), path.clone(), failure.clone()));
    }
    tokens.sort();
    unavailable.sort_by(|left, right| left.0.cmp(&right.0));
    Opened {
        databases: opened,
        tokens,
        locations,
        unavailable,
    }
}

fn collect_discovered(
    cache: &mut crate::intake_batch::IntakeBatch,
    discovery: std::io::Result<Discovery>,
) -> (Vec<Message>, Vec<Event>) {
    let discovery = match discovery {
        Ok(discovery) => Some(discovery),
        Err(error) => {
            crate::intake::diagnostic(format_args!(
                "  ! crush: discovery failed: {}",
                crate::ingest::terminal_safe(&error)
            ));
            None
        }
    };
    let opened = discovery.as_ref().map(open_with_tokens);
    if let Some(discovery) = discovery.as_ref() {
        for (path, reason) in &discovery.issues {
            cache.record_source_read_issue("crush", path, "source-read-failed", reason.clone());
        }
    }
    if let Some(opened) = opened.as_ref() {
        for (_, path, failure) in &opened.unavailable {
            let issue = crate::intake_batch::SourceReadIssue::new(
                "crush",
                path,
                "source-read-failed",
                failure.reason.clone(),
            );
            cache.record_read_issue(if failure.durable() {
                issue
            } else {
                issue.retryable()
            });
        }
    }
    let mut token_by_cache = HashMap::new();
    if let Some(opened) = opened.as_ref() {
        token_by_cache.extend(
            opened
                .tokens
                .iter()
                .map(|(cache_id, _, token)| (cache_id.clone(), token.clone())),
        );
    }
    let all_failed = opened
        .as_ref()
        .is_some_and(|opened| opened.databases.is_empty() && !opened.unavailable.is_empty());
    let incomplete_discovery = discovery
        .as_ref()
        .is_some_and(|discovery| discovery.incomplete);
    let no_healthy_database = opened
        .as_ref()
        .map(|opened| opened.databases.is_empty())
        .unwrap_or(true);
    // Every database failed with a defect no retry heals and the published generation held nothing
    // there: an empty listing that keeps every cached conversation, as for an incomplete discovery,
    // instead of failing closed and taking every other agent's retry lane down with it.
    let durably_blind = all_failed
        && !incomplete_discovery
        && opened.as_ref().is_some_and(|opened| {
            opened
                .unavailable
                .iter()
                .all(|(_, _path, failure)| failure.durable())
        });
    let tokens = if durably_blind {
        Some(Vec::new())
    } else {
        opened
            .as_ref()
            .filter(|_| !all_failed && !(incomplete_discovery && no_healthy_database))
            .map(|opened| opened.tokens.clone())
    };
    let unavailable = opened
        .as_ref()
        .filter(|_| !all_failed)
        .map(|opened| {
            opened
                .unavailable
                .iter()
                .map(|(namespace, _, _)| namespace.clone())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let preserve_unlisted = incomplete_discovery || durably_blind;
    let has_readable_source = opened
        .as_ref()
        .is_some_and(|opened| !opened.databases.is_empty());
    let parse_issues = std::cell::RefCell::new(Vec::new());
    // A failed conversation is named by its own rows, never by its busy database's generation,
    // so one stuck chat in a database others keep writing still fails the same way twice.
    let failed_digests = std::cell::RefCell::new(Vec::new());
    let pass = cache.collect_token_cached_keyed_partial(
        "crush",
        tokens,
        &unavailable,
        preserve_unlisted,
        has_readable_source,
        |id, session| {
            use crate::intake_batch::ReadOutcome;
            let Some(opened) = opened.as_ref() else {
                return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
            };
            let Some(&index) = opened.locations.get(id) else {
                return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
            };
            let (database, connection) = &opened.databases[index];
            let parsed = crate::ingest::parse_guard::isolate("crush", &database.path, || {
                parse_session(
                    connection,
                    &database.path,
                    &database.project,
                    session,
                    token_by_cache.get(id).map(String::as_str).unwrap_or(""),
                )
            });
            match parsed {
                Ok((messages, events, true)) => {
                    if messages.is_empty() && events.is_empty() {
                        parse_issues.borrow_mut().push((
                            database.path.clone(),
                            session.to_owned(),
                            None,
                        ));
                    }
                    (messages, events, ReadOutcome::Complete)
                }
                // The conversation failed whole: none of its rows are vouched for, and the record
                // below confines the failure to it, so its siblings stay complete.
                Ok((_, _, false)) => {
                    parse_issues.borrow_mut().push((
                        database.path.clone(),
                        session.to_owned(),
                        None,
                    ));
                    if let Some(digest) = conversation_digest(connection, session) {
                        failed_digests.borrow_mut().push((
                            id.to_owned(),
                            session.to_owned(),
                            digest,
                        ));
                    }
                    (Vec::new(), Vec::new(), ReadOutcome::Invalid)
                }
                Err(panic) => {
                    parse_issues.borrow_mut().push((
                        database.path.clone(),
                        session.to_owned(),
                        Some(panic.reason),
                    ));
                    (Vec::new(), Vec::new(), ReadOutcome::Skipped)
                }
            }
        },
    );
    // The record names the conversation whose parse failed: a reader must not charge a
    // database-wide record to the siblings that merely changed since.
    for (path, session, panicked) in parse_issues.into_inner() {
        let reason = panicked.unwrap_or_else(|| {
            "the database session query did not produce a complete row".to_owned()
        });
        cache.record_read_issue(
            crate::intake_batch::SourceReadIssue::new("crush", path, "source-read-failed", reason)
                .for_session(session),
        );
    }
    drop(failed_digests);
    (pass.messages, pass.events)
}

/// Open the crush store read-only and collect the user's messages + tool events, reparsing
/// only conversations whose token moved (Fingerprint::Token). A store that won't open serves
/// the cached conversations, never empty.
pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    collect_discovered(cache, discover())
}

fn live_tokens(intake_keys: bool) -> crate::ingest::registry::TokenAvailability {
    live_tokens_from(discover(), intake_keys)
}

/// Each database the census opens lists its conversations; one it cannot is named by an issue
/// scoped to it, as is a registry whose databases stayed undiscovered. Only when nothing opened
/// is the whole census unreadable.
fn live_tokens_from(
    discovery: std::io::Result<Discovery>,
    intake_keys: bool,
) -> crate::ingest::registry::TokenAvailability {
    use crate::ingest::registry::{TokenAvailability, TokenReadIssue};

    let discovery = match discovery {
        Ok(discovery) => discovery,
        Err(error) => {
            return TokenAvailability::Unreadable(vec![TokenReadIssue::new(
                data_roots()
                    .into_iter()
                    .next()
                    .unwrap_or_else(crate::ingest::home),
                format!("source discovery failed: {error}"),
            )])
        }
    };
    let Opened {
        databases,
        tokens: live,
        locations,
        unavailable,
    } = open_with_tokens(&discovery);
    let mut issues: Vec<_> = unavailable
        .iter()
        .map(|(_, path, failure)| {
            TokenReadIssue::with_kind(path, failure.kind, failure.reason.clone())
        })
        .collect();
    issues.extend(
        discovery
            .issues
            .iter()
            .map(|(path, reason)| TokenReadIssue::new(path, reason)),
    );
    if discovery.incomplete && issues.is_empty() {
        issues.push(TokenReadIssue::new(
            data_roots()
                .into_iter()
                .next()
                .unwrap_or_else(crate::ingest::home),
            "source discovery was incomplete",
        ));
    }
    let mut tokens = Vec::with_capacity(live.len());
    for (cache_id, session, token) in live {
        if !intake_keys {
            tokens.push((cache_id, token));
            continue;
        }
        let Some(index) = locations.get(&cache_id).copied() else {
            return TokenAvailability::Unreadable(vec![TokenReadIssue::new(
                crate::ingest::home(),
                "conversation token has no source database",
            )]);
        };
        tokens.push((
            crate::intake::token_id(&databases[index].0.path, &session),
            token,
        ));
    }
    TokenAvailability::from_observation(tokens, issues)
}

/// Registry entry (see ingest::registry). Tokens include the live SQLite generation.
pub struct Crush;
impl crate::ingest::registry::Adapter for Crush {
    fn archive_roots(&self) -> Vec<PathBuf> {
        let mut roots = self.store_roots();
        roots.extend(data_roots().into_iter().map(|p| p.join("projects.json")));
        roots
    }
    fn archive_content(&self, path: &Path) -> bool {
        self.store_content(path) || path.file_name().is_some_and(|n| n == "projects.json")
    }
    fn name(&self) -> &'static str {
        "crush"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Token
    }
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<PathBuf> {
        discover()
            .map(|discovery| {
                // An absent registered database is a root the walk finds nothing under: two
                // such preflights confirm its removal, as for a Stat adapter's store root.
                let mut roots: Vec<PathBuf> = discovery
                    .databases
                    .into_iter()
                    .map(|database| database.path)
                    .chain(discovery.unavailable.into_iter().map(|(path, _)| path))
                    .chain(discovery.absent)
                    .collect();
                roots.sort();
                roots
            })
            .unwrap_or_default()
    }
    fn store_content(&self, path: &Path) -> bool {
        path.file_name().and_then(|name| name.to_str()) == Some("crush.db")
    }
    fn token_prefix(&self, database: &Path) -> Option<String> {
        self.store_content(database)
            .then(|| cache_namespace(database))
    }
    fn token_namespace<'a>(&self, cache_id: &'a str) -> &'a str {
        // cache_namespace is hex digits closed by the first NUL.
        cache_id.find('\0').map_or("", |end| &cache_id[..=end])
    }
    fn freshness_tokens(&self) -> crate::ingest::registry::TokenAvailability {
        live_tokens(false)
    }
    fn intake_tokens(&self) -> crate::ingest::registry::TokenAvailability {
        live_tokens(true)
    }
    fn runtime_issue_root(&self) -> PathBuf {
        data_roots()
            .into_iter()
            .next()
            .unwrap_or_else(crate::ingest::home)
    }
}
