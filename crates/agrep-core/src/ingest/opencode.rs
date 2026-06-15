//! opencode adapter: ~/.local/share/opencode/*.db (per-build SQLite chat stores).
//! Four live DBs (opencode.db, opencode-dev.db, opencode-local.db,
//! opencode-dev-before-copy.db); *.bak*/*.corrupted are ignored. Each is opened
//! READ-ONLY + immutable (URI `mode=ro&immutable=1`) so a running opencode isn't
//! disturbed, and a DB that fails to open is skipped (never panics).
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

use rusqlite::{Connection, OpenFlags};

use crate::ingest::{cap_str, is_wrapper, project_name, summarize_tool_input, EVENT_CAP};
use crate::model::{Event, Message};

/// The four live DB filenames. `.bak*`/`.corrupted` siblings are intentionally absent.
const DB_FILES: &[&str] = &[
    "opencode.db",
    "opencode-dev.db",
    "opencode-local.db",
    "opencode-dev-before-copy.db",
];

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

/// Pull the user's messages AND tool events out of one opencode DB in a single pass over
/// the `part` table. Returns empty on any failure.
///
/// The part table is where the whole history lives (~1.6GB across the live DBs), and the
/// old shape scanned it TWICE (messages, then events with a LIKE) while shipping every
/// part's full JSON - megabyte tool outputs included - into Rust strings, re-parsing the
/// message envelope once per part. Now SQLite does the filtering and field extraction in
/// C (`json_extract`), the output is capped with `substr` before it crosses the FFI, and
/// both consumers share one scan. Rust never sees a payload it won't keep.
///
/// KNOWN (accepted) divergence from the old Rust-side unescaping: an output containing an
/// embedded NUL byte (UTF-16 dumps, decoded binaries) truncates at the NUL in SQLite's
/// text path. The field is an 800-char display PREVIEW and everything past a NUL is
/// binary junk; the full payload stays in the source store.
fn collect_db(path: &std::path::Path) -> (Vec<Message>, Vec<Event>) {
    let path_str = match path.to_str() {
        Some(s) => s,
        None => return (Vec::new(), Vec::new()),
    };
    // URI form so we can request read-only + immutable (won't touch a live WAL).
    // Forward slashes work for SQLite URIs on Windows too.
    let uri = format!("file:{}?mode=ro&immutable=1", path_str.replace('\\', "/"));
    let conn = match Connection::open_with_flags(
        &uri,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    ) {
        Ok(c) => c,
        Err(e) => {
            // the file exists (callers filter on that), so a failed open is real news
            eprintln!("  ! opencode: cannot open {}: {e}", path.display());
            return (Vec::new(), Vec::new());
        }
    };

    // One pass over text + tool parts. The LIKE pair is a cheap byte-scan prefilter (it
    // can false-positive on payload contents); `json_extract` is the real type check.
    // LEFT JOIN session: messages need s.directory (NULL drops them, matching the old
    // INNER JOIN), but tool events never required a session row.
    // Order by (session, message time, part time, part id) so each session's turns and
    // each message's parts assemble deterministically; events re-sort below.
    let mut stmt = match conn.prepare(
        "SELECT m.session_id, s.directory, m.time_created, m.id, \
                json_extract(m.data,'$.role'), json_extract(m.data,'$.modelID'), \
                json_extract(p.data,'$.type'), \
                json_extract(p.data,'$.text'), \
                json_extract(p.data,'$.tool'), json_extract(p.data,'$.callID'), \
                json_extract(p.data,'$.state.status'), json_extract(p.data,'$.state.input'), \
                substr(coalesce(json_extract(p.data,'$.state.output'),''),1,4000), \
                p.time_created, p.id, s.id IS NOT NULL \
         FROM part p \
         JOIN message m ON p.message_id = m.id \
         LEFT JOIN session s ON m.session_id = s.id \
         WHERE (p.data LIKE '%\"type\":\"text\"%' OR p.data LIKE '%\"type\":\"tool\"%') \
           AND json_extract(p.data,'$.type') IN ('text','tool') \
         ORDER BY m.session_id, m.time_created, m.id, p.time_created, p.id",
    ) {
        Ok(s) => s,
        Err(_) => return (Vec::new(), Vec::new()),
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
        p_ts: i64,
        p_id: String,
        /// whether a session row exists at all (LEFT JOIN; the old message query was an
        /// INNER JOIN, so text parts without one are dropped to match)
        has_session: bool,
    }
    let rows = match stmt.query_map([], |row| {
        Ok(Row {
            session_id: row.get::<_, String>(0).unwrap_or_default(),
            directory: row.get::<_, Option<String>>(1).unwrap_or(None),
            m_ts: row.get::<_, i64>(2).unwrap_or(0),
            m_id: row.get::<_, String>(3).unwrap_or_default(),
            role: row.get::<_, Option<String>>(4).unwrap_or(None),
            model: row.get::<_, Option<String>>(5).unwrap_or(None),
            p_ty: row.get::<_, String>(6).unwrap_or_default(),
            p_text: row.get::<_, Option<String>>(7).unwrap_or(None),
            tool: row.get::<_, Option<String>>(8).unwrap_or(None),
            call_id: row.get::<_, Option<String>>(9).unwrap_or(None),
            status: row.get::<_, Option<String>>(10).unwrap_or(None),
            input: row.get::<_, Option<String>>(11).unwrap_or(None),
            output: row.get::<_, String>(12).unwrap_or_default(),
            p_ts: row.get::<_, i64>(13).unwrap_or(0),
            p_id: row.get::<_, String>(14).unwrap_or_default(),
            has_session: row.get::<_, i64>(15).unwrap_or(0) != 0,
        })
    }) {
        Ok(r) => r,
        Err(_) => return (Vec::new(), Vec::new()),
    };

    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    // Per-session running turn index (rows are session-ordered).
    let mut cur_session = String::new();
    let mut turn = 0u32;
    // Accumulator for the current message's surviving text parts (user OR assistant).
    let mut cur_msg_id = String::new();
    let mut cur_dir = String::new();
    let mut cur_ts: i64 = 0;
    let mut cur_text = String::new();
    let mut cur_role = String::new();
    let mut cur_model = String::new();
    let mut have_msg = false;

    // Flush the accumulated message: a user turn becomes a new Message; an assistant
    // turn is attached as the reply (+model) of the user turn it answers.
    fn flush(
        out: &mut Vec<crate::model::RawMessage>,
        turn: &mut u32,
        session: &str,
        dir: &str,
        ts: i64,
        role: &str,
        model: &str,
        text: &str,
    ) {
        if role == "user" {
            if text.trim().is_empty() || is_wrapper(text) {
                return;
            }
            out.push(crate::model::RawMessage {
                agent: "opencode",
                project: project_name(dir),
                session: session.to_string(),
                ts,
                turn: *turn,
                text: text.to_string(),
                model: String::new(),
                reply: String::new(),
            });
            *turn += 1;
        } else if role == "assistant" {
            if let Some(last) = out.last_mut() {
                crate::ingest::append_capped(&mut last.reply, text, 1600);
                if last.model.is_empty() && !model.is_empty() {
                    last.model = model.to_string();
                }
            }
        }
    }

    // Tool rows are buffered, then sorted by (session, part time, part id) - the exact
    // ORDER BY the old dedicated events query used - before becoming Events.
    let mut tool_rows: Vec<Row> = Vec::new();

    for r in rows.flatten() {
        if r.p_ty == "tool" {
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
            has_session,
            ..
        } = r;
        // The old message query INNER JOINed session; drop text parts without one.
        if !has_session {
            continue;
        }
        let directory = directory.unwrap_or_default();
        let role = role.unwrap_or_default();
        let model = model.unwrap_or_default();
        // We only care about the human and the assistant; skip system/other.
        if role != "user" && role != "assistant" {
            continue;
        }
        let ptext = match p_text {
            Some(t) if !t.trim().is_empty() => t,
            _ => continue,
        };
        // Drop opencode-injected wrappers at the PART level - user turns only (the
        // assistant's prose is what we want to keep verbatim as the reply).
        if role == "user" && is_injected_part(&ptext) {
            continue;
        }

        // New session => reset turn counter (flush any pending message first).
        if session_id != cur_session {
            if have_msg {
                flush(
                    &mut out,
                    &mut turn,
                    &cur_session,
                    &cur_dir,
                    cur_ts,
                    &cur_role,
                    &cur_model,
                    &cur_text,
                );
            }
            cur_session = session_id.clone();
            turn = 0;
            have_msg = false;
            cur_text.clear();
        }

        // New message within the session => flush the previous one.
        if !have_msg || m_id != cur_msg_id {
            if have_msg {
                flush(
                    &mut out,
                    &mut turn,
                    &cur_session,
                    &cur_dir,
                    cur_ts,
                    &cur_role,
                    &cur_model,
                    &cur_text,
                );
            }
            cur_msg_id = m_id;
            cur_dir = directory;
            cur_ts = m_ts;
            cur_role = role;
            cur_model = model;
            cur_text.clear();
            have_msg = true;
        }

        if !cur_text.is_empty() {
            cur_text.push('\n');
        }
        cur_text.push_str(&ptext);
    }
    // Final pending message.
    if have_msg {
        flush(
            &mut out,
            &mut turn,
            &cur_session,
            &cur_dir,
            cur_ts,
            &cur_role,
            &cur_model,
            &cur_text,
        );
    }

    // Tool parts -> events, in the old dedicated query's order.
    tool_rows.sort_by(|a, b| {
        a.session_id
            .cmp(&b.session_id)
            .then(a.p_ts.cmp(&b.p_ts))
            .then(a.p_id.cmp(&b.p_id))
    });
    let mut events: Vec<Event> = Vec::with_capacity(tool_rows.len());
    for r in tool_rows {
        let input = r
            .input
            .as_deref()
            .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok())
            .map(|v| summarize_tool_input(&v))
            .unwrap_or_default();
        let ok = match r.status.as_deref() {
            Some("completed") => Some(true),
            Some("error") => Some(false),
            _ => None,
        };
        events.push(Event {
            agent: "opencode",
            session: r.session_id,
            ts: r.p_ts,
            kind: "tool",
            name: r.tool.unwrap_or_else(|| "?".to_string()),
            input,
            output: cap_str(&r.output, EVENT_CAP),
            ok,
            call_id: r.call_id.unwrap_or(r.p_id),
            child_session: String::new(),
        });
    }

    // Subagent sessions: a child session (parent_id set) becomes a subagent_start event
    // in the parent. The child is independently viewable, so just link it.
    if let Ok(mut stmt) = conn.prepare(
        "SELECT id, parent_id, COALESCE(title,''), time_created \
         FROM session WHERE parent_id IS NOT NULL AND parent_id != ''",
    ) {
        let srows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0).unwrap_or_default(),
                row.get::<_, String>(1).unwrap_or_default(),
                row.get::<_, String>(2).unwrap_or_default(),
                row.get::<_, i64>(3).unwrap_or(0),
            ))
        });
        if let Ok(srows) = srows {
            for (child, parent, title, ts) in srows.flatten() {
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
                    ok: None,
                    call_id: child.clone(),
                    child_session: child,
                });
            }
        }
    }

    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        events,
    )
}

/// Open each live opencode DB read-only and collect the user's typed messages + tool events.
/// Cache-driven like claude/codex: a DB whose (mtime, size) hasn't moved is served from the
/// parse cache instead of a full table scan. The immutable=1 open means this reader only
/// ever sees checkpointed content, and checkpoints touch the main DB file's mtime, so the
/// stat is a sound staleness key for exactly what collect_db can observe.
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let dir = crate::ingest::home()
        .join(".local")
        .join("share")
        .join("opencode");

    let paths: Vec<std::path::PathBuf> = DB_FILES
        .iter()
        .map(|name| dir.join(name))
        .filter(|p| p.exists())
        .collect();

    let pass = crate::ingest_cache::collect_cached(cache, &dir, &paths, collect_db);
    (pass.messages, pass.events)
}
