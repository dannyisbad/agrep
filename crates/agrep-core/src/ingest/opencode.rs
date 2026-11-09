//! opencode adapter: native data-root/opencode/opencode*.db SQLite chat stores.
//! Release-channel databases are discovered dynamically; *.bak*/*.corrupted are ignored.
//! Each is opened
//! READ-ONLY through SQLite's live snapshot protocol, so committed WAL rows are visible
//! without disturbing a running writer; a DB that fails to open is skipped (never panics).
//!
//! Two on-disk schemas exist and are detected per database (opencode 2.x migrates
//! in place, dropping the old tables):
//!
//! v1 (opencode 0.x/1.x):
//!   session(id, directory, parent_id, title, ...)   -- directory -> project_name
//!   message(id, session_id, data JSON {role,modelID}, time_created ms)
//!   part(message_id, session_id, data JSON {type,text,tool,callID,state}, time_created)
//!
//! v2 (opencode 2.x):
//!   session_v2(id, parent_id, directory, title, ...)
//!   session_message(id, session_id, type 'user'|'assistant', seq, time_created ms,
//!                   data JSON) -- user: {text}; assistant: {model:{id},
//!                   content:[{type:'reasoning'|'text'|'tool', ...}]}; a tool part
//!                   carries {id,name,state:{status,input,content:[{type:'text',text}]}}
//!
//! the user's text = concat of text parts whose message has role=="user". opencode v1
//! bundles non-user content into the SAME user message as extra "text" parts:
//! tool-call narration ("Called the <X> tool with the following input: {...}"),
//! tool results ("Image read successfully"), and file attachments
//! (`<path>...</path><type>file</type><content>...`). Real corpora mix these with a
//! genuine typed part in the same message, so filtering is done per PART (drop
//! injected wrappers, then concatenate survivors) - never per message. We
//! under-include rather than risk labeling agent/system text as the user's.

use crate::ingest::registry::{
    metadata_is_link, path_eq, refused_root_link, relative_path, root_file, root_metadata,
};
use crate::ingest::{
    cap_event_output, cap_str, is_wrapper, project_name, summarize_tool_input_with_chars, EVENT_CAP,
};
use crate::intake::{Skip, Tally};
use crate::intake_batch::ReadOutcome;
use crate::model::{Event, Message, RawMessage};
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

pub(crate) fn data_dirs_at(home: &Path) -> Vec<std::path::PathBuf> {
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

/// A database found in a data dir: a plain regular file; links there are never followed.
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

/// The database `OPENCODE_DB` names, a store root: a regular file, or a link to one followed once.
fn explicit_db(path: &Path) -> bool {
    match root_file(path) {
        Ok((_, meta)) => meta.is_file(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
        // The census reports a refused link against the root itself.
        Err(error) if refused_root_link(&error) => false,
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

/// The databases in data dir `dir`, a store root that may be a link, followed once. The
/// explicit database, when it sits here, is the caller's to judge.
fn databases_in(dir: &Path, explicit: Option<&Path>) -> Vec<std::path::PathBuf> {
    let mut paths = Vec::new();
    match root_metadata(dir) {
        Ok(meta) if meta.is_dir() => {}
        Ok(_) => return paths,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound || refused_root_link(&error) => {
            return paths
        }
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
        if discovered_name(&path)
            && !explicit.is_some_and(|explicit| path_eq(explicit, &path))
            && regular_db(&path)
        {
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
pub(crate) fn is_injected_part(text: &str) -> bool {
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

/// One row per readable text/tool unit, shaped identically for both schemas so the
/// accumulation pipeline below stays shared. SQLite filters, extracts, and caps
/// fields before they cross the FFI. SQLite text functions stop at embedded NUL,
/// so only those exceptional outputs cross as a full BLOB and are capped losslessly
/// in Rust.
const PART_SQL_V1: &str = "SELECT m.session_id, s.directory, m.time_created, m.id, \
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
     ORDER BY m.session_id, m.time_created, m.id, p.time_created, p.id";

const CHILD_SQL_V1: &str = "SELECT id, parent_id, COALESCE(title,''), time_created \
     FROM session WHERE parent_id IS NOT NULL AND parent_id != ''";

/// v2 keeps every column position/type of v1: content parts live inline in
/// session_message.data, so the assistant side expands through json_each while the
/// user side is one synthetic 'text' row. A tool part's output is state.output when
/// present (older shape) else its state.content text blocks joined by newline.
const PART_SQL_V2: &str = "SELECT m.session_id, s.directory, m.time_created, m.id, \
            m.type, \
            CASE WHEN json_valid(m.data) THEN json_extract(m.data,'$.model.id') END, \
            CASE WHEN m.type='user' THEN 'text' \
                 ELSE COALESCE(json_extract(c.value,'$.type'),'') END, \
            CASE WHEN m.type='user' THEN CASE WHEN json_valid(m.data) \
                     THEN json_extract(m.data,'$.text') END \
                 ELSE json_extract(c.value,'$.text') END, \
            json_extract(c.value,'$.name'), \
            json_extract(c.value,'$.id'), \
            json_extract(c.value,'$.state.status'), \
            json_extract(c.value,'$.state.input'), \
            CAST(CASE WHEN instr(CAST(coalesce(json_extract(c.value,'$.state.output'), \
                    (SELECT group_concat(json_extract(j.value,'$.text'), char(10)) \
                     FROM json_each(c.value,'$.state.content') j \
                     WHERE json_extract(j.value,'$.type')='text'),'') AS BLOB),X'00') > 0 \
                THEN coalesce(json_extract(c.value,'$.state.output'), \
                    (SELECT group_concat(json_extract(j.value,'$.text'), char(10)) \
                     FROM json_each(c.value,'$.state.content') j \
                     WHERE json_extract(j.value,'$.type')='text'),'') \
                ELSE substr(coalesce(json_extract(c.value,'$.state.output'), \
                    (SELECT group_concat(json_extract(j.value,'$.text'), char(10)) \
                     FROM json_each(c.value,'$.state.content') j \
                     WHERE json_extract(j.value,'$.type')='text'),''),1,4000) \
                END AS BLOB), \
            length(coalesce(json_extract(c.value,'$.state.output'), \
                (SELECT group_concat(json_extract(j.value,'$.text'), char(10)) \
                 FROM json_each(c.value,'$.state.content') j \
                 WHERE json_extract(j.value,'$.type')='text'),'')), \
            length(CAST(coalesce(json_extract(c.value,'$.state.output'), \
                (SELECT group_concat(json_extract(j.value,'$.text'), char(10)) \
                 FROM json_each(c.value,'$.state.content') j \
                 WHERE json_extract(j.value,'$.type')='text'),'') AS BLOB)), \
            COALESCE(json_extract(c.value,'$.time.created'), m.time_created), \
            m.id || ':' || COALESCE(c.key,'u'), \
            COALESCE(s.parent_id,''), s.id IS NOT NULL, \
            json_valid(m.data), \
            CASE WHEN m.type='assistant' AND json_valid(m.data) \
                 THEN CASE WHEN c.value IS NULL THEN 0 ELSE json_valid(c.value) END \
                 ELSE json_valid(m.data) END \
     FROM session_message m \
     LEFT JOIN session_v2 s ON m.session_id = s.id \
     LEFT JOIN json_each(CASE WHEN m.type='assistant' AND json_valid(m.data) \
               THEN json_extract(m.data,'$.content') END) c \
     WHERE m.type='user' \
        OR (m.type='assistant' AND NOT json_valid(m.data)) \
        OR json_extract(c.value,'$.type') IN ('text','tool') \
     ORDER BY m.session_id, m.time_created, m.id, COALESCE(c.key,-1)";

const CHILD_SQL_V2: &str = "SELECT id, parent_id, COALESCE(title,''), time_created \
     FROM session_v2 WHERE parent_id IS NOT NULL AND parent_id != ''";

/// One part row of `PART_SQL_V1`/`PART_SQL_V2`, by column position.
struct PartRow {
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

impl PartRow {
    fn read(row: &rusqlite::Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
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
    }
}

/// A text part that passed the per-part filters, on its way into its message.
struct TextPart {
    session: String,
    message_id: String,
    directory: String,
    parent: String,
    ts: i64,
    role: String,
    model: String,
    text: String,
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
    /// A work part came between the previous prose and this message's first text part.
    worked: bool,
}

/// One database's part-row scan: text parts join per message into user rows carrying
/// their assistant replies; tool parts are buffered for `tool_events`.
struct PartScan<'t> {
    tally: &'t Tally,
    out: Vec<RawMessage>,
    turn: u32,
    pending: Option<PendingText>,
    // part rows arrive in store order (message time, then part key): a work part sets the
    // flag the next prose consumes; a prompt opens a turn with none
    worked: bool,
    worked_session: String,
    // Tool rows are buffered, then sorted by (session, part time, part id) before becoming Events.
    tool_rows: Vec<PartRow>,
    // False when any part row, JSON-validity check, or row conversion failed; signals that
    // some data was skipped, so cached rows from prior scans may not safely be replaced.
    covered: bool,
}

impl<'t> PartScan<'t> {
    fn new(tally: &'t Tally) -> Self {
        Self {
            tally,
            out: Vec::new(),
            turn: 0,
            pending: None,
            worked: false,
            worked_session: String::new(),
            tool_rows: Vec::new(),
            covered: true,
        }
    }

    fn row(&mut self, row: rusqlite::Result<PartRow>) {
        self.tally.seen();
        let row = match row {
            Ok(row) => row,
            Err(e) => {
                self.tally.error(&format!("part row: {e}"));
                self.covered = false;
                return;
            }
        };
        if !row.part_json_valid {
            self.tally
                .error(&format!("malformed part JSON: {}", row.p_id));
            self.covered = false;
            return;
        }
        if !row.message_json_valid {
            self.tally
                .error(&format!("malformed message JSON: {}", row.m_id));
            self.covered = false;
            return;
        }
        if row.session_id != self.worked_session {
            self.worked_session.clone_from(&row.session_id);
            self.worked = false;
        }
        if row.p_ty == "tool" {
            self.tool_row(row);
        } else if let Some(part) = self.text_part(row) {
            self.append_text(part);
        }
    }

    fn tool_row(&mut self, row: PartRow) {
        self.tally.agent_row();
        self.worked |= !row
            .tool
            .as_deref()
            .is_some_and(crate::ingest::is_bookkeeping_tool);
        self.tool_rows.push(row);
    }

    /// The row's text part, or None (counted) when a per-part filter drops it.
    fn text_part(&self, row: PartRow) -> Option<TextPart> {
        let PartRow {
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
        } = row;
        // Drop text parts without a session row.
        if !has_session {
            self.tally.skip(Skip::Meta);
            return None;
        }
        let directory = directory.unwrap_or_default();
        let role = role.unwrap_or_default();
        let model = model.unwrap_or_default();
        if role != "user" && role != "assistant" {
            self.tally.skip(Skip::NonHuman);
            return None;
        }
        let text = match p_text {
            Some(t) if !t.trim().is_empty() => t,
            _ => {
                self.tally.skip(Skip::EmptyText);
                return None;
            }
        };
        // Drop opencode-injected wrappers at the PART level - user turns only (the
        // assistant's prose is what we want to keep verbatim as the reply).
        if role == "user" && is_injected_part(&text) {
            self.tally.skip(Skip::Wrapper);
            return None;
        }
        if role == "assistant" {
            self.tally.agent_row();
        }
        Some(TextPart {
            session: session_id,
            message_id: m_id,
            directory,
            parent: parent_id,
            ts: m_ts,
            role,
            model,
            text,
        })
    }

    fn append_text(&mut self, part: TextPart) {
        if let Some(previous) = self
            .pending
            .take_if(|current| current.session != part.session)
        {
            self.flush(previous);
            self.turn = 0;
        }
        if self
            .pending
            .as_ref()
            .is_none_or(|current| current.message_id != part.message_id)
        {
            if let Some(previous) = self.pending.take() {
                self.flush(previous);
            }
            let worked_before = std::mem::take(&mut self.worked);
            self.pending = Some(PendingText {
                session: part.session,
                message_id: part.message_id,
                directory: part.directory,
                parent: part.parent,
                ts: part.ts,
                worked: part.role == "assistant" && worked_before,
                role: part.role,
                model: part.model,
                ..PendingText::default()
            });
        }
        let current = self
            .pending
            .as_mut()
            .expect("a text row must have an accumulator");
        if !current.text.is_empty() {
            current.text.push('\n');
        }
        current.text.push_str(&part.text);
        current.source_parts += 1;
    }

    fn flush(&mut self, pending: PendingText) {
        if pending.role == "user" {
            self.flush_user(pending);
        } else if pending.role == "assistant" {
            self.join_reply(pending);
        }
    }

    fn flush_user(&mut self, pending: PendingText) {
        if pending.text.trim().is_empty() {
            self.tally.skip_n(Skip::EmptyText, pending.source_parts);
            return;
        }
        if is_wrapper(&pending.text) {
            self.tally.skip_n(Skip::Wrapper, pending.source_parts);
            return;
        }
        self.tally
            .skip_n(Skip::NonMessage, pending.source_parts.saturating_sub(1));
        self.tally.row();
        self.out.push(RawMessage {
            agent: "opencode",
            project: project_name(&pending.directory),
            session: pending.session,
            ts: pending.ts,
            turn: self.turn,
            text: pending.text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: !pending.parent.is_empty(),
            parent: pending.parent,
            reply_shape: Default::default(),
        });
        self.turn += 1;
    }

    fn join_reply(&mut self, pending: PendingText) {
        let Some(last) = self
            .out
            .last_mut()
            .filter(|message| message.session == pending.session)
        else {
            return;
        };
        crate::ingest::append_reply(last, &pending.text);
        crate::ingest::ReplyOrder::joined_known(&mut last.reply_shape, pending.worked);
        if last.model.is_empty() && !pending.model.is_empty() {
            last.model = pending.model;
        }
    }

    /// The user rows, the buffered tool rows and whether every part row was read.
    fn finish(mut self) -> (Vec<RawMessage>, Vec<PartRow>, bool) {
        if let Some(current) = self.pending.take() {
            self.flush(current);
        }
        (self.out, self.tool_rows, self.covered)
    }
}

fn tool_events(mut rows: Vec<PartRow>, tally: &Tally) -> Vec<Event> {
    rows.sort_by(|a, b| {
        a.session_id
            .cmp(&b.session_id)
            .then(a.p_ts.cmp(&b.p_ts))
            .then(a.p_id.cmp(&b.p_id))
    });
    let mut events: Vec<Event> = Vec::with_capacity(rows.len());
    for row in rows {
        let event = tool_event(row);
        tally.event();
        events.push(event);
    }
    events
}

fn tool_event(r: PartRow) -> Event {
    let name = r.tool.unwrap_or_else(|| "?".to_string());
    let args = r
        .input
        .as_deref()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok());
    let (input, input_chars) = args
        .as_ref()
        .map(summarize_tool_input_with_chars)
        .unwrap_or_default();
    let state = crate::ingest::task_state(&name, args.as_ref());
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
    Event {
        turn: None,
        agent: "opencode",
        session: r.session_id,
        ts: r.p_ts,
        kind: "tool",
        name,
        input,
        output,
        input_chars,
        output_chars,
        output_bytes,
        ok,
        call_id: r.call_id.unwrap_or(r.p_id),
        child_session: String::new(),
        meta: String::new(),
        state,
    }
}

/// Subagent sessions: a child session (parent_id set) becomes a subagent_start event
/// in the parent. The child is independently viewable, so just link it.
/// Returns false when a child row or query the reader could not decode leaves that link
/// unobserved, not absent; the subagent scope is then uncovered and cannot license
/// wholesale replacement.
fn push_subagent_starts(
    conn: &rusqlite::Connection,
    child_sql: &str,
    tally: &Tally,
    events: &mut Vec<Event>,
) -> bool {
    let mut stmt = match conn.prepare(child_sql) {
        Ok(stmt) => stmt,
        Err(e) => {
            tally.seen();
            tally.error(&format!("session query: {e}"));
            return false;
        }
    };
    let rows = match stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
        ))
    }) {
        Ok(rows) => rows,
        Err(e) => {
            tally.seen();
            tally.error(&format!("session rows: {e}"));
            return false;
        }
    };
    let mut covered = true;
    for row in rows {
        tally.seen();
        let (child, parent, title, ts) = match row {
            Ok(row) => row,
            Err(e) => {
                tally.error(&format!("session row: {e}"));
                covered = false;
                continue;
            }
        };
        tally.agent_row();
        tally.event();
        events.push(subagent_start(child, parent, &title, ts));
    }
    covered
}

fn subagent_start(child: String, parent: String, title: &str, ts: i64) -> Event {
    Event {
        turn: None,
        agent: "opencode",
        session: parent,
        ts,
        kind: "subagent_start",
        name: if title.is_empty() {
            "subagent".to_string()
        } else {
            cap_str(title, 200)
        },
        input: String::new(),
        output: String::new(),
        input_chars: 0,
        output_chars: 0,
        output_bytes: 0,
        ok: None,
        call_id: child.clone(),
        child_session: child,
        meta: String::new(),
        state: String::new(),
    }
}

/// Reports a database that exists but would not open, and classifies the failure.
fn open_failure(path: &Path, e: &rusqlite::Error, tally: &Tally) -> ReadOutcome {
    // the file exists (callers filter on that), so a failed open is real news
    crate::intake::diagnostic(format_args!(
        "  ! opencode: cannot open {}: {}",
        crate::ingest::terminal_safe(path.display()),
        crate::ingest::terminal_safe(e)
    ));
    tally.error(&format!("cannot open: {e}"));
    crate::ingest::note_read_failure(path, e.to_string());
    // A database that moved under its snapshot is read again by the next pass; a lock or
    // a journal may clear or persist, and a defect no retry heals is unreadable.
    if crate::ingest::sqlite_open_moved(e) {
        ReadOutcome::Moved
    } else if crate::ingest::sqlite_open_defect(path, e).is_some() {
        ReadOutcome::Invalid
    } else {
        ReadOutcome::Unavailable
    }
}

/// The (part, child-session) queries for this database's schema; None when the probe fails.
fn schema_queries(
    conn: &rusqlite::Connection,
    tally: &Tally,
) -> Option<(&'static str, &'static str)> {
    // opencode 2.x migrates in place and drops the v1 tables, so table presence is
    // the schema authority; an unreadable sqlite_master is a failed read, not v1.
    let v2 = match conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master \
         WHERE type='table' AND name='session_message')",
        [],
        |row| row.get::<_, i64>(0),
    ) {
        Ok(flag) => flag != 0,
        Err(e) => {
            tally.error(&format!("schema probe: {e}"));
            return None;
        }
    };
    Some(if v2 {
        (PART_SQL_V2, CHILD_SQL_V2)
    } else {
        (PART_SQL_V1, CHILD_SQL_V1)
    })
}

/// Pull messages and tool events from one opencode database in one scan.
fn collect_db(path: &Path) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    // seen = message-query part rows plus inspected child-session rows
    let tally = crate::intake::tally();
    let conn = match crate::ingest::open_sqlite_ro(path) {
        Ok(c) => c,
        Err(e) => return (Vec::new(), Vec::new(), open_failure(path, &e, &tally)),
    };
    let Some((part_sql, child_sql)) = schema_queries(&conn, &tally) else {
        return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
    };
    let mut stmt = match conn.prepare(part_sql) {
        Ok(s) => s,
        Err(e) => {
            tally.error(&format!("part query: {e}"));
            return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
        }
    };
    let rows = match stmt.query_map([], PartRow::read) {
        Ok(r) => r,
        Err(e) => {
            tally.error(&format!("part rows: {e}"));
            return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
        }
    };
    let mut scan = PartScan::new(&tally);
    for row in rows {
        scan.row(row);
    }
    let (out, tool_rows, main_scope_covered) = scan.finish();
    let mut events = tool_events(tool_rows, &tally);
    let child_scope_covered = push_subagent_starts(&conn, child_sql, &tally, &mut events);
    (
        out.into_iter().map(RawMessage::freeze).collect(),
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
pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    let dirs = data_dirs_at(&crate::ingest::home());
    let explicit = explicit_db_at(&dirs[0]);
    let mut messages = Vec::new();
    let mut events = Vec::new();
    let mut seen = Vec::new();
    for dir in dirs {
        let inside = explicit
            .as_deref()
            .filter(|path| relative_path(path, &dir).is_some());
        let mut files = databases_in(&dir, inside);
        if let Some(path) = inside {
            if !contains_path(&seen, path) {
                seen.push(path.to_path_buf());
                if explicit_db(path) {
                    files.push(path.to_path_buf());
                    files.sort();
                }
            }
        }
        for path in &files {
            if !contains_path(&seen, path) {
                seen.push(path.clone());
            }
        }
        let pass = crate::intake_batch::collect_cached_for(cache, "opencode", &files, collect_db);
        messages.extend(pass.messages);
        events.extend(pass.events);
    }
    if let Some(path) = explicit.filter(|path| !contains_path(&seen, path)) {
        let files = if explicit_db(&path) {
            std::slice::from_ref(&path)
        } else {
            &[]
        };
        let pass = crate::intake_batch::collect_cached_for(cache, "opencode", files, collect_db);
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
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn may_read_partially(&self) -> bool {
        true
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
    fn archive_content(&self, path: &std::path::Path) -> bool {
        self.store_content(path)
            || (path.extension().is_some_and(|e| e == "json")
                && path.components().any(|p| p.as_os_str() == "storage"))
    }
    fn freshness_content(&self, path: &std::path::Path) -> bool {
        self.store_content(path)
    }
}
