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
    /// readable crush.db files under a root that projects.json never registers;
    /// prod warns at discovery time, tests read the field as the evidence
    #[cfg_attr(not(test), allow(dead_code))]
    unregistered: Vec<PathBuf>,
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
            eprintln!(
                "  ! crush: {} exists but is not referenced by projects.json; not ingested",
                crate::ingest::terminal_safe(path.display())
            );
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
            eprintln!(
                "  ! crush: cannot read {}: {}",
                crate::ingest::terminal_safe(registry.display()),
                crate::ingest::terminal_safe(&error)
            );
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
        unregistered,
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
        eprintln!(
            "  ! crush: cannot open {}: {}",
            crate::ingest::terminal_safe(path.display()),
            crate::ingest::terminal_safe(&error)
        );
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
                eprintln!(
                    "  ! crush: cannot read sessions from {}",
                    crate::ingest::terminal_safe(database.path.display())
                );
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
    cache: &mut crate::ingest_cache::IngestCache,
    discovery: std::io::Result<Discovery>,
) -> (Vec<Message>, Vec<Event>) {
    let discovery = match discovery {
        Ok(discovery) => Some(discovery),
        Err(error) => {
            eprintln!(
                "  ! crush: discovery failed: {}",
                crate::ingest::terminal_safe(&error)
            );
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
            let issue = crate::ingest_cache::SourceReadIssue::new(
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
            opened.unavailable.iter().all(|(_, path, failure)| {
                failure.durable()
                    && cache.published_material_under("crush", path)
                        == crate::ingest_cache::MaterialVerdict::Retained
            })
        });
    let tokens = if durably_blind {
        Some(Vec::new())
    } else {
        opened
            .as_ref()
            .filter(|_| !all_failed && !(incomplete_discovery && no_healthy_database))
            .map(|opened| opened.tokens.clone())
    };
    let mut unavailable = opened
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
    // A registered database a stat proves gone may be an unmounted volume or a renamed project:
    // while it may hold material, its cached chats are kept and the pass guarded until a second
    // preflight confirms the absence, as for a Stat adapter's store root.
    if let Some(discovery) = discovery.as_ref().filter(|_| !all_failed) {
        for path in &discovery.absent {
            if cache.repeated_absence_covers("crush", path)
                || !cache.token_scope_expected("crush", path)
            {
                continue;
            }
            crate::ingest::warn_source_skip(
                "crush",
                path,
                "registered database is absent; its conversations wait for a second observation",
            );
            cache.record_source_read_issue(
                "crush",
                path,
                "source-root-unavailable",
                "registered database disappeared; a second observation confirms the removal",
            );
            cache.mark_guarded_stale();
            unavailable.push(cache_namespace(path));
        }
    }
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
            use crate::ingest_cache::ReadOutcome;
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
                        failed_digests
                            .borrow_mut()
                            .push((id.to_owned(), session.to_owned(), digest));
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
            crate::ingest_cache::SourceReadIssue::new("crush", path, "source-read-failed", reason)
                .for_session(session),
        );
    }
    for (id, session, digest) in failed_digests.into_inner() {
        cache.set_token_failure_identity("crush", &id, &session, digest);
    }
    (pass.messages, pass.events)
}

/// Open the crush store read-only and collect the user's messages + tool events, reparsing
/// only conversations whose token moved (Fingerprint::Token). A store that won't open serves
/// the cached conversations, never empty.
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
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
    fn name(&self) -> &'static str {
        "crush"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Token
    }
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "agrep-crush-{label}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn write_db(path: &Path, text: &str) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let connection = Connection::open(path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE sessions(
                    id TEXT PRIMARY KEY,
                    parent_session_id TEXT,
                    title TEXT,
                    updated_at INTEGER,
                    created_at INTEGER
                 );
                 CREATE TABLE messages(
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    role TEXT,
                    parts TEXT,
                    model TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                 );
                 INSERT INTO sessions VALUES ('same-session', '', '', 1000, 1000);",
            )
            .unwrap();
        let parts = serde_json::json!([{"type": "text", "data": {"text": text}}]);
        connection
            .execute(
                "INSERT INTO messages VALUES ('message', 'same-session', 'user', ?1, '', 1000, 1000)",
                [parts.to_string()],
            )
            .unwrap();
    }

    fn census_failure(path: &Path) -> Unenumerable {
        let connection = match open_ro(path) {
            Ok(connection) => connection,
            Err(failure) => return failure,
        };
        session_tokens(&connection, path, "crush").unwrap_err()
    }

    #[test]
    fn census_names_a_schema_the_parser_cannot_read_as_unsupported() {
        let base = temp_dir("census-schema");
        let schemas = [
            ("foreign", "CREATE TABLE notes(id TEXT, body TEXT);"),
            (
                "no-column",
                "CREATE TABLE sessions(id TEXT PRIMARY KEY, updated_at INTEGER);",
            ),
            (
                "no-messages",
                "CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT, \
                 updated_at INTEGER);",
            ),
        ];
        for (label, schema) in schemas {
            let path = base.join(format!("{label}.db"));
            Connection::open(&path)
                .unwrap()
                .execute_batch(schema)
                .unwrap();
            let failure = census_failure(&path);
            assert_eq!(
                failure.kind, "unsupported-file-type",
                "{label}: {failure:?}"
            );
        }
        for (label, bytes) in [("empty", &b""[..]), ("not-sqlite", &b"plain text\n"[..])] {
            let path = base.join(format!("{label}.db"));
            fs::write(&path, bytes).unwrap();
            let failure = census_failure(&path);
            assert_eq!(
                failure.kind, "unsupported-file-type",
                "{label}: {failure:?}"
            );
        }
        let healthy = base.join("healthy.db");
        write_db(&healthy, "fine");
        let connection = open_ro(&healthy).unwrap_or_else(|failure| panic!("{failure:?}"));
        assert_eq!(
            session_tokens(&connection, &healthy, "crush")
                .unwrap()
                .len(),
            1
        );
        let _ = fs::remove_dir_all(&base);
    }

    fn cached_message(project: &str, text: &str) -> Message {
        crate::model::RawMessage {
            agent: "crush",
            project: project.to_string(),
            session: "same-session".to_string(),
            ts: 1000,
            turn: 0,
            text: text.to_string(),
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
            reply_shape: Default::default(),
        }
        .freeze()
    }

    #[test]
    fn cold_mixed_database_failure_publishes_healthy_database() {
        let base = temp_dir("cold-partial-db");
        let root = base.join("global");
        let healthy_project = base.join("healthy");
        let broken_project = base.join("broken");
        let healthy_data = healthy_project.join(".crush");
        let broken_data = broken_project.join(".crush");
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&broken_data).unwrap();
        write_db(&healthy_data.join("crush.db"), "healthy database survives");
        fs::write(broken_data.join("crush.db"), b"not a sqlite database").unwrap();
        fs::write(
            root.join("projects.json"),
            serde_json::json!({
                "projects": [
                    {"path": healthy_project, "data_dir": healthy_data},
                    {"path": broken_project, "data_dir": broken_data}
                ]
            })
            .to_string(),
        )
        .unwrap();

        let discovery = discover_at(vec![root], &base.join("home")).unwrap();
        assert_eq!(discovery.databases.len(), 2);
        let mut cache = crate::ingest_cache::IngestCache::cold();
        let (messages, _) = collect_discovered(&mut cache, Ok(discovery));
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].text.as_ref(), "healthy database survives");
        assert!(cache.output_complete());
        assert!(!cache.source_snapshot_safe());
        assert_eq!(cache.source_read_issues().len(), 1);
        assert_eq!(
            cache.source_read_issues()[0].path,
            broken_data.join("crush.db")
        );
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn a_conversations_failed_parse_is_recorded_against_that_conversation() {
        let base = temp_dir("failed-parse-session");
        let root = base.join("global");
        let database = root.join("crush.db");
        write_db(&database, "healthy sibling");
        let connection = Connection::open(&database).unwrap();
        connection
            .execute_batch(
                "INSERT INTO sessions VALUES ('bad-session', '', '', 2000, 2000);
                 INSERT INTO messages VALUES ('bad-good', 'bad-session', 'user', '[{\"type\":\"text\",\"data\":{\"text\":\"must not leak\"}}]', '', 2000, 2000);
                 INSERT INTO messages VALUES ('bad', 'bad-session', 'user', '{not json', '', 2001, 2001);",
            )
            .unwrap();
        drop(connection);
        let discovery = discover_at(vec![root], &base.join("home")).unwrap();
        // A first index: nothing published, so the failed conversation holds nothing back.
        let mut cache = crate::ingest_cache::IngestCache::cold();
        cache.set_published_material(std::collections::HashSet::new());
        cache.set_published_token_namespaces(Some(std::collections::HashSet::new()));
        let (messages, _) = collect_discovered(&mut cache, Ok(discovery));
        assert_eq!(messages.len(), 1);
        assert!(cache.output_complete());
        let issues = cache.source_read_issues();
        assert_eq!(issues.len(), 1, "{issues:?}");
        assert_eq!(issues[0].path, database);
        assert_eq!(issues[0].kind, "source-read-failed");
        assert_eq!(issues[0].session.as_deref(), Some("bad-session"));
        let _ = fs::remove_dir_all(base);
    }

    /// A registered data dir (or `~/.crush`) that cannot be searched hides its database from a
    /// stat: that database is unavailable, its conversations retained and its issue scoped to it,
    /// in the warm collect, the census and the store roots alike. Never absent, never pruned.
    #[cfg(unix)]
    #[test]
    fn uninspectable_data_dir_keeps_its_database_unavailable_not_absent() {
        use crate::ingest::registry::TokenAvailability;
        use std::os::unix::fs::PermissionsExt;

        unsafe extern "C" {
            fn geteuid() -> u32;
        }
        if unsafe { geteuid() } == 0 {
            return;
        }
        let base = temp_dir("denied-data-dir");
        let root = base.join("global");
        let home = base.join("home");
        let project = base.join("project");
        let project_data = project.join(".crush");
        let old_data = home.join(".crush");
        fs::create_dir_all(&root).unwrap();
        write_db(&root.join("crush.db"), "global question");
        write_db(&project_data.join("crush.db"), "project question");
        write_db(&old_data.join("crush.db"), "old home question");
        fs::write(
            root.join("projects.json"),
            serde_json::json!({"projects": [{"path": project, "data_dir": project_data}]})
                .to_string(),
        )
        .unwrap();
        let cache_path = base.join("cache.bin");
        let mut cold = crate::ingest_cache::IngestCache::cold();
        let (messages, _) = collect_discovered(&mut cold, discover_at(vec![root.clone()], &home));
        assert_eq!(messages.len(), 3);
        cold.save(&cache_path).unwrap();

        for denied in [&project_data, &old_data] {
            let database = denied.join("crush.db");
            fs::set_permissions(denied, fs::Permissions::from_mode(0o000)).unwrap();
            let discovery = discover_at(vec![root.clone()], &home).unwrap();
            fs::set_permissions(denied, fs::Permissions::from_mode(0o700)).unwrap();
            assert!(!discovery.incomplete);
            assert!(discovery.issues.is_empty());
            assert_eq!(discovery.databases.len(), 2, "{discovery:?}");
            assert!(!discovery.databases.iter().any(|db| db.path == database));
            assert_eq!(discovery.unavailable.len(), 1, "{discovery:?}");
            assert_eq!(discovery.unavailable[0].0, database);
            assert_eq!(discovery.unavailable[0].1.kind, "permission-denied");

            fs::set_permissions(denied, fs::Permissions::from_mode(0o000)).unwrap();
            let mut warm = crate::ingest_cache::IngestCache::load(&cache_path);
            let (messages, _) =
                collect_discovered(&mut warm, discover_at(vec![root.clone()], &home));
            let census = live_tokens_from(discover_at(vec![root.clone()], &home), false);
            fs::set_permissions(denied, fs::Permissions::from_mode(0o700)).unwrap();
            let texts: BTreeSet<&str> = messages.iter().map(|m| m.text.as_ref()).collect();
            assert_eq!(
                texts,
                BTreeSet::from(["global question", "project question", "old home question"]),
                "{}",
                denied.display()
            );
            assert!(warm.output_complete());
            let issues = warm.source_read_issues();
            assert_eq!(issues.len(), 1, "{issues:?}");
            assert_eq!(issues[0].path, database);
            match census {
                TokenAvailability::Partial { tokens, issues } => {
                    assert_eq!(tokens.len(), 2);
                    assert!(!tokens
                        .iter()
                        .any(|(id, _)| id.starts_with(&cache_namespace(&database))));
                    assert_eq!(issues.len(), 1, "{issues:?}");
                    assert_eq!(issues[0].path, database);
                    assert_eq!(issues[0].kind, "permission-denied");
                }
                other => panic!("{other:?}"),
            }
        }
        let _ = fs::remove_dir_all(base);
    }

    /// A registered database behind a parent that became a file or a name too long for the
    /// filesystem cannot exist: absent, never unavailable. Behind a symlink loop or a link
    /// pointing at a file the data may be intact: unavailable, every cached conversation kept.
    #[cfg(unix)]
    #[test]
    fn registered_paths_a_stat_proves_absent_are_absent_and_mispointed_links_unavailable() {
        use std::os::unix::fs::symlink;

        let base = temp_dir("absent-by-stat");
        let root = base.join("global");
        let home = base.join("home");
        fs::create_dir_all(&root).unwrap();
        write_db(&root.join("crush.db"), "global question");
        // Each project sits a level below the defect, so its own stat fails rather than a parent's.
        let gone = base.join("gone").join("project");
        fs::write(
            base.join("gone"),
            b"a file where the projects directory was",
        )
        .unwrap();
        let too_long = base.join("x".repeat(300)).join("project");
        let looped = base.join("loop-a").join("project");
        symlink(base.join("loop-b"), base.join("loop-a")).unwrap();
        symlink(base.join("loop-a"), base.join("loop-b")).unwrap();
        let mispointed = base.join("link-to-file").join("project");
        symlink(base.join("gone"), base.join("link-to-file")).unwrap();
        for (project, absent) in [
            (&gone, true),
            (&too_long, true),
            (&looped, false),
            (&mispointed, false),
        ] {
            let error = fs::symlink_metadata(project).unwrap_err();
            assert_eq!(
                stat_proves_absent(project, &error),
                absent,
                "{}: {error:?}",
                project.display()
            );
        }
        let projects: Vec<_> = [&gone, &too_long, &looped, &mispointed]
            .iter()
            .map(|project| serde_json::json!({"path": project, "data_dir": project.join(".crush")}))
            .collect();
        fs::write(
            root.join("projects.json"),
            serde_json::json!({"projects": projects}).to_string(),
        )
        .unwrap();
        let discovery = discover_at(vec![root.clone()], &home).unwrap();
        assert!(!discovery.incomplete);
        assert!(discovery.issues.is_empty());
        assert_eq!(discovery.databases.len(), 1);
        assert_eq!(discovery.databases[0].path, root.join("crush.db"));
        let unavailable: Vec<&Path> = discovery
            .unavailable
            .iter()
            .map(|(path, _)| path.as_path())
            .collect();
        assert_eq!(
            unavailable,
            vec![
                mispointed.join(".crush").join("crush.db").as_path(),
                looped.join(".crush").join("crush.db").as_path()
            ],
            "{discovery:?}"
        );
        // Registered databases a stat proves gone are listed for the absence protocol; the root
        // and legacy ~/.crush databases, which nothing registers, are not.
        assert_eq!(
            discovery.absent,
            vec![
                gone.join(".crush").join("crush.db"),
                too_long.join(".crush").join("crush.db")
            ],
            "{discovery:?}"
        );
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn unregistered_nested_database_is_named_but_not_ingested() {
        let base = temp_dir("unregistered-nested");
        let root = base.join("global");
        fs::create_dir_all(&root).unwrap();
        let registered = base.join("project").join(".crush");
        write_db(&registered.join("crush.db"), "registered store is ingested");
        write_db(
            &root.join("projdir").join("crush.db"),
            "nested store is not",
        );
        fs::write(
            root.join("projects.json"),
            serde_json::json!({
                "projects": [{"path": base.join("project"), "data_dir": registered}]
            })
            .to_string(),
        )
        .unwrap();

        let discovery = discover_at(vec![root.clone()], &base.join("home")).unwrap();
        assert_eq!(
            discovery.unregistered,
            vec![root.join("projdir").join("crush.db")]
        );
        // the warning is a report, not a read failure: nothing is marked incomplete
        assert!(!discovery.incomplete);
        assert!(discovery.issues.is_empty());
        let mut cache = crate::ingest_cache::IngestCache::cold();
        let (messages, _) = collect_discovered(&mut cache, Ok(discovery));
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].text.as_ref(), "registered store is ingested");
        assert!(cache.source_read_issues().is_empty());
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn registered_databases_are_never_reported_unregistered() {
        let base = temp_dir("registered-nested");
        let root = base.join("global");
        let data = root.join("projdir");
        fs::create_dir_all(&root).unwrap();
        write_db(&data.join("crush.db"), "registered nested store");
        fs::write(
            root.join("projects.json"),
            serde_json::json!({
                "projects": [{"path": base.join("project"), "data_dir": data}]
            })
            .to_string(),
        )
        .unwrap();

        let discovery = discover_at(vec![root], &base.join("home")).unwrap();
        assert_eq!(discovery.databases.len(), 1);
        assert!(discovery.unregistered.is_empty());
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn data_root_precedence_is_platform_explicit() {
        let home = Path::new("/Users/runner");
        let legacy = home.join(".local/share/crush");
        assert_eq!(
            data_roots_at(
                home,
                false,
                Some(OsString::from("/override")),
                Some(OsString::from("/xdg")),
                Some(OsString::from("C:/Local")),
                false,
            ),
            vec![PathBuf::from("/override"), legacy.clone()]
        );
        assert_eq!(
            data_roots_at(
                home,
                false,
                None,
                Some(OsString::from("/xdg")),
                Some(OsString::from("C:/Local")),
                true,
            ),
            vec![PathBuf::from("/xdg/crush"), legacy.clone()]
        );
        assert_eq!(
            data_roots_at(
                home,
                false,
                None,
                None,
                Some(OsString::from("C:/Local")),
                true,
            ),
            vec![PathBuf::from("C:/Local/crush"), legacy.clone()]
        );
        assert_eq!(
            data_roots_at(
                home,
                true,
                Some(OsString::from("/override")),
                None,
                None,
                false,
            ),
            vec![legacy]
        );
    }

    #[test]
    fn project_databases_with_equal_session_ids_cache_independently() {
        let base = temp_dir("multi-db");
        let root = base.join("global");
        let alpha = base.join("alpha");
        let beta = base.join("beta");
        let alpha_data = alpha.join(".crush");
        let beta_data = beta.join(".crush");
        fs::create_dir_all(&root).unwrap();
        write_db(&alpha_data.join("crush.db"), "alpha question");
        write_db(&beta_data.join("crush.db"), "beta question");
        let registry = serde_json::json!({
            "projects": [
                {"path": alpha, "data_dir": alpha_data, "last_accessed": "2026-07-18T12:00:00Z"},
                {"path": beta, "data_dir": beta_data, "last_accessed": "2026-07-17T12:00:00Z"},
                {"path": alpha, "data_dir": alpha_data, "last_accessed": "2026-07-16T12:00:00Z"}
            ]
        });
        fs::write(root.join("projects.json"), registry.to_string()).unwrap();

        let discovery = discover_at(vec![root.clone()], &base.join("home")).unwrap();
        assert_eq!(discovery.databases.len(), 2);
        let opened = open_with_tokens(&discovery);
        assert!(opened.unavailable.is_empty());
        assert_eq!(opened.tokens.len(), 2);
        assert_ne!(opened.tokens[0].0, opened.tokens[1].0);
        let mut cache = crate::ingest_cache::IngestCache::cold();
        let first = cache.collect_token_cached_keyed_partial(
            "crush",
            Some(opened.tokens.clone()),
            &[],
            false,
            true,
            |id, _| {
                let index = opened.locations[id];
                let project = &opened.databases[index].0.project;
                let text = format!("{project} question");
                (vec![cached_message(project, &text)], Vec::new())
            },
        );
        let texts: BTreeSet<&str> = first
            .messages
            .iter()
            .map(|message| message.text.as_ref())
            .collect();
        assert_eq!(texts, BTreeSet::from(["alpha question", "beta question"]));
        let projects: BTreeSet<&str> = first
            .messages
            .iter()
            .map(|message| message.project.as_ref())
            .collect();
        assert_eq!(projects, BTreeSet::from(["alpha", "beta"]));
        drop(opened);

        let cache_path = base.join("cache.bin");
        cache.save(&cache_path).unwrap();
        let mut warm = crate::ingest_cache::IngestCache::load(&cache_path);
        fs::write(alpha_data.join("crush.db"), b"not a sqlite database").unwrap();
        let beta_connection = Connection::open(beta_data.join("crush.db")).unwrap();
        let parts = serde_json::json!([{"type": "text", "data": {"text": "beta updated"}}]);
        beta_connection
            .execute(
                "UPDATE messages SET parts = ?1 WHERE id = 'message'",
                [parts.to_string()],
            )
            .unwrap();
        beta_connection
            .execute(
                "UPDATE sessions SET updated_at = 2000 WHERE id = 'same-session'",
                [],
            )
            .unwrap();
        drop(beta_connection);
        let partial = discover_at(vec![root.clone()], &base.join("home")).unwrap();
        let opened = open_with_tokens(&partial);
        assert_eq!(opened.databases.len(), 1);
        assert_eq!(opened.unavailable.len(), 1);
        assert_eq!(opened.unavailable[0].2.kind, "unsupported-file-type");
        let unavailable: Vec<String> = opened
            .unavailable
            .iter()
            .map(|(namespace, _, _)| namespace.clone())
            .collect();
        let second = warm.collect_token_cached_keyed_partial(
            "crush",
            Some(opened.tokens.clone()),
            &unavailable,
            false,
            true,
            |id, _| {
                let index = opened.locations[id];
                let project = &opened.databases[index].0.project;
                (vec![cached_message(project, "beta updated")], Vec::new())
            },
        );
        let texts: BTreeSet<&str> = second
            .messages
            .iter()
            .map(|message| message.text.as_ref())
            .collect();
        assert_eq!(texts, BTreeSet::from(["alpha question", "beta updated"]));
        assert!(warm.source_snapshot_safe());
        warm.save(&cache_path).unwrap();
        drop(opened);

        fs::remove_file(alpha_data.join("crush.db")).unwrap();
        fs::remove_file(beta_data.join("crush.db")).unwrap();
        let mut reloaded = crate::ingest_cache::IngestCache::load(&cache_path);
        let cached = reloaded.collect_token_cached(
            "crush",
            Some(Vec::new()),
            |_| -> (Vec<Message>, Vec<Event>) { unreachable!() },
        );
        let texts: BTreeSet<&str> = cached
            .messages
            .iter()
            .map(|message| message.text.as_ref())
            .collect();
        assert_eq!(texts, BTreeSet::from(["alpha question", "beta updated"]));

        let _ = fs::remove_dir_all(base);
    }

    /// One database the census cannot open leaves every sibling listed under an issue naming
    /// it alone; a registry it cannot read names that registry; nothing opened is unreadable.
    #[test]
    fn census_lists_each_opened_database_beside_the_issue_scoped_to_an_unopened_one() {
        use crate::ingest::registry::TokenAvailability;

        let base = temp_dir("scoped-census");
        let root = base.join("global");
        let home = base.join("home");
        let global = root.join("crush.db");
        let alpha = base.join("alpha");
        let beta = base.join("beta");
        let alpha_db = alpha.join(".crush").join("crush.db");
        let beta_db = beta.join(".crush").join("crush.db");
        fs::create_dir_all(&root).unwrap();
        write_db(&global, "global question");
        write_db(&alpha_db, "alpha question");
        write_db(&beta_db, "beta question");
        let registry = serde_json::json!({
            "projects": [
                {"path": alpha, "data_dir": alpha_db.parent().unwrap(), "last_accessed": "2026-07-18T12:00:00Z"},
                {"path": beta, "data_dir": beta_db.parent().unwrap(), "last_accessed": "2026-07-17T12:00:00Z"}
            ]
        });
        fs::write(root.join("projects.json"), registry.to_string()).unwrap();
        let census = |intake_keys: bool| {
            live_tokens_from(discover_at(vec![root.clone()], &home), intake_keys)
        };
        let ids = |tokens: &[(String, String)]| -> BTreeSet<String> {
            tokens.iter().map(|(id, _)| id.clone()).collect()
        };
        let expected = |databases: &[&Path], intake_keys: bool| -> BTreeSet<String> {
            databases
                .iter()
                .map(|db| {
                    if intake_keys {
                        crate::intake::token_id(db, "same-session")
                    } else {
                        cache_id(&cache_namespace(db), "same-session")
                    }
                })
                .collect()
        };
        match census(false) {
            TokenAvailability::Data(tokens) => {
                assert_eq!(
                    ids(&tokens),
                    expected(&[&global, &alpha_db, &beta_db], false)
                )
            }
            other => panic!("{other:?}"),
        }

        fs::write(&alpha_db, b"not a sqlite database").unwrap();
        for intake_keys in [false, true] {
            match census(intake_keys) {
                TokenAvailability::Partial { tokens, issues } => {
                    assert_eq!(ids(&tokens), expected(&[&global, &beta_db], intake_keys));
                    assert_eq!(issues.len(), 1, "{issues:?}");
                    assert_eq!(issues[0].path, alpha_db);
                    assert_eq!(issues[0].kind, "unsupported-file-type");
                }
                other => panic!("intake_keys={intake_keys}: {other:?}"),
            }
        }

        fs::write(root.join("projects.json"), b"{broken").unwrap();
        match census(false) {
            TokenAvailability::Partial { tokens, issues } => {
                assert_eq!(ids(&tokens), expected(&[&global], false));
                assert_eq!(issues.len(), 1, "{issues:?}");
                assert_eq!(issues[0].path, root.join("projects.json"));
                assert_eq!(issues[0].kind, "source-unreadable");
            }
            other => panic!("{other:?}"),
        }

        fs::write(&global, b"not a sqlite database").unwrap();
        match census(false) {
            TokenAvailability::Unreadable(issues) => {
                let paths: BTreeSet<PathBuf> =
                    issues.iter().map(|issue| issue.path.clone()).collect();
                assert_eq!(
                    paths,
                    BTreeSet::from([global.clone(), root.join("projects.json")])
                );
            }
            other => panic!("{other:?}"),
        }

        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn token_snapshot_rejects_a_database_generation_that_moves_mid_read() {
        let root = temp_dir("token-generation");
        let path = root.join("crush.db");
        let connection = Connection::open(&path).unwrap();
        connection
            .execute(
                "CREATE TABLE sessions(id TEXT PRIMARY KEY, updated_at INTEGER)",
                [],
            )
            .unwrap();

        let value = stable_generation_read(&path, None, |_| {
            connection
                .execute("INSERT INTO sessions VALUES ('session', 1)", [])
                .ok()?;
            Some(())
        });
        assert!(value.is_none());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn map_registry_resolves_relative_project_data_dir() {
        let base = temp_dir("map-registry");
        let root = base.join("global");
        let project = base.join("mapped-project");
        fs::create_dir_all(&root).unwrap();
        write_db(&project.join(".crush/crush.db"), "mapped question");
        let registry = serde_json::json!({
            "projects": {
                project.to_string_lossy().to_string(): {
                    "data_dir": ".crush",
                    "last_accessed": "2026-07-18T12:00:00Z"
                }
            }
        });
        fs::write(root.join("projects.json"), registry.to_string()).unwrap();

        let discovery = discover_at(vec![root], &base.join("home")).unwrap();
        assert_eq!(discovery.databases.len(), 1);
        assert_eq!(discovery.databases[0].project, "mapped-project");

        let _ = fs::remove_dir_all(base);
    }

    #[cfg(unix)]
    #[test]
    fn projects_registry_rejects_special_files() {
        let base = temp_dir("projects-special-file");
        let root = base.join("global");
        fs::create_dir_all(&root).unwrap();
        let path = root.join("projects.json");
        assert!(std::process::Command::new("mkfifo")
            .arg(&path)
            .status()
            .unwrap()
            .success());
        let error = read_projects(&path, &root, &mut Found::default()).unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn bad_registries_do_not_hide_standalone_databases() {
        let base = temp_dir("bad-registries");
        let corrupt_root = base.join("corrupt-root");
        let oversized_root = base.join("oversized-root");
        let home = base.join("home");
        fs::create_dir_all(&corrupt_root).unwrap();
        fs::create_dir_all(&oversized_root).unwrap();
        write_db(&corrupt_root.join("crush.db"), "corrupt registry database");
        write_db(
            &oversized_root.join("crush.db"),
            "oversized registry database",
        );
        write_db(&home.join(".crush/crush.db"), "legacy database");
        fs::write(corrupt_root.join("projects.json"), b"{broken").unwrap();
        fs::File::create(oversized_root.join("projects.json"))
            .unwrap()
            .set_len(PROJECTS_JSON_MAX_BYTES + 1)
            .unwrap();

        let discovery = discover_at(vec![corrupt_root, oversized_root], &home).unwrap();
        assert_eq!(discovery.databases.len(), 3);
        let mut cache = crate::ingest_cache::IngestCache::cold();
        let pass = collect_discovered(&mut cache, Ok(discovery));
        let texts: BTreeSet<&str> = pass.0.iter().map(|message| message.text.as_ref()).collect();
        assert_eq!(
            texts,
            BTreeSet::from([
                "corrupt registry database",
                "legacy database",
                "oversized registry database",
            ])
        );
        assert!(cache.output_complete());
        assert!(!cache.source_snapshot_safe());

        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn corrupt_registry_without_a_healthy_database_is_incomplete() {
        let base = temp_dir("bad-registry-only");
        let root = base.join("global");
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("projects.json"), b"{broken").unwrap();

        let mut cache = crate::ingest_cache::IngestCache::cold();
        let pass = collect_discovered(&mut cache, discover_at(vec![root], &base.join("home")));
        assert!(pass.0.is_empty());
        assert!(!cache.output_complete());
        assert!(!cache.source_snapshot_safe());

        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn durably_foreign_store_fails_closed_only_where_published_material_is_at_stake() {
        use crate::ingest_cache::IngestCache;
        use std::collections::HashSet;

        let base = temp_dir("foreign-only");
        let root = base.join("global");
        let home = base.join("home");
        let db = root.join("crush.db");
        write_db(&db, "cached before the store turned foreign");
        let cache_path = base.join("cache.bin");
        let mut initial = IngestCache::cold();
        assert_eq!(
            collect_discovered(&mut initial, discover_at(vec![root.clone()], &home))
                .0
                .len(),
            1
        );
        initial.save(&cache_path).unwrap();
        fs::write(&db, b"not a sqlite database").unwrap();

        let mut first_index = IngestCache::cold();
        first_index.set_published_material(HashSet::new());
        first_index.set_published_token_namespaces(Some(HashSet::new()));
        let pass = collect_discovered(&mut first_index, discover_at(vec![root.clone()], &home));
        assert!(pass.0.is_empty());
        assert!(first_index.output_complete());
        assert!(!first_index.source_snapshot_safe());

        let recorded = ("crush".to_string(), cache_namespace(&db));
        let at_stake_inventories = [
            (None, Some(HashSet::new())),
            (Some(HashSet::from([db.clone()])), Some(HashSet::new())),
            (Some(HashSet::new()), None),
            (Some(HashSet::new()), Some(HashSet::from([recorded]))),
        ];
        for (published, namespaces) in at_stake_inventories {
            let mut at_stake = IngestCache::cold();
            if let Some(published) = published {
                at_stake.set_published_material(published);
            }
            at_stake.set_published_token_namespaces(namespaces);
            collect_discovered(&mut at_stake, discover_at(vec![root.clone()], &home));
            assert!(!at_stake.output_complete());
        }

        // A warm base still holding the database's conversations serves them and deletes none.
        for _pass in 0..2 {
            let mut warm = IngestCache::load(&cache_path);
            warm.set_published_material(HashSet::new());
            warm.set_published_token_namespaces(Some(HashSet::new()));
            let pass = collect_discovered(&mut warm, discover_at(vec![root.clone()], &home));
            assert_eq!(pass.0.len(), 1);
            assert!(warm.output_complete());
            assert!(!warm.source_snapshot_safe());
            warm.save(&cache_path).unwrap();
        }

        let _ = fs::remove_dir_all(base);
    }

    #[test]
    fn corrupt_registry_keeps_cached_projects_while_refreshing_global_database() {
        let base = temp_dir("registry-last-good");
        let root = base.join("global");
        let project = base.join("project");
        let project_data = project.join(".crush");
        fs::create_dir_all(&root).unwrap();
        write_db(&root.join("crush.db"), "global old");
        write_db(&project_data.join("crush.db"), "project last good");
        fs::write(
            root.join("projects.json"),
            serde_json::json!({
                "projects": [{"path": project, "data_dir": project_data}]
            })
            .to_string(),
        )
        .unwrap();
        let cache_path = base.join("cache.bin");
        let mut initial = crate::ingest_cache::IngestCache::cold();
        let first = collect_discovered(
            &mut initial,
            discover_at(vec![root.clone()], &base.join("home")),
        );
        assert_eq!(first.0.len(), 2);
        initial.save(&cache_path).unwrap();

        let connection = Connection::open(root.join("crush.db")).unwrap();
        let parts = serde_json::json!([{"type": "text", "data": {"text": "global new"}}]);
        connection
            .execute(
                "UPDATE messages SET parts = ?1 WHERE id = 'message'",
                [parts.to_string()],
            )
            .unwrap();
        connection
            .execute(
                "UPDATE sessions SET updated_at = 2000 WHERE id = 'same-session'",
                [],
            )
            .unwrap();
        drop(connection);
        fs::write(root.join("projects.json"), b"{broken").unwrap();

        let mut warm = crate::ingest_cache::IngestCache::load(&cache_path);
        let second = collect_discovered(&mut warm, discover_at(vec![root], &base.join("home")));
        let texts: BTreeSet<&str> = second
            .0
            .iter()
            .map(|message| message.text.as_ref())
            .collect();
        assert_eq!(texts, BTreeSet::from(["global new", "project last good"]));
        assert!(warm.output_complete());
        assert!(!warm.source_snapshot_safe());

        let _ = fs::remove_dir_all(base);
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_registry_roots_data_dirs_and_databases_are_rejected() {
        use std::os::unix::fs::symlink;

        let base = temp_dir("symlinks");
        let root = base.join("global");
        let outside = base.join("outside");
        fs::create_dir_all(&root).unwrap();
        write_db(&outside.join("crush.db"), "outside question");
        write_db(&outside.join("nested/crush.db"), "nested outside question");
        symlink(&outside, root.join("linked-data")).unwrap();
        symlink(outside.join("crush.db"), root.join("crush.db")).unwrap();
        let registry = serde_json::json!([
            {
                "path": base.join("project"),
                "data_dir": root.join("linked-data"),
                "last_accessed": 1
            },
            {
                "path": base.join("nested-project"),
                "data_dir": root.join("linked-data/nested"),
                "last_accessed": 2
            }
        ]);
        fs::write(root.join("projects.json"), registry.to_string()).unwrap();

        let discovery = discover_at(vec![root.clone()], &base.join("home")).unwrap();
        assert!(discovery.databases.is_empty());
        fs::rename(root.join("projects.json"), root.join("projects-real.json")).unwrap();
        symlink(root.join("projects-real.json"), root.join("projects.json")).unwrap();
        let discovery = discover_at(vec![root.clone()], &base.join("home")).unwrap();
        assert!(discovery.databases.is_empty());
        symlink(&root, base.join("linked-root")).unwrap();
        let discovery = discover_at(vec![base.join("linked-root")], &base.join("home")).unwrap();
        assert!(discovery.databases.is_empty());

        let _ = fs::remove_dir_all(base);
    }

    #[cfg(windows)]
    #[test]
    fn descendant_proof_accepts_windows_path_case_variants() {
        let base = temp_dir("case-descendant");
        let root = base.join("RegistryRoot");
        let data = root.join("Nested").join("Data");
        fs::create_dir_all(&data).unwrap();
        let upper_data = PathBuf::from(data.to_string_lossy().to_uppercase());
        assert!(matches!(dir_below(&upper_data, &root), Some(Ok(true))));
        assert!(data_dir_present(&upper_data, &root, &base.join("project")).unwrap());
        let _ = fs::remove_dir_all(base);
    }
}
