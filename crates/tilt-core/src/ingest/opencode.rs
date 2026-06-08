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
//! Danny's text = concat of part.data.text for type=="text" parts whose message
//! has role=="user". But opencode bundles non-Danny content into the SAME user
//! message as extra "text" parts: tool-call narration ("Called the <X> tool with
//! the following input: {...}"), tool results ("Image read successfully"), and
//! file attachments (`<path>...</path><type>file</type><content>...`). 106 user
//! messages mix these with a genuine typed part, so filtering is done per PART
//! (drop injected wrappers, then concatenate survivors) — never per message.
//! We under-include rather than risk labeling agent/system text as Danny's.

use rayon::prelude::*;
use rusqlite::{Connection, OpenFlags};
use serde::Deserialize;

use crate::ingest::{is_wrapper, project_name};
use crate::model::Message;

/// The four live DB filenames. `.bak*`/`.corrupted` siblings are intentionally absent.
const DB_FILES: &[&str] = &[
    "opencode.db",
    "opencode-dev.db",
    "opencode-local.db",
    "opencode-dev-before-copy.db",
];

#[derive(Deserialize)]
struct MsgData {
    role: Option<String>,
}

#[derive(Deserialize)]
struct PartData {
    #[serde(rename = "type")]
    ty: Option<String>,
    text: Option<String>,
}

/// True for opencode-injected "text" parts that are NOT something Danny typed:
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

/// Pull Danny's messages out of one opencode DB. Returns empty on any failure.
fn collect_db(path: &std::path::Path) -> Vec<Message> {
    let path_str = match path.to_str() {
        Some(s) => s,
        None => return Vec::new(),
    };
    // URI form so we can request read-only + immutable (won't touch a live WAL).
    // Forward slashes work for SQLite URIs on Windows too.
    let uri = format!("file:{}?mode=ro&immutable=1", path_str.replace('\\', "/"));
    let conn = match Connection::open_with_flags(
        &uri,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    ) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };

    // One pass over user-message text parts, joined to the session directory.
    // Order by (session, message time, part time, part id) so each session's
    // turns and each message's parts assemble deterministically.
    let mut stmt = match conn.prepare(
        "SELECT m.session_id, s.directory, m.time_created, m.id, m.data, p.data \
         FROM part p \
         JOIN message m ON p.message_id = m.id \
         JOIN session s ON m.session_id = s.id \
         ORDER BY m.session_id, m.time_created, m.id, p.time_created, p.id",
    ) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };

    let rows = match stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0).unwrap_or_default(), // session_id
            row.get::<_, String>(1).unwrap_or_default(), // directory
            row.get::<_, i64>(2).unwrap_or(0),           // message time_created (ms)
            row.get::<_, String>(3).unwrap_or_default(), // message id
            row.get::<_, String>(4).unwrap_or_default(), // message.data JSON
            row.get::<_, String>(5).unwrap_or_default(), // part.data JSON
        ))
    }) {
        Ok(r) => r,
        Err(_) => return Vec::new(),
    };

    let mut out: Vec<Message> = Vec::new();
    // Per-session running turn index (rows are session-ordered).
    let mut cur_session = String::new();
    let mut turn = 0u32;
    // Accumulator for the current message's surviving Danny-authored text parts.
    let mut cur_msg_id = String::new();
    let mut cur_dir = String::new();
    let mut cur_ts: i64 = 0;
    let mut cur_text = String::new();
    let mut have_msg = false;

    // Flush the accumulated message into `out` if it carries real text.
    let flush = |out: &mut Vec<Message>,
                 turn: &mut u32,
                 session: &str,
                 dir: &str,
                 ts: i64,
                 text: &str| {
        if text.trim().is_empty() || is_wrapper(text) {
            return;
        }
        out.push(Message {
            agent: "opencode",
            project: project_name(dir),
            session: session.to_string(),
            ts,
            turn: *turn,
            text: text.to_string(),
        });
        *turn += 1;
    };

    for row in rows.flatten() {
        let (session_id, directory, m_ts, m_id, m_data, p_data) = row;

        // Only user-role messages.
        let role = serde_json::from_str::<MsgData>(&m_data)
            .ok()
            .and_then(|d| d.role);
        if role.as_deref() != Some("user") {
            continue;
        }
        // Only text parts with non-empty text.
        let part = match serde_json::from_str::<PartData>(&p_data) {
            Ok(p) => p,
            Err(_) => continue,
        };
        if part.ty.as_deref() != Some("text") {
            continue;
        }
        let ptext = match part.text {
            Some(t) if !t.trim().is_empty() => t,
            _ => continue,
        };
        // Drop opencode-injected wrappers at the PART level (key correctness step).
        if is_injected_part(&ptext) {
            continue;
        }

        // New session => reset turn counter (flush any pending message first).
        if session_id != cur_session {
            if have_msg {
                flush(&mut out, &mut turn, &cur_session, &cur_dir, cur_ts, &cur_text);
            }
            cur_session = session_id.clone();
            turn = 0;
            have_msg = false;
            cur_text.clear();
        }

        // New message within the session => flush the previous one.
        if !have_msg || m_id != cur_msg_id {
            if have_msg {
                flush(&mut out, &mut turn, &cur_session, &cur_dir, cur_ts, &cur_text);
            }
            cur_msg_id = m_id;
            cur_dir = directory;
            cur_ts = m_ts;
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
        flush(&mut out, &mut turn, &cur_session, &cur_dir, cur_ts, &cur_text);
    }

    out
}

/// Open each live opencode DB read-only and collect Danny's typed messages.
pub fn collect() -> Vec<Message> {
    let dir = crate::ingest::home()
        .join(".local")
        .join("share")
        .join("opencode");

    DB_FILES
        .par_iter()
        .map(|name| dir.join(name))
        .filter(|p| p.exists())
        .flat_map(|p| collect_db(&p))
        .collect()
}
