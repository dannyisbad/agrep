//! Per-conversation staleness tokens: opening each discovered database and listing its
//! sessions under a stable SQLite generation.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use super::discovery::{discover, runtime_root, Database, Discovery};
use super::session::{MESSAGES_QUERY, PARENT_QUERY};
use super::{cache_id, cache_namespace, open_ro, Unenumerable};
use crate::ingest::registry::{token_fingerprint, TokenAvailability, TokenReadIssue};
use crate::ingest::ReadOnlyConnection;

const TOKENS_QUERY: &str = "SELECT id, updated_at FROM sessions";

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
    conn: &ReadOnlyConnection,
    path: &Path,
    project: &str,
) -> Result<Vec<(String, String)>, Unenumerable> {
    let project_token = crate::ingest::registry::fnv_token(project.as_bytes());
    let mut rejected = None;
    let tokens = stable_generation_read(path, Some(conn.source_generation()), |generation| {
        read_session_tokens(conn, generation, &project_token)
            .map_err(|error| rejected = schema_rejection(&error))
            .ok()
    });
    tokens.ok_or_else(|| rejected.unwrap_or_else(Unenumerable::transient))
}

/// The census inside one generation; the parse statements are prepared too, so a schema the
/// parser would reject fails here.
fn read_session_tokens(
    conn: &ReadOnlyConnection,
    generation: &str,
    project_token: &str,
) -> rusqlite::Result<Vec<(String, String)>> {
    let mut stmt = conn.prepare(TOKENS_QUERY)?;
    for query in [PARENT_QUERY, MESSAGES_QUERY] {
        conn.prepare(query)?;
    }
    let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?;
    let mut out = Vec::new();
    for r in rows {
        let r = r?;
        // updated_at present -> "u:<ms>" (see registry::token_fingerprint).
        let token = format!(
            "{}:{generation}:{project_token}",
            token_fingerprint(&serde_json::json!({ "updatedAt": r.1 }))
        );
        out.push((r.0, token));
    }
    out.sort();
    Ok(out)
}

pub(super) struct Opened {
    pub(super) databases: Vec<(Database, ReadOnlyConnection)>,
    pub(super) tokens: Vec<(String, String, String)>,
    pub(super) locations: HashMap<String, usize>,
    pub(super) unavailable: Vec<(String, PathBuf, Unenumerable)>,
}

/// The open database and its session tokens.
fn open_sessions(
    database: &Database,
) -> Result<(ReadOnlyConnection, Vec<(String, String)>), Unenumerable> {
    let connection = open_ro(&database.path)?;
    match session_tokens(&connection, &database.path, &database.project) {
        Ok(sessions) => Ok((connection, sessions)),
        Err(failure) => {
            crate::intake::diagnostic(format_args!(
                "  ! crush: cannot read sessions from {}",
                crate::ingest::terminal_safe(database.path.display())
            ));
            Err(failure)
        }
    }
}

pub(super) fn open_with_tokens(discovery: &Discovery) -> Opened {
    let mut opened = Vec::with_capacity(discovery.databases.len());
    let mut tokens = Vec::new();
    let mut locations = HashMap::new();
    let mut unavailable = Vec::new();
    for database in &discovery.databases {
        let namespace = cache_namespace(&database.path);
        let (connection, sessions) = match open_sessions(database) {
            Ok(open) => open,
            Err(failure) => {
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

pub(super) fn live_tokens(intake_keys: bool) -> TokenAvailability {
    live_tokens_from(discover(), intake_keys)
}

/// Each database the census opens lists its conversations; one it cannot is named by an issue
/// scoped to it, as is a registry whose databases stayed undiscovered. Only when nothing opened
/// is the whole census unreadable.
fn live_tokens_from(discovery: std::io::Result<Discovery>, intake_keys: bool) -> TokenAvailability {
    let discovery = match discovery {
        Ok(discovery) => discovery,
        Err(error) => {
            return TokenAvailability::Unreadable(vec![TokenReadIssue::new(
                runtime_root(),
                format!("source discovery failed: {error}"),
            )])
        }
    };
    let opened = open_with_tokens(&discovery);
    let issues = census_issues(&discovery, &opened.unavailable);
    let Opened {
        databases,
        tokens: live,
        locations,
        ..
    } = opened;
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

fn census_issues(
    discovery: &Discovery,
    unavailable: &[(String, PathBuf, Unenumerable)],
) -> Vec<TokenReadIssue> {
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
            runtime_root(),
            "source discovery was incomplete",
        ));
    }
    issues
}
