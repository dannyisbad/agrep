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

mod discovery;
mod session;
mod tokens;
mod workspace;

use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use rusqlite::Connection;

use crate::ingest::registry::ATTRIBUTION_ISSUE_KIND;
use crate::intake_batch::{IntakeBatch, ReadOutcome, SourceReadIssue};
use crate::model::{Event, Message};
use discovery::{user_dir, user_dir_candidates, user_dir_checked, UserDirError};
use session::parse_session;
use tokens::{
    freshness_tokens_at, intake_tokens_at, record_census, stable_token_snapshot, TokenSnapshot,
    SCHEMA_ABSENT_SESSION,
};
use workspace::{workspace_projects_if_conversations, Attribution};

/// Why Cursor's database did not open: the issue kind of a defect no retry heals, or none for a
/// failure a retry may clear, whether the database moved under its snapshot, and the error.
struct OpenFailure {
    defect: Option<&'static str>,
    moved: bool,
    reason: String,
}

/// Open Cursor without write authority; failures retain the last-good token generation.
fn open_ro(path: &Path) -> Result<crate::ingest::ReadOnlyConnection, OpenFailure> {
    crate::ingest::open_sqlite_ro(path).map_err(|e| {
        crate::intake::diagnostic(format_args!(
            "  ! cursor: cannot open {}: {}",
            crate::ingest::terminal_safe(path.display()),
            crate::ingest::terminal_safe(&e)
        ));
        OpenFailure {
            defect: crate::ingest::sqlite_open_defect(path, &e),
            moved: crate::ingest::sqlite_open_moved(&e),
            reason: e.to_string(),
        }
    })
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

fn has_cursor_table(conn: &Connection) -> rusqlite::Result<bool> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master \
         WHERE type = 'table' AND name = 'cursorDiskKV' COLLATE NOCASE)",
        [],
        |row| row.get(0),
    )
}

/// Open the store read-only and collect messages + tool events, reparsing only sessions
/// whose token moved. A store that cannot open serves the cached conversations.
pub fn collect(cache: &mut IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    collect_discovered(cache, user_dir_checked())
}

fn record_discovery_failure(cache: &mut IntakeBatch, error: &UserDirError) {
    crate::intake::diagnostic(format_args!(
        "  ! cursor: source discovery failed: {}",
        crate::ingest::terminal_safe(error)
    ));
    cache.record_source_read_issue(
        "cursor",
        &error.path,
        "source-stat-failed",
        error.to_string(),
    );
}

fn record_open_failure(cache: &mut IntakeBatch, path: &Path, failure: &OpenFailure) {
    let issue = SourceReadIssue::new(
        "cursor",
        path,
        "source-read-failed",
        format!("cursor database could not be opened: {}", failure.reason),
    );
    cache.record_read_issue(match failure {
        OpenFailure { moved: true, .. } => issue.moving(),
        OpenFailure { defect: None, .. } => issue.retryable(),
        _ => issue,
    });
}

/// The opened store's project attribution; None (recorded) when it could not be read.
fn read_attribution(
    cache: &mut IntakeBatch,
    conn: &Connection,
    user: &Path,
    path: &Path,
) -> Option<Attribution> {
    match workspace_projects_if_conversations(conn, user) {
        Ok(attribution) => Some(attribution),
        Err(error) => {
            crate::intake::diagnostic(format_args!(
                "  ! cursor: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&error)
            ));
            cache.record_source_read_issue("cursor", path, "source-read-failed", error.to_string());
            None
        }
    }
}

/// A conversation an unreadable workspace source leaves unattributed keeps the project its
/// cached rows carry (the published generation's when no cache holds them) or parses under
/// "cursor"; the gap is attribution-only since every conversation was read.
fn keep_published_projects(cache: &mut IntakeBatch, attribution: &mut Attribution) {
    let published = cache.token_projects("cursor");
    attribution.retain_published(&published);
    if let Some(gap) = attribution.incomplete.as_ref() {
        cache.record_source_read_issue(
            "cursor",
            &gap.path,
            ATTRIBUTION_ISSUE_KIND,
            gap.reason.clone(),
        );
    }
}

/// The opened store's token snapshot; None (recorded) when it could not be read.
fn read_snapshot(
    cache: &mut IntakeBatch,
    conn: &crate::ingest::ReadOnlyConnection,
    path: &Path,
    projects: &HashMap<String, String>,
) -> Option<TokenSnapshot> {
    match stable_token_snapshot(conn, path, projects, true) {
        Ok(snapshot) => Some(snapshot),
        Err(error) => {
            crate::intake::diagnostic(format_args!(
                "  ! cursor: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&error)
            ));
            let issue =
                SourceReadIssue::new("cursor", path, "source-read-failed", error.to_string());
            cache.record_read_issue(if error.is::<crate::ingest::SourceMoved>() {
                issue.moving()
            } else {
                issue
            });
            None
        }
    }
}

/// Composers whose parse failed: (database, session, panic reason).
type ParseIssues = RefCell<Vec<(PathBuf, String, Option<String>)>>;

fn parse_composer(
    conn: Option<&Connection>,
    db_path: Option<&Path>,
    projects: &HashMap<String, String>,
    sid: &str,
    parse_issues: &ParseIssues,
) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    if sid == SCHEMA_ABSENT_SESSION {
        return (Vec::new(), Vec::new(), ReadOutcome::Complete);
    }
    let Some(c) = conn else {
        return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
    };
    let path = db_path.expect("open connection has a database path");
    match crate::ingest::parse_guard::isolate("cursor", path, || parse_session(c, projects, sid)) {
        Ok((messages, events, true)) => (messages, events, ReadOutcome::Complete),
        // The composer failed whole: none of its rows are vouched for, and the record
        // below confines the failure to it, so its siblings stay complete.
        Ok((_, _, false)) => {
            parse_issues
                .borrow_mut()
                .push((path.to_path_buf(), sid.to_owned(), None));
            (Vec::new(), Vec::new(), ReadOutcome::Invalid)
        }
        Err(panic) => {
            parse_issues.borrow_mut().push((
                path.to_path_buf(),
                sid.to_owned(),
                Some(panic.reason),
            ));
            (Vec::new(), Vec::new(), ReadOutcome::Skipped)
        }
    }
}

/// The record names the composer whose parse failed: a reader must not charge a
/// database-wide record to the siblings that merely changed since.
fn record_parse_issues(cache: &mut IntakeBatch, parse_issues: ParseIssues) {
    for (path, session, panicked) in parse_issues.into_inner() {
        let reason = panicked
            .unwrap_or_else(|| "the composer's stored rows did not parse completely".to_owned());
        cache.record_read_issue(
            SourceReadIssue::new("cursor", path, "source-read-failed", reason).for_session(session),
        );
    }
}

/// The discovered profile's global database: where it is and, when it opened, its connection.
struct Store {
    user: Option<PathBuf>,
    path: Option<PathBuf>,
    conn: Option<crate::ingest::ReadOnlyConnection>,
    /// A database with a defect no retry heals, where nothing was ever published, lists no
    /// conversation and keeps its cache, like a schema-absent one, instead of failing closed.
    durably_blind: bool,
}

fn open_store(cache: &mut IntakeBatch, discovery: &Result<Option<PathBuf>, UserDirError>) -> Store {
    if let Err(error) = discovery {
        record_discovery_failure(cache, error);
    }
    let user = discovery.as_ref().ok().and_then(|user| user.clone());
    let path = user
        .as_ref()
        .map(|u| u.join("globalStorage").join("state.vscdb"));
    let opened = path.as_deref().map(open_ro);
    let durably_blind = matches!(
        opened,
        Some(Err(OpenFailure {
            defect: Some(_),
            ..
        }))
    );
    if let (Some(Err(failure)), Some(path)) = (opened.as_ref(), path.as_deref()) {
        record_open_failure(cache, path, failure);
    }
    Store {
        user,
        path,
        conn: opened.and_then(Result::ok),
        durably_blind,
    }
}

fn collect_discovered(
    cache: &mut IntakeBatch,
    discovery: Result<Option<PathBuf>, UserDirError>,
) -> (Vec<Message>, Vec<Event>) {
    let Store {
        user,
        path: db_path,
        conn,
        durably_blind,
    } = open_store(cache, &discovery);
    let mut attribution = match (&conn, &user, db_path.as_deref()) {
        (Some(conn), Some(user), Some(path)) => read_attribution(cache, conn, user, path),
        _ => None,
    };
    if let Some(attribution) = attribution.as_mut() {
        keep_published_projects(cache, attribution);
    }
    let projects = attribution
        .as_ref()
        .map(|attribution| &attribution.projects);
    let snapshot = match (&discovery, &conn, projects, db_path.as_deref()) {
        (Ok(None), _, _, _) => Some(TokenSnapshot::empty()),
        (Ok(Some(_)), Some(conn), Some(projects), Some(path)) => {
            read_snapshot(cache, conn, path, projects)
        }
        _ => None,
    };
    if let Some(census) = snapshot
        .as_ref()
        .and_then(|snapshot| snapshot.census.as_ref())
    {
        record_census(census);
    }
    // An unenumerable store lists no conversation, so its list may not be read as the whole
    // set: already-indexed conversations are preserved instead of pruned as deleted.
    let unenumerable = durably_blind
        || snapshot
            .as_ref()
            .is_some_and(|snapshot| snapshot.schema_absent);
    let has_readable_source = snapshot.is_some();
    let tokens = if durably_blind {
        Some(Vec::new())
    } else {
        snapshot.map(|snapshot| snapshot.sessions)
    };
    let empty_projects = HashMap::new();
    let projects = projects.unwrap_or(&empty_projects);
    let parse_issues = ParseIssues::default();
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
        |_, sid| {
            parse_composer(
                conn.as_deref(),
                db_path.as_deref(),
                projects,
                sid,
                &parse_issues,
            )
        },
    );
    record_parse_issues(cache, parse_issues);
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). Token: per-conversation staleness via a
/// composer+bubbles content hash.
pub struct Cursor;

impl crate::ingest::registry::Adapter for Cursor {
    fn name(&self) -> &'static str {
        "cursor"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Token
    }
    fn collect(&self, cache: &mut IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<PathBuf> {
        user_dir()
            .map(|u| u.join("globalStorage").join("state.vscdb"))
            .into_iter()
            .collect()
    }
    fn archive_roots(&self) -> Vec<PathBuf> {
        user_dir_candidates(&crate::ingest::home())
            .into_iter()
            .map(|p| p.join("globalStorage/state.vscdb"))
            .collect()
    }
    fn freshness_tokens(&self) -> crate::ingest::registry::TokenAvailability {
        freshness_tokens_at(user_dir_checked())
    }
    fn intake_tokens(&self) -> crate::ingest::registry::TokenAvailability {
        intake_tokens_at(user_dir_checked(), Some(&HashMap::new()))
    }
    fn intake_tokens_with(
        &self,
        cache: Option<&IntakeBatch>,
    ) -> crate::ingest::registry::TokenAvailability {
        let published = cache.map(|cache| cache.token_projects("cursor"));
        intake_tokens_at(user_dir_checked(), published.as_ref())
    }
    fn token_prefix(&self, database: &Path) -> Option<String> {
        // One database holds every conversation, so its ids carry no database namespace.
        (database.file_name() == Some(std::ffi::OsStr::new("state.vscdb"))).then(String::new)
    }
    fn runtime_issue_root(&self) -> PathBuf {
        let user =
            user_dir().unwrap_or_else(|| user_dir_candidates(&crate::ingest::home())[0].clone());
        user.join("globalStorage").join("state.vscdb")
    }
}
