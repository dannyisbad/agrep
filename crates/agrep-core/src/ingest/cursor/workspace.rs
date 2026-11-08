//! Project attribution: which workspace folder each conversation belongs to.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use rusqlite::Connection;

use super::{has_cursor_table, kv_text_checked};
use crate::ingest::project_name;
use crate::ingest::registry::read_bounded_regular_file;

const WORKSPACE_JSON_MAX_BYTES: u64 = 4 * 1024 * 1024;

fn has_item_table(conn: &Connection) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master \
         WHERE type = 'table' AND name = 'ItemTable' COLLATE NOCASE)",
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

/// Conversations no readable workspace source maps, in key order.
fn unmapped_conversations(
    conn: &Connection,
    projects: &HashMap<String, String>,
) -> rusqlite::Result<Vec<String>> {
    if !has_cursor_table(conn)? {
        return Ok(Vec::new());
    }
    let mut statement = conn.prepare(
        "SELECT key FROM cursorDiskKV
         WHERE key >= 'composerData:' AND key < 'composerData;'
         AND typeof(value) IN ('text', 'blob') ORDER BY key",
    )?;
    let rows = statement.query_map([], |row| row.get::<_, String>(0))?;
    let mut unmapped = Vec::new();
    for key in rows {
        let key = key?;
        if let Some(session) = key.strip_prefix("composerData:") {
            if !projects.contains_key(session) {
                unmapped.push(session.to_string());
            }
        }
    }
    Ok(unmapped)
}

/// Project attribution of every conversation a readable workspace source maps. While a
/// workspace source cannot be read, the conversations nothing maps keep the project the last
/// generation published, or carry the "cursor" fallback when none did: attribution is part of
/// the token, so repair relabels only what changes, in one reparse.
pub(super) struct Attribution {
    pub(super) projects: HashMap<String, String>,
    /// The first unreadable workspace source, with the conversations it leaves unattributed.
    pub(super) incomplete: Option<AttributionGap>,
}

#[derive(Clone, Debug)]
pub(super) struct AttributionGap {
    pub(super) path: PathBuf,
    pub(super) reason: String,
    /// Conversations under the "cursor" fallback until the source is readable.
    pub(super) unmapped: Vec<String>,
    /// Conversations keeping their published project until the source is readable.
    pub(super) retained: Vec<String>,
}

impl Attribution {
    fn complete(projects: HashMap<String, String>) -> Self {
        Self {
            projects,
            incomplete: None,
        }
    }

    /// Keep the project `published` (the last-good rows) gave each unmapped conversation.
    pub(super) fn retain_published(&mut self, published: &HashMap<String, String>) {
        let Some(gap) = self.incomplete.as_mut() else {
            return;
        };
        let mut unmapped = Vec::with_capacity(gap.unmapped.len());
        for session in gap.unmapped.drain(..) {
            match published.get(&session) {
                Some(project) => {
                    self.projects.insert(session.clone(), project.clone());
                    gap.retained.push(session);
                }
                None => unmapped.push(session),
            }
        }
        gap.unmapped = unmapped;
    }

    /// Every conversation whose attribution waits on the unreadable source, in key order.
    pub(super) fn pending(&self) -> Vec<String> {
        let Some(gap) = self.incomplete.as_ref() else {
            return Vec::new();
        };
        let mut pending: Vec<String> = gap.unmapped.iter().chain(&gap.retained).cloned().collect();
        pending.sort();
        pending
    }
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

/// What the workspace sources mapped so far.
#[derive(Default)]
struct WorkspaceScan {
    /// composer id -> project
    projects: HashMap<String, String>,
    /// workspace id -> project
    projects_by_workspace: HashMap<String, String>,
    /// The first workspace source that could not be read, and why.
    gap: Option<(PathBuf, String)>,
}

impl WorkspaceScan {
    /// Warns about a source that could not be read; the first one becomes the gap.
    fn unreadable(&mut self, path: &Path, action: &str, error: impl std::fmt::Display) {
        crate::ingest::warn_source_skip("cursor", path, &error);
        self.gap.get_or_insert_with(|| {
            (
                path.to_path_buf(),
                format!("cannot {action} {}: {error}", path.display()),
            )
        });
    }

    /// The workspaceStorage entries; an unlistable directory lists none.
    fn workspace_dirs(&mut self, root: &Path) -> Vec<PathBuf> {
        let listed = match std::fs::read_dir(root) {
            Ok(rd) => rd
                .map(|entry| entry.map(|entry| entry.path()))
                .collect::<Result<Vec<_>, _>>(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Vec::new(),
            Err(error) => Err(error),
        };
        listed.unwrap_or_else(|error| {
            self.unreadable(root, "enumerate", error);
            Vec::new()
        })
    }

    fn workspace(&mut self, dir: &Path) {
        if let Some(project) = self.workspace_project(dir) {
            self.legacy_composers(dir, &project);
        }
    }

    /// The project workspace.json's folder names, registered under the workspace id.
    fn workspace_project(&mut self, dir: &Path) -> Option<String> {
        let workspace = dir.join("workspace.json");
        let ws = match read_bounded_regular_file(&workspace, WORKSPACE_JSON_MAX_BYTES) {
            Ok(Some(ws)) => ws,
            Ok(None) => return None,
            Err(error) => {
                self.unreadable(&workspace, "read", error);
                return None;
            }
        };
        let ws: serde_json::Value = match serde_json::from_slice(&ws) {
            Ok(value) => value,
            Err(error) => {
                self.unreadable(&workspace, "decode", error);
                return None;
            }
        };
        let folder = ws.get("folder").and_then(|f| f.as_str())?;
        let path = percent_decode(folder.trim_start_matches("file://").trim_start_matches('/'));
        let project = project_name(&path);
        if let Some(workspace_id) = dir.file_name().and_then(|name| name.to_str()) {
            self.projects_by_workspace
                .insert(workspace_id.to_string(), project.clone());
        }
        Some(project)
    }

    /// Attributes the composers the workspace database's older allComposers row lists.
    fn legacy_composers(&mut self, dir: &Path, project: &str) {
        let db = dir.join("state.vscdb");
        match std::fs::metadata(&db) {
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(error) => return self.unreadable(&db, "inspect", error),
        }
        let conn = match crate::ingest::open_sqlite_ro(&db) {
            Ok(c) => c,
            Err(error) => return self.unreadable(&db, "open", error),
        };
        match has_item_table(&conn) {
            Ok(true) => {}
            Ok(false) => return,
            Err(error) => return self.unreadable(&db, "inspect", error),
        }
        let v = match kv_text_checked(&conn, "ItemTable", "composer.composerData") {
            Ok(Some(value)) => value,
            Ok(None) => return,
            Err(error) => return self.unreadable(&db, "read", error),
        };
        let v: serde_json::Value = match serde_json::from_str(&v) {
            Ok(value) => value,
            Err(error) => return self.unreadable(&db, "decode", error),
        };
        for c in v
            .get("allComposers")
            .and_then(|a| a.as_array())
            .into_iter()
            .flatten()
        {
            if let Some(id) = c.get("composerId").and_then(|i| i.as_str()) {
                self.projects.insert(id.to_string(), project.to_string());
            }
        }
    }

    /// Global composerHeaders rows win over the workspace rows they overlay.
    fn overlay_headers(&mut self, conn: &Connection, user: &Path) {
        if let Err(error) =
            overlay_composer_header_projects(conn, &self.projects_by_workspace, &mut self.projects)
        {
            let db = user.join("globalStorage").join("state.vscdb");
            self.unreadable(&db, "read", error);
        }
    }
}

/// Composer attribution is part of the session token, so a workspace move relabels cached rows.
fn workspace_projects(conn: &Connection, user: &Path) -> anyhow::Result<Attribution> {
    let mut scan = WorkspaceScan::default();
    let mut dirs = scan.workspace_dirs(&user.join("workspaceStorage"));
    dirs.sort();
    for dir in dirs {
        scan.workspace(&dir);
    }
    scan.overlay_headers(conn, user);
    let incomplete = match scan.gap {
        None => None,
        Some((path, reason)) => {
            let unmapped = unmapped_conversations(conn, &scan.projects)?;
            (!unmapped.is_empty()).then_some(AttributionGap {
                path,
                reason,
                unmapped,
                retained: Vec::new(),
            })
        }
    };
    Ok(Attribution {
        projects: scan.projects,
        incomplete,
    })
}

pub(super) fn workspace_projects_if_conversations(
    conn: &Connection,
    user: &Path,
) -> anyhow::Result<Attribution> {
    if !has_conversations(conn)? {
        return Ok(Attribution::complete(HashMap::new()));
    }
    workspace_projects(conn, user)
}
