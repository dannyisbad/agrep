//! opencode adapter: native data-root/opencode/opencode*.db SQLite chat stores.
//! Release-channel databases are discovered dynamically; *.bak*/*.corrupted are ignored.
//! Each is opened
//! READ-ONLY through SQLite's live snapshot protocol, so committed WAL rows are visible
//! without disturbing a running writer; a DB that fails to open is skipped (never panics).
//!
//! Schema (identical across DBs):
//!   session(id, directory, ...)            -- directory -> project_name
//!   message(id, session_id, data JSON {role}, time_created ms)
//!   part(message_id, session_id, data JSON {type,text}, time_created)
//!
//! the user's text = concat of part.data.text for type=="text" parts whose message
//! has role=="user". But opencode bundles non-user content into the SAME user
//! message as extra "text" parts: tool-call narration ("Called the <X> tool with
//! the following input: {...}"), tool results ("Image read successfully"), and
//! file attachments (`<path>...</path><type>file</type><content>...`). Real corpora
//! mix these with a genuine typed part in the same message, so filtering is done per
//! PART (drop injected wrappers, then concatenate survivors) - never per message.
//! We under-include rather than risk labeling agent/system text as the user's.

use crate::ingest::registry::{metadata_is_link, path_eq, relative_path};
use crate::ingest::{
    cap_event_output, cap_str, is_wrapper, project_name, summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::ingest_cache::ReadOutcome;
use crate::model::{Event, Message};
use std::ffi::OsStr;
use std::path::Path;

/// `.bak*`/`.corrupted` siblings are intentionally absent.
fn data_dirs_for(
    home: &Path,
    xdg: Option<&OsStr>,
    local_app_data: Option<&OsStr>,
    home_override: bool,
    windows: bool,
) -> Vec<std::path::PathBuf> {
    let legacy = home.join(".local").join("share").join("opencode");
    let base = if home_override {
        home.join(".local").join("share")
    } else if let Some(xdg) = xdg.filter(|value| !value.is_empty()) {
        std::path::PathBuf::from(xdg)
    } else if windows {
        local_app_data
            .filter(|value| !value.is_empty())
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Local"))
    } else {
        home.join(".local").join("share")
    };
    let primary = base.join("opencode");
    if primary == legacy {
        vec![primary]
    } else {
        vec![primary, legacy]
    }
}

fn data_dirs_at(home: &Path) -> Vec<std::path::PathBuf> {
    let xdg = std::env::var_os("XDG_DATA_HOME");
    let local = std::env::var_os("LOCALAPPDATA");
    data_dirs_for(
        home,
        xdg.as_deref(),
        local.as_deref(),
        std::env::var_os("AGREP_HOME").is_some(),
        cfg!(target_os = "windows"),
    )
}

fn explicit_db_from(raw: &OsStr, data_dir: &Path) -> Option<std::path::PathBuf> {
    if raw.is_empty() {
        return None;
    }
    let path = std::path::PathBuf::from(raw);
    if path.is_absolute() {
        Some(path)
    } else if path.components().count() == 1
        && !raw.to_string_lossy().contains('/')
        && !raw.to_string_lossy().contains('\\')
    {
        Some(data_dir.join(path))
    } else {
        None
    }
}

fn explicit_db_at(data_dir: &Path) -> Option<std::path::PathBuf> {
    let raw = std::env::var_os("OPENCODE_DB")?;
    explicit_db_from(&raw, data_dir)
}

fn regular_db(path: &Path) -> bool {
    match std::fs::symlink_metadata(path) {
        Ok(meta) if meta.is_file() && !metadata_is_link(&meta) => true,
        Ok(meta) if metadata_is_link(&meta) => {
            crate::ingest::warn_source_skip("opencode", path, "symlink sources are not followed");
            false
        }
        Ok(_) => false,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
        Err(error) => {
            crate::ingest::warn_source_skip("opencode", path, &error);
            false
        }
    }
}

fn discovered_name(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            let name = name.to_ascii_lowercase();
            name.starts_with("opencode")
                && name.ends_with(".db")
                && !name.contains(".bak")
                && !name.contains(".corrupted")
        })
}

fn databases_in(dir: &Path) -> Vec<std::path::PathBuf> {
    let mut paths = Vec::new();
    match std::fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(meta) if metadata_is_link(&meta) => {
            crate::ingest::warn_source_skip("opencode", dir, "symlink sources are not followed");
            return paths;
        }
        Ok(_) => return paths,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return paths,
        Err(error) => {
            crate::ingest::warn_source_skip("opencode", dir, &error);
            return paths;
        }
    }
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) => {
            crate::ingest::warn_source_skip("opencode", dir, &error);
            return paths;
        }
    };
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("opencode", dir, &error);
                continue;
            }
        };
        let path = entry.path();
        if discovered_name(&path) && regular_db(&path) {
            paths.push(path);
        }
    }
    paths.sort();
    paths.dedup();
    paths
}

fn contains_path(paths: &[std::path::PathBuf], wanted: &Path) -> bool {
    paths.iter().any(|path| path_eq(path, wanted))
}

/// True for opencode-injected "text" parts that are NOT something the user typed:
/// tool-call narration, tool results, and file-attachment payloads. These appear
/// as `type:"text"` parts inside user messages alongside the real typed prompt.
fn is_injected_part(text: &str) -> bool {
    let t = text.trim_start();
    // File-attachment payload: <path>...</path><type>file</type><content>...
    t.starts_with("<path>")
        || t.starts_with("<type>")
        || t.starts_with("<content>")
        // Tool-call narration echoed back into the user turn.
        || t.starts_with("Called the ")
        // Tool result for an attached image.
        || t.starts_with("Image read successfully")
        // Shared command/system wrappers (<command-name>, <bash-input>, etc.).
        || is_wrapper(text)
}

#[derive(Default)]
struct PendingText {
    session: String,
    message_id: String,
    directory: String,
    parent: String,
    ts: i64,
    role: String,
    model: String,
    text: String,
    source_parts: u64,
}

fn flush_text(
    pending: PendingText,
    out: &mut Vec<crate::model::RawMessage>,
    turn: &mut u32,
    tally: &crate::intake::Tally,
) {
    if pending.role == "user" {
        if pending.text.trim().is_empty() {
            for _ in 0..pending.source_parts {
                tally.skip(crate::intake::Skip::EmptyText);
            }
            return;
        }
        if is_wrapper(&pending.text) {
            for _ in 0..pending.source_parts {
                tally.skip(crate::intake::Skip::Wrapper);
            }
            return;
        }
        for _ in 1..pending.source_parts {
            tally.skip(crate::intake::Skip::NonMessage);
        }
        tally.row();
        out.push(crate::model::RawMessage {
            agent: "opencode",
            project: project_name(&pending.directory),
            session: pending.session,
            ts: pending.ts,
            turn: *turn,
            text: pending.text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: !pending.parent.is_empty(),
            parent: pending.parent,
        });
        *turn += 1;
    } else if pending.role == "assistant" {
        if let Some(last) = out
            .last_mut()
            .filter(|message| message.session == pending.session)
        {
            let chars = crate::ingest::append_capped(
                &mut last.reply,
                &pending.text,
                crate::ingest::REPLY_CAP,
            );
            last.reply_chars += chars;
            if last.model.is_empty() && !pending.model.is_empty() {
                last.model = pending.model;
            }
        }
    }
}

/// Pull messages and tool events from one opencode database in one `part` table scan.
///
/// SQLite filters, extracts, and caps fields before they cross the FFI. SQLite text
/// functions stop at embedded NUL, so only those exceptional outputs cross as a full
/// BLOB and are capped losslessly in Rust.
fn collect_db(path: &std::path::Path) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    // seen = message-query part rows plus inspected child-session rows
    let tally = crate::intake::file("opencode", path);
    let conn = match crate::ingest::open_sqlite_ro(path) {
        Ok(c) => c,
        Err(e) => {
            // the file exists (callers filter on that), so a failed open is real news
            eprintln!(
                "  ! opencode: cannot open {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            );
            tally.error(&format!("cannot open: {e}"));
            return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
        }
    };

    // CASE is lazy in SQLite, so malformed candidate rows remain countable without
    // letting json_extract abort the statement and hide later valid rows.
    let mut stmt = match conn.prepare(
        "SELECT m.session_id, s.directory, m.time_created, m.id, \
                CASE WHEN json_valid(m.data) THEN json_extract(m.data,'$.role') END, \
                CASE WHEN json_valid(m.data) THEN json_extract(m.data,'$.modelID') END, \
                CASE WHEN json_valid(p.data) THEN json_extract(p.data,'$.type') END, \
                CASE WHEN json_valid(p.data) THEN json_extract(p.data,'$.text') END, \
                CASE WHEN json_valid(p.data) THEN json_extract(p.data,'$.tool') END, \
                CASE WHEN json_valid(p.data) THEN json_extract(p.data,'$.callID') END, \
                CASE WHEN json_valid(p.data) THEN json_extract(p.data,'$.state.status') END, \
                CASE WHEN json_valid(p.data) THEN json_extract(p.data,'$.state.input') END, \
                CAST(CASE WHEN instr(CAST(coalesce(CASE WHEN json_valid(p.data) \
                    THEN json_extract(p.data,'$.state.output') END,'') AS BLOB),X'00') > 0 \
                    THEN coalesce(CASE WHEN json_valid(p.data) \
                        THEN json_extract(p.data,'$.state.output') END,'') \
                    ELSE substr(coalesce(CASE WHEN json_valid(p.data) \
                        THEN json_extract(p.data,'$.state.output') END,''),1,4000) \
                    END AS BLOB), \
                length(coalesce(CASE WHEN json_valid(p.data) \
                    THEN json_extract(p.data,'$.state.output') END,'')), \
                length(CAST(coalesce(CASE WHEN json_valid(p.data) \
                    THEN json_extract(p.data,'$.state.output') END,'') AS BLOB)), \
                p.time_created, p.id, COALESCE(s.parent_id,''), s.id IS NOT NULL, \
                json_valid(m.data), json_valid(p.data) \
         FROM part p \
         JOIN message m ON p.message_id = m.id \
         LEFT JOIN session s ON m.session_id = s.id \
         WHERE (p.data LIKE '%\"type\":\"text\"%' OR p.data LIKE '%\"type\":\"tool\"%') \
           AND CASE WHEN json_valid(p.data) \
               THEN json_extract(p.data,'$.type') IN ('text','tool') ELSE 1 END \
         ORDER BY m.session_id, m.time_created, m.id, p.time_created, p.id",
    ) {
        Ok(s) => s,
        Err(e) => {
            tally.error(&format!("part query: {e}"));
            return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
        }
    };

    struct Row {
        session_id: String,
        directory: Option<String>,
        m_ts: i64,
        m_id: String,
        role: Option<String>,
        model: Option<String>,
        p_ty: String,
        p_text: Option<String>,
        tool: Option<String>,
        call_id: Option<String>,
        status: Option<String>,
        input: Option<String>,
        output: String,
        output_chars: usize,
        output_bytes: usize,
        p_ts: i64,
        p_id: String,
        parent_id: String,
        /// whether a session row exists at all (LEFT JOIN); text parts without one are dropped
        has_session: bool,
        message_json_valid: bool,
        part_json_valid: bool,
    }
    let rows = match stmt.query_map([], |row| {
        Ok(Row {
            session_id: row.get(0)?,
            directory: row.get(1)?,
            m_ts: row.get(2)?,
            m_id: row.get(3)?,
            role: row.get(4)?,
            model: row.get(5)?,
            p_ty: row.get::<_, Option<String>>(6)?.unwrap_or_default(),
            p_text: row.get(7)?,
            tool: row.get(8)?,
            call_id: row.get(9)?,
            status: row.get(10)?,
            input: row.get(11)?,
            output: String::from_utf8(row.get(12)?).map_err(|error| {
                rusqlite::Error::FromSqlConversionFailure(
                    12,
                    rusqlite::types::Type::Blob,
                    Box::new(error),
                )
            })?,
            output_chars: row.get(13)?,
            output_bytes: row.get(14)?,
            p_ts: row.get(15)?,
            p_id: row.get(16)?,
            parent_id: row.get(17)?,
            has_session: row.get::<_, i64>(18)? != 0,
            message_json_valid: row.get::<_, i64>(19)? != 0,
            part_json_valid: row.get::<_, i64>(20)? != 0,
        })
    }) {
        Ok(r) => r,
        Err(e) => {
            tally.error(&format!("part rows: {e}"));
            return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
        }
    };

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut turn = 0u32;
    let mut pending: Option<PendingText> = None;

    // Tool rows are buffered, then sorted by (session, part time, part id) before becoming Events.
    let mut tool_rows: Vec<Row> = Vec::new();
    // False when any part row, JSON-validity check, or row conversion failed; signals that
    // some data was skipped, so cached rows from prior scans may not safely be replaced.
    let mut main_scope_covered = true;

    for r in rows {
        tally.seen();
        let r = match r {
            Ok(r) => r,
            Err(e) => {
                tally.error(&format!("part row: {e}"));
                main_scope_covered = false;
                continue;
            }
        };
        if !r.part_json_valid {
            tally.error(&format!("malformed part JSON: {}", r.p_id));
            main_scope_covered = false;
            continue;
        }
        if !r.message_json_valid {
            tally.error(&format!("malformed message JSON: {}", r.m_id));
            main_scope_covered = false;
            continue;
        }
        if r.p_ty == "tool" {
            tally.agent_row();
            tool_rows.push(r);
            continue;
        }
        let Row {
            session_id,
            directory,
            m_ts,
            m_id,
            role,
            model,
            p_text,
            parent_id,
            has_session,
            ..
        } = r;
        // Drop text parts without a session row.
        if !has_session {
            tally.skip(crate::intake::Skip::Meta);
            continue;
        }
        let directory = directory.unwrap_or_default();
        let role = role.unwrap_or_default();
        let model = model.unwrap_or_default();
        if role != "user" && role != "assistant" {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }
        let ptext = match p_text {
            Some(t) if !t.trim().is_empty() => t,
            _ => {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
        };
        // Drop opencode-injected wrappers at the PART level - user turns only (the
        // assistant's prose is what we want to keep verbatim as the reply).
        if role == "user" && is_injected_part(&ptext) {
            tally.skip(crate::intake::Skip::Wrapper);
            continue;
        }
        if role == "assistant" {
            tally.agent_row();
        }

        if pending
            .as_ref()
            .is_some_and(|current| current.session != session_id)
        {
            flush_text(
                pending.take().expect("pending row"),
                &mut out,
                &mut turn,
                &tally,
            );
            turn = 0;
        }

        if pending
            .as_ref()
            .is_none_or(|current| current.message_id != m_id)
        {
            if let Some(current) = pending.take() {
                flush_text(current, &mut out, &mut turn, &tally);
            }
            pending = Some(PendingText {
                session: session_id,
                message_id: m_id,
                directory,
                parent: parent_id,
                ts: m_ts,
                role,
                model,
                ..PendingText::default()
            });
        }

        let current = pending
            .as_mut()
            .expect("a text row must have an accumulator");
        if !current.text.is_empty() {
            current.text.push('\n');
        }
        current.text.push_str(&ptext);
        current.source_parts += 1;
    }
    if let Some(current) = pending {
        flush_text(current, &mut out, &mut turn, &tally);
    }

    tool_rows.sort_by(|a, b| {
        a.session_id
            .cmp(&b.session_id)
            .then(a.p_ts.cmp(&b.p_ts))
            .then(a.p_id.cmp(&b.p_id))
    });
    let mut events: Vec<Event> = Vec::with_capacity(tool_rows.len());
    for r in tool_rows {
        let (input, input_chars) = r
            .input
            .as_deref()
            .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok())
            .map(|v| summarize_tool_input_with_chars(&v))
            .unwrap_or_default();
        let recovered_nul_output = r.output.contains('\0');
        let (output, output_chars, output_bytes) = if recovered_nul_output {
            cap_event_output(&r.output)
        } else {
            (
                cap_str(&r.output, EVENT_CAP),
                r.output_chars,
                r.output_bytes,
            )
        };
        let ok = match r.status.as_deref() {
            Some("completed") => Some(true),
            Some("error") => Some(false),
            _ => None,
        };
        tally.event();
        events.push(Event {
            agent: "opencode",
            session: r.session_id,
            ts: r.p_ts,
            kind: "tool",
            name: r.tool.unwrap_or_else(|| "?".to_string()),
            input,
            output,
            input_chars,
            output_chars,
            output_bytes,
            ok,
            call_id: r.call_id.unwrap_or(r.p_id),
            child_session: String::new(),
        });
    }

    // Subagent sessions: a child session (parent_id set) becomes a subagent_start event
    // in the parent. The child is independently viewable, so just link it.
    let child_query = conn.prepare(
        "SELECT id, parent_id, COALESCE(title,''), time_created \
         FROM session WHERE parent_id IS NOT NULL AND parent_id != ''",
    );
    // A child row or query the reader could not decode leaves that link unobserved, not
    // absent; the subagent scope is then uncovered and cannot license wholesale replacement.
    let mut child_scope_covered = true;
    match child_query {
        Ok(mut stmt) => match stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
            ))
        }) {
            Ok(srows) => {
                for row in srows {
                    tally.seen();
                    let (child, parent, title, ts) = match row {
                        Ok(row) => row,
                        Err(e) => {
                            tally.error(&format!("session row: {e}"));
                            child_scope_covered = false;
                            continue;
                        }
                    };
                    tally.agent_row();
                    tally.event();
                    events.push(Event {
                        agent: "opencode",
                        session: parent,
                        ts,
                        kind: "subagent_start",
                        name: if title.is_empty() {
                            "subagent".to_string()
                        } else {
                            cap_str(&title, 200)
                        },
                        input: String::new(),
                        output: String::new(),
                        input_chars: 0,
                        output_chars: 0,
                        output_bytes: 0,
                        ok: None,
                        call_id: child.clone(),
                        child_session: child,
                    });
                }
            }
            Err(e) => {
                tally.seen();
                tally.error(&format!("session rows: {e}"));
                child_scope_covered = false;
            }
        },
        Err(e) => {
            tally.seen();
            tally.error(&format!("session query: {e}"));
            child_scope_covered = false;
        }
    }

    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        events,
        if child_scope_covered && main_scope_covered {
            ReadOutcome::Complete
        } else {
            ReadOutcome::Partial
        },
    )
}

/// Open each live opencode DB read-only and collect the user's typed messages + tool events.
/// Cache-driven like claude/codex: a DB whose (mtime, size) hasn't moved is served from the
/// parse cache instead of a full table scan. The cache stamp includes the committed WAL,
/// so a WAL-only commit cannot be mistaken for a main-file cache hit.
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let dirs = data_dirs_at(&crate::ingest::home());
    let explicit = explicit_db_at(&dirs[0]);
    let mut messages = Vec::new();
    let mut events = Vec::new();
    let mut seen = Vec::new();
    for dir in dirs {
        let mut files = databases_in(&dir);
        if let Some(path) = explicit
            .as_ref()
            .filter(|path| relative_path(path, &dir).is_some())
        {
            if regular_db(path) && !contains_path(&files, path) {
                files.push(path.clone());
                files.sort();
            }
        }
        for path in &files {
            if !contains_path(&seen, path) {
                seen.push(path.clone());
            }
        }
        let pass =
            crate::ingest_cache::collect_cached_for(cache, "opencode", &dir, &files, collect_db);
        messages.extend(pass.messages);
        events.extend(pass.events);
    }
    if let Some(path) = explicit.filter(|path| !contains_path(&seen, path)) {
        let files = if regular_db(&path) {
            std::slice::from_ref(&path)
        } else {
            &[]
        };
        let pass =
            crate::ingest_cache::collect_cached_for(cache, "opencode", &path, files, collect_db);
        messages.extend(pass.messages);
        events.extend(pass.events);
    }
    (messages, events)
}

/// Registry entry (see ingest::registry). Byte-identical wrapper over `collect`.
pub struct Opencode;
impl crate::ingest::registry::Adapter for Opencode {
    fn name(&self) -> &'static str {
        "opencode"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        let dirs = data_dirs_at(&crate::ingest::home());
        let mut roots = dirs.clone();
        if let Some(path) = explicit_db_at(&dirs[0]) {
            roots.push(path);
        }
        roots
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        discovered_name(path)
            || explicit_db_at(&data_dirs_at(&crate::ingest::home())[0])
                .is_some_and(|explicit| path_eq(&explicit, path))
    }
    fn freshness_content(&self, path: &std::path::Path) -> bool {
        self.store_content(path)
    }
}

#[cfg(test)]
mod tests {
    #[cfg(windows)]
    use super::contains_path;
    use super::{collect_db, data_dirs_for, databases_in, explicit_db_from, ReadOutcome};
    use crate::ingest_cache::{collect_cached_for, IngestCache};

    fn temp_dir(label: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "agrep-opencode-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    #[test]
    fn platform_data_roots_are_native_first_with_legacy_fallback() {
        let home = std::path::Path::new("/home/tester");
        let xdg = std::ffi::OsStr::new("/var/data");
        assert_eq!(
            data_dirs_for(home, Some(xdg), None, false, false),
            [
                std::path::PathBuf::from("/var/data/opencode"),
                home.join(".local/share/opencode"),
            ]
        );
        assert_eq!(
            data_dirs_for(
                home,
                None,
                Some(std::ffi::OsStr::new("C:/Local")),
                false,
                true,
            )[0],
            std::path::PathBuf::from("C:/Local").join("opencode")
        );
        assert_eq!(
            data_dirs_for(home, Some(xdg), None, true, false),
            [home.join(".local/share/opencode")]
        );
    }

    #[test]
    fn explicit_override_is_absolute_or_one_filename() {
        let root = std::path::Path::new("/data/opencode");
        assert_eq!(
            explicit_db_from(std::ffi::OsStr::new("nightly.db"), root),
            Some(root.join("nightly.db"))
        );
        assert!(explicit_db_from(std::ffi::OsStr::new("nested/nightly.db"), root).is_none());
        assert!(explicit_db_from(std::ffi::OsStr::new("nested\\nightly.db"), root).is_none());
    }

    #[cfg(windows)]
    #[test]
    fn explicit_override_case_variant_is_not_discovered_twice() {
        let files = vec![std::path::PathBuf::from(
            r"C:\Users\tester\AppData\Local\opencode\opencode.db",
        )];
        assert!(contains_path(
            &files,
            std::path::Path::new(r"c:\users\tester\appdata\local\OpenCode\OpenCode.DB")
        ));
    }

    #[test]
    fn discovers_channel_databases_but_not_backups_or_symlinks() {
        let dir = temp_dir("channels");
        std::fs::create_dir_all(&dir).unwrap();
        for name in ["opencode.db", "opencode-nightly.db", "OpenCode-Beta.DB"] {
            std::fs::write(dir.join(name), b"db").unwrap();
        }
        for name in ["opencode.bak.db", "opencode.corrupted.db", "unrelated.db"] {
            std::fs::write(dir.join(name), b"skip").unwrap();
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(dir.join("opencode.db"), dir.join("opencode-link.db")).unwrap();
        let names: Vec<_> = databases_in(&dir)
            .into_iter()
            .map(|path| path.file_name().unwrap().to_owned())
            .collect();
        assert_eq!(names.len(), 3);
        assert!(names.contains(&std::ffi::OsString::from("opencode-nightly.db")));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn dropped_leading_turns_do_not_leak_replies_across_sessions() {
        let dir = temp_dir("xsession");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.db");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            // Session sb's only user message is injected tool narration (dropped), so
            // its assistant reply has no user turn of its own to attach to.
            conn.execute_batch(
                "CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
                 CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
                 CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                 INSERT INTO session VALUES ('sa', '/work/alpha', NULL, NULL, 1000);
                 INSERT INTO session VALUES ('sb', '/work/beta', NULL, NULL, 2000);
                 INSERT INTO message VALUES ('m1', 'sa', '{\"role\":\"user\"}', 1000);
                 INSERT INTO message VALUES ('m2', 'sa', '{\"role\":\"assistant\"}', 1001);
                 INSERT INTO message VALUES ('m3', 'sb', '{\"role\":\"user\"}', 2000);
                 INSERT INTO message VALUES ('m4', 'sb', '{\"role\":\"assistant\",\"modelID\":\"leaked-model\"}', 2001);
                 INSERT INTO part VALUES ('p1', 'm1', 'sa', '{\"type\":\"text\",\"text\":\"session a question\"}', 1000);
                 INSERT INTO part VALUES ('p2', 'm2', 'sa', '{\"type\":\"text\",\"text\":\"session a answer\"}', 1001);
                 INSERT INTO part VALUES ('p3', 'm3', 'sb', '{\"type\":\"text\",\"text\":\"Called the read tool with the following input: {}\"}', 2000);
                 INSERT INTO part VALUES ('p4', 'm4', 'sb', '{\"type\":\"text\",\"text\":\"SESSION B LEAKED REPLY\"}', 2001);",
            )
            .unwrap();
        }
        let (messages, _, outcome) = collect_db(&path);
        let healthy = outcome == ReadOutcome::Complete;
        assert!(healthy);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "session a question");
        assert_eq!(&*messages[0].reply, "session a answer");
        assert_eq!(&*messages[0].model, "");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn embedded_nul_tool_output_survives_ingest_and_cache_discloses_source_units() {
        let dir = temp_dir("nul-output");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.db");
        let short_output = "abc\0déf".to_string();
        let long_output = format!("head\0{}", "é🙂".repeat(super::EVENT_CAP));
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
                 CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
                 CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                 INSERT INTO session VALUES ('s1', '/work/alpha', NULL, NULL, 1000);
                 INSERT INTO message VALUES ('m1', 's1', '{\"role\":\"assistant\"}', 1000);",
            )
            .unwrap();
            for (id, call_id, output, timestamp) in [
                ("p1", "call-short", &short_output, 1000_i64),
                ("p2", "call-long", &long_output, 1001_i64),
            ] {
                let data = serde_json::json!({
                    "type": "tool",
                    "tool": "shell",
                    "callID": call_id,
                    "state": {
                        "status": "completed",
                        "input": {},
                        "output": output,
                    },
                })
                .to_string();
                conn.execute(
                    "INSERT INTO part VALUES (?1, 'm1', 's1', ?2, ?3)",
                    rusqlite::params![id, data, timestamp],
                )
                .unwrap();
            }
        }

        let (_, events, outcome) = collect_db(&path);
        let healthy = outcome == ReadOutcome::Complete;
        assert!(healthy);
        assert_eq!(events.len(), 2);
        let short = events
            .iter()
            .find(|event| event.call_id == "call-short")
            .unwrap();
        assert_eq!(short.output, short_output);
        assert_eq!(short.output_chars, short_output.chars().count());
        assert_eq!(short.output_bytes, short_output.len());
        let long = events
            .iter()
            .find(|event| event.call_id == "call-long")
            .unwrap();
        assert_eq!(long.output, super::cap_str(&long_output, super::EVENT_CAP));
        assert_eq!(long.output_chars, long_output.chars().count());
        assert_eq!(long.output_bytes, long_output.len());

        let event_dir = dir.join("events");
        let event_name = crate::cache::event_fname("opencode", "s1");
        let keep = std::collections::HashSet::from([event_name.clone()]);
        crate::cache::write_events(
            &events,
            &event_dir,
            &keep,
            &["opencode"],
            true,
            &std::collections::HashSet::new(),
        )
        .unwrap();
        let event_store =
            rusqlite::Connection::open(event_dir.join(crate::cache::EVENT_STORE_NAME)).unwrap();
        let payload: Vec<u8> = event_store
            .query_row(
                "SELECT payload FROM event_sessions WHERE name=?1",
                [&event_name],
                |row| row.get(0),
            )
            .unwrap();
        let rows: Vec<serde_json::Value> = std::str::from_utf8(&payload)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        let short = rows
            .iter()
            .find(|row| row["call_id"] == "call-short")
            .unwrap();
        assert_eq!(short["output"], short_output);
        assert_eq!(short["output_chars"], short_output.chars().count());
        assert_eq!(short["output_bytes"], short_output.len());
        assert!(short.get("output_truncated").is_none());
        let long = rows
            .iter()
            .find(|row| row["call_id"] == "call-long")
            .unwrap();
        assert_eq!(
            long["output"],
            super::cap_str(&long_output, super::EVENT_CAP)
        );
        assert_eq!(long["output_chars"], long_output.chars().count());
        assert_eq!(long["output_bytes"], long_output.len());
        assert_eq!(long["output_truncated"], true);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn malformed_matching_part_does_not_abort_later_rows() {
        let dir = temp_dir("malformed-row");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.db");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
                   CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
                   CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                   INSERT INTO session VALUES ('s1', '/work/alpha', NULL, NULL, 1000);
                   INSERT INTO message VALUES ('m1', 's1', '{"role":"user"}', 1000);
                   INSERT INTO message VALUES ('m2', 's1', '{"role":"user"}', 2000);
                   INSERT INTO message VALUES ('m3', 's1', '{"role":"user"}', 3000);
                   INSERT INTO part VALUES ('p1', 'm1', 's1', '{"type":"text","text":"valid before"}', 1000);
                   INSERT INTO part VALUES ('p2', 'm2', 's1', '{"type":"text","text":"broken"', 2000);
                   INSERT INTO part VALUES ('p3', 'm3', 's1', '{"type":"text","text":"valid after"}', 3000);"#,
            )
            .unwrap();
        }

        let (messages, events, outcome) = collect_db(&path);
        assert_eq!(
            outcome,
            ReadOutcome::Partial,
            "malformed part JSON must yield Partial, not Complete",
        );
        assert!(events.is_empty());
        assert_eq!(messages.len(), 2);
        assert_eq!(&*messages[0].text, "valid before");
        assert_eq!(&*messages[1].text, "valid after");
        assert_eq!(messages[1].turn, 1);

        let cache_path = dir.join("ingest-cache.bin");
        let files = [path.clone()];
        let mut cache = IngestCache::cold();
        let first = collect_cached_for(&mut cache, "opencode", &dir, &files, collect_db);
        assert_eq!(first.parsed, 1);
        assert_eq!(first.messages.len(), 2);
        assert!(!cache.source_snapshot_safe());
        assert_eq!(cache.source_read_issues().len(), 1);
        assert_eq!(
            cache.source_read_issues()[0].reason,
            "source parser could not cover part of the read"
        );
        cache.save(&cache_path).unwrap();

        let mut unchanged = IngestCache::load(&cache_path);
        let second = collect_cached_for(&mut unchanged, "opencode", &dir, &files, collect_db);
        assert_eq!(
            second.parsed, 1,
            "an unchanged partial source must be reparsed, not cache-hit"
        );
        assert_eq!(second.messages.len(), 2);
        assert!(!unchanged.source_snapshot_safe());
        assert_eq!(unchanged.source_read_issues().len(), 1);
        assert_eq!(unchanged.source_read_issues()[0].kind, "source-read-failed");
        unchanged.save(&cache_path).unwrap();

        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"INSERT INTO message VALUES ('m4', 's1', '{"role":"user"}', 4000);
                   INSERT INTO part VALUES ('p4', 'm4', 's1', '{"type":"text","text":"new valid row"}', 4000);"#,
            )
            .unwrap();
        }
        let mut changed = IngestCache::load(&cache_path);
        let third = collect_cached_for(&mut changed, "opencode", &dir, &files, collect_db);
        assert_eq!(third.parsed, 1);
        assert_eq!(third.messages.len(), 3);
        assert_eq!(&*third.messages[2].text, "new valid row");
        assert!(!changed.source_snapshot_safe());
        assert_eq!(changed.source_read_issues().len(), 1);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn malformed_message_json_yields_partial_outcome() {
        let dir = temp_dir("malformed-msg-json");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.db");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
                   CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
                   CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                   INSERT INTO session VALUES ('s1', '/work/alpha', NULL, NULL, 1000);
                   INSERT INTO message VALUES ('m1', 's1', '{"role":"user"}', 1000);
                   INSERT INTO message VALUES ('m2', 's1', 'BROKEN_MSG_JSON', 2000);
                   INSERT INTO message VALUES ('m3', 's1', '{"role":"user"}', 3000);
                   INSERT INTO part VALUES ('p1', 'm1', 's1', '{"type":"text","text":"before"}', 1000);
                   INSERT INTO part VALUES ('p2', 'm2', 's1', '{"type":"text","text":"mid"}', 2000);
                   INSERT INTO part VALUES ('p3', 'm3', 's1', '{"type":"text","text":"after"}', 3000);"#,
            )
            .unwrap();
        }
        let (messages, _events, outcome) = collect_db(&path);
        assert_eq!(
            outcome,
            ReadOutcome::Partial,
            "malformed message JSON must yield Partial, not Complete",
        );
        assert_eq!(messages.len(), 2);
        assert_eq!(&*messages[0].text, "before");
        assert_eq!(&*messages[1].text, "after");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn undecodable_child_session_row_yields_partial_outcome() {
        let dir = temp_dir("null-child-ts");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.db");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
                   CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
                   CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                   INSERT INTO session VALUES ('parent', '/work/alpha', NULL, NULL, 1000);
                   INSERT INTO session VALUES ('child', '/work/alpha', 'parent', 'probe task', 1500);
                   INSERT INTO message VALUES ('m1', 'parent', '{"role":"user"}', 1000);
                   INSERT INTO part VALUES ('p1', 'm1', 'parent', '{"type":"text","text":"launch a subagent"}', 1000);"#,
            )
            .unwrap();
        }
        let (messages, events, outcome) = collect_db(&path);
        assert_eq!(outcome, ReadOutcome::Complete);
        assert_eq!(messages.len(), 1);
        assert_eq!(
            events
                .iter()
                .filter(|event| event.kind == "subagent_start")
                .count(),
            1
        );

        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE session SET time_created = NULL WHERE id = 'child'",
            [],
        )
        .unwrap();
        drop(conn);
        let (messages, events, outcome) = collect_db(&path);
        assert_eq!(outcome, ReadOutcome::Partial);
        assert_eq!(messages.len(), 1);
        assert!(events.iter().all(|event| event.kind != "subagent_start"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn failing_child_session_query_yields_partial_outcome() {
        let dir = temp_dir("no-title-column");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.db");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, time_created INTEGER);
                   CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
                   CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                   INSERT INTO session VALUES ('s1', '/work/alpha', NULL, 1000);
                   INSERT INTO message VALUES ('m1', 's1', '{"role":"user"}', 1000);
                   INSERT INTO part VALUES ('p1', 'm1', 's1', '{"type":"text","text":"still readable"}', 1000);"#,
            )
            .unwrap();
        }
        let (messages, _, outcome) = collect_db(&path);
        assert_eq!(outcome, ReadOutcome::Partial);
        assert_eq!(messages.len(), 1);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn undecodable_message_row_yields_partial_outcome() {
        let dir = temp_dir("null-message-ts");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("opencode.db");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT, title TEXT, time_created INTEGER);
                   CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT, time_created INTEGER);
                   CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                   INSERT INTO session VALUES ('s1', '/work/alpha', NULL, NULL, 1000);
                   INSERT INTO message VALUES ('m1', 's1', '{"role":"user"}', NULL);
                   INSERT INTO message VALUES ('m2', 's1', '{"role":"user"}', 2000);
                   INSERT INTO part VALUES ('p1', 'm1', 's1', '{"type":"text","text":"lost to decode"}', 1000);
                   INSERT INTO part VALUES ('p2', 'm2', 's1', '{"type":"text","text":"survivor"}', 2000);"#,
            )
            .unwrap();
        }
        let (messages, _, outcome) = collect_db(&path);
        assert_eq!(outcome, ReadOutcome::Partial);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "survivor");
        let _ = std::fs::remove_dir_all(dir);
    }
}
