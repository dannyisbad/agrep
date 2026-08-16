//! Shared harness for the ingest integration suites (golden.rs, torture.rs): fixture
//! discovery, scrubbed-env subprocess ingest, output normalization, and the intake
//! accounting identity. Each test binary uses a subset, hence the dead_code allowance.
#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

use rusqlite::{Connection, OptionalExtension};

pub const BIN: &str = env!("CARGO_BIN_EXE_agrep-rs");
static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

pub fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
}

pub fn temp_dir(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let p = std::env::temp_dir().join(format!(
        "agrep-golden-{tag}-{}-{}-{}",
        std::process::id(),
        nanos,
        TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed),
    ));
    fs::create_dir_all(&p).unwrap();
    p
}

/// Lines of a file, sorted (missing file -> empty). Sorting makes the golden immune to the
/// parallel ingest's run-to-run row order.
pub fn sorted_lines(path: &Path) -> Vec<String> {
    let mut v: Vec<String> = fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter(|l| !l.is_empty())
        .map(str::to_string)
        .collect();
    v.sort();
    v
}

pub fn event_rows(data: &Path) -> Vec<(String, Vec<u8>)> {
    let events = data.join("events");
    let store = events.join(agrep_core::cache::EVENT_STORE_NAME);
    if let Ok(connection) = Connection::open_with_flags(
        &store,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        if let Ok(mut statement) =
            connection.prepare("SELECT name, payload FROM event_sessions ORDER BY name")
        {
            if let Ok(rows) = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?))) {
                if let Ok(rows) = rows.collect::<rusqlite::Result<Vec<_>>>() {
                    return rows;
                }
            }
        }
    }
    let mut rows: Vec<_> = fs::read_dir(events)
        .ok()
        .into_iter()
        .flatten()
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("jsonl"))
        .filter_map(|path| {
            let name = path.file_name()?.to_string_lossy().into_owned();
            fs::read(path).ok().map(|body| (name, body))
        })
        .collect();
    rows.sort_by(|left, right| left.0.cmp(&right.0));
    rows
}

pub fn event_row(data: &Path, name: &str) -> Option<Vec<u8>> {
    let connection = Connection::open(
        data.join("events")
            .join(agrep_core::cache::EVENT_STORE_NAME),
    )
    .ok()?;
    connection
        .query_row(
            "SELECT payload FROM event_sessions WHERE name=?1",
            [name],
            |row| row.get(0),
        )
        .optional()
        .ok()?
}

pub fn delete_event_row(data: &Path, name: &str) {
    Connection::open(
        data.join("events")
            .join(agrep_core::cache::EVENT_STORE_NAME),
    )
    .unwrap()
    .execute("DELETE FROM event_sessions WHERE name=?1", [name])
    .unwrap();
}

/// One deterministic blob: messages/replies/sessions and every DB-backed event row.
pub fn normalize(data: &Path) -> String {
    let mut out = String::new();
    for name in ["messages.jsonl", "replies.jsonl", "sessions.jsonl"] {
        let lines = sorted_lines(&data.join(name));
        out.push_str(&format!("=== {name} ({} lines) ===\n", lines.len()));
        for l in &lines {
            out.push_str(l);
            out.push('\n');
        }
    }
    for (fname, body) in event_rows(data) {
        let mut lines: Vec<_> = String::from_utf8_lossy(&body)
            .lines()
            .filter(|line| !line.is_empty())
            .map(str::to_string)
            .collect();
        lines.sort();
        out.push_str(&format!("=== events/{fname} ({} lines) ===\n", lines.len()));
        for l in &lines {
            out.push_str(l);
            out.push('\n');
        }
    }
    out
}

/// Run `agrep-rs index --agent <adapter>` against a fixture home into `data`, scrubbed env.
/// `full` forces a cold cache (--full); otherwise the cache in `data` is used (warm).
pub fn ingest_output(agent: &str, home: &Path, data: &Path, full: bool) -> std::process::Output {
    let mut cmd = Command::new(BIN);
    cmd.args(["index", "--agent", agent]);
    if full {
        cmd.arg("--full");
    }
    cmd.env("AGREP_HOME", home);
    cmd.env("AGREP_DATA_DIR", data);
    // strip anything that would point discovery or the binary at a real install
    for k in [
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "CLINE_DIR",
        "XDG_CONFIG_HOME",
        "AGREP_RS_BIN",
    ] {
        cmd.env_remove(k);
    }
    cmd.output().expect("spawn agrep-rs")
}

/// Cold-streaming lane used by the first no-index search. Keep this a real subprocess so the
/// handoff artifacts are tested exactly as the Python CLI observes them.
pub fn ingest_emit_output(agent: &str, home: &Path, data: &Path) -> std::process::Output {
    let mut cmd = Command::new(BIN);
    cmd.args(["index", "--agent", agent, "--emit-rows"]);
    cmd.env("AGREP_HOME", home);
    cmd.env("AGREP_DATA_DIR", data);
    for k in [
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "CLINE_DIR",
        "XDG_CONFIG_HOME",
        "AGREP_RS_BIN",
    ] {
        cmd.env_remove(k);
    }
    cmd.output().expect("spawn agrep-rs --emit-rows")
}

pub fn ingest_into(agent: &str, home: &Path, data: &Path, full: bool) {
    let out = ingest_output(agent, home, data, full);
    assert!(
        out.status.success(),
        "ingest failed for {agent}:\n{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

/// The golden path: a fresh cold ingest into a throwaway data dir, normalized.
/// Every instrumented file must also satisfy the intake accounting identity.
pub fn run_ingest(agent: &str, home: &Path) -> String {
    let data = temp_dir(agent);
    ingest_into(agent, home, &data, true);
    check_intake_identity(&data);
    let blob = normalize(&data);
    let _ = fs::remove_dir_all(&data);
    blob
}

/// Intake trust, enforced at the fixture level: for every file the ingest tallied,
/// `seen == rows + agent_rows + Σskips + errors` - a parser may skip for a NAMED
/// reason or fail loudly, but it may not lose a record silently. Adapters not yet
/// instrumented simply have no entries; coverage tightens as they land.
pub fn check_intake_identity(data: &Path) {
    let path = data.join("intake_stats.json");
    let Ok(body) = fs::read_to_string(&path) else {
        return;
    };
    let book: serde_json::Value = serde_json::from_str(&body).expect("intake_stats parses");
    let Some(files) = book.get("files").and_then(|f| f.as_object()) else {
        return;
    };
    for (id, e) in files {
        let n = |k: &str| e.get(k).and_then(|v| v.as_u64()).unwrap_or(0);
        let skips: u64 = e
            .get("skips")
            .and_then(|s| s.as_object())
            .map(|s| s.values().filter_map(|v| v.as_u64()).sum())
            .unwrap_or(0);
        assert_eq!(
            n("seen"),
            n("rows") + n("agent_rows") + skips + n("errors"),
            "intake identity broken for {id}: {e}"
        );
    }
}

pub fn fixture_home(agent: &str) -> PathBuf {
    fixtures_dir().join(agent).join("home")
}

/// opencode's store is a sqlite db; build it from the auditable seed.sql into a fresh temp
/// home (never committed as a binary, never mutating the repo).
pub fn opencode_home() -> PathBuf {
    opencode_home_from("opencode")
}

/// Same store location, opencode 2.x schema (session_v2/session_message).
pub fn opencode_v2_home() -> PathBuf {
    opencode_home_from("opencode_v2")
}

fn opencode_home_from(fixture: &str) -> PathBuf {
    let home = temp_dir("opencode-home");
    let ocdir = home.join(".local").join("share").join("opencode");
    fs::create_dir_all(&ocdir).unwrap();
    let seed = fs::read_to_string(fixtures_dir().join(fixture).join("seed.sql")).unwrap();
    let conn = rusqlite::Connection::open(ocdir.join("opencode.db")).unwrap();
    conn.execute_batch(&seed).unwrap();
    conn.close().unwrap();
    home
}

/// crush's store is a sqlite db built from the auditable seed.sql into a fresh temp home.
pub fn crush_home() -> PathBuf {
    let home = temp_dir("crush-home");
    let dir = home.join(".local").join("share").join("crush");
    fs::create_dir_all(&dir).unwrap();
    let seed = fs::read_to_string(fixtures_dir().join("crush").join("seed.sql")).unwrap();
    let conn = rusqlite::Connection::open(dir.join("crush.db")).unwrap();
    conn.execute_batch(&seed).unwrap();
    conn.close().unwrap();
    home
}

/// cursor's store: a globalStorage state.vscdb plus one workspaceStorage entry (the db that
/// maps composers to a folder), both from auditable seed sql. Uses the `.config` candidate
/// path, valid under AGREP_HOME on every OS.
pub fn cursor_home() -> PathBuf {
    let home = temp_dir("cursor-home");
    let user = home.join(".config").join("Cursor").join("User");
    fs::create_dir_all(user.join("globalStorage")).unwrap();
    let seed = fs::read_to_string(fixtures_dir().join("cursor").join("global.sql")).unwrap();
    let conn = rusqlite::Connection::open(user.join("globalStorage").join("state.vscdb")).unwrap();
    conn.execute_batch(&seed).unwrap();
    conn.close().unwrap();

    let ws = user
        .join("workspaceStorage")
        .join("aaaa0001bbbb0002cccc0003dddd0004");
    fs::create_dir_all(&ws).unwrap();
    fs::write(
        ws.join("workspace.json"),
        r#"{"folder":"file:///c%3A/Users/tester/Desktop/flaky-app"}"#,
    )
    .unwrap();
    let seed = fs::read_to_string(fixtures_dir().join("cursor").join("workspace.sql")).unwrap();
    let conn = rusqlite::Connection::open(ws.join("state.vscdb")).unwrap();
    conn.execute_batch(&seed).unwrap();
    conn.close().unwrap();
    home
}

/// Recursive fixture-home copy, so torture cases can plant hostile files without
/// touching the checked-in fixtures.
pub fn copy_dir(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for entry in fs::read_dir(src).unwrap().flatten() {
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if from.is_dir() {
            copy_dir(&from, &to);
        } else {
            fs::copy(&from, &to).unwrap();
        }
    }
}
