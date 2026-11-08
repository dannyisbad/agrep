//! Per-conversation staleness tokens and the raw-record census, read inside one stable
//! SQLite generation.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use rusqlite::types::Value as SqlValue;
use rusqlite::Connection;

use super::discovery::UserDirError;
use super::workspace::{workspace_projects_if_conversations, Attribution, AttributionGap};
use super::{has_cursor_table, val_bytes};
use crate::ingest::registry::{TokenAvailability, TokenReadIssue, ATTRIBUTION_ISSUE_KIND};

const TOKEN_SCAN_SQL: [&str; 2] = [
    "SELECT key, value FROM cursorDiskKV \
     WHERE key >= 'composerData:' AND key < 'composerData;' ORDER BY key",
    "SELECT key, value FROM cursorDiskKV \
     WHERE key >= 'bubbleId:' AND key < 'bubbleId;' ORDER BY key",
];
const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const CENSUS_SESSION: &str = "\0census\0";
/// Reserved non-conversation session (like CENSUS_SESSION) marking an unenumerable store,
/// so its zero conversations can never read as a complete, issue-free census of zero.
pub(super) const SCHEMA_ABSENT_SESSION: &str = "\0schema-absent\0";
const SCHEMA_ABSENT_TOKEN: &str = "unenumerable";

fn fnv_extend(hash: &mut u64, bytes: &[u8]) {
    for &byte in bytes {
        *hash ^= u64::from(byte);
        *hash = hash.wrapping_mul(0x100000001b3);
    }
}

fn fnv_finish(hash: u64) -> String {
    format!("h:{hash:016x}")
}

fn fnv_sql_value(hash: &mut u64, value: &SqlValue) {
    match value {
        SqlValue::Null => fnv_extend(hash, b"\0null\0"),
        SqlValue::Integer(value) => {
            fnv_extend(hash, b"\0integer\0");
            fnv_extend(hash, &value.to_le_bytes());
        }
        SqlValue::Real(value) => {
            fnv_extend(hash, b"\0real\0");
            fnv_extend(hash, &value.to_bits().to_le_bytes());
        }
        SqlValue::Text(value) => {
            fnv_extend(hash, b"\0text\0");
            fnv_extend(hash, value.as_bytes());
        }
        SqlValue::Blob(value) => {
            fnv_extend(hash, b"\0blob\0");
            fnv_extend(hash, value);
        }
    }
}

#[derive(Debug, Eq, PartialEq)]
pub(super) struct CursorCensus {
    token: String,
    composer_rows: u64,
    unreferenced_bubbles: u64,
}

pub(super) struct TokenSnapshot {
    pub(super) sessions: Vec<(String, String)>,
    pub(super) census: Option<CursorCensus>,
    /// The store held no `cursorDiskKV` to enumerate, so nothing about conversations was
    /// observed. Unknown, not proven empty: an enumerable table with zero rows leaves this
    /// false and still converges as the genuine fresh-profile case.
    pub(super) schema_absent: bool,
}

impl TokenSnapshot {
    /// No profile: nothing to list, and nothing unknown.
    pub(super) fn empty() -> Self {
        Self {
            sessions: Vec::new(),
            census: None,
            schema_absent: false,
        }
    }
}

fn add_references(cid: &str, value: &[u8], references: &mut HashSet<String>) {
    let Ok(record): Result<serde_json::Value, _> = serde_json::from_slice(value) else {
        return;
    };
    for header in record
        .get("fullConversationHeadersOnly")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
    {
        if let Some(bubble) = header.get("bubbleId").and_then(serde_json::Value::as_str) {
            references.insert(format!("bubbleId:{cid}:{bubble}"));
        }
    }
}

/// The composer a `bubbleId:<composer>:<bubble>` key belongs to. A composer id may itself hold
/// ':', so the key is not split at its first one: the longest known composer id followed by ':'
/// owns it, found with one map probe per ':' in the key.
fn bubble_composer<'a>(key: &'a str, composers: &HashMap<String, u64>) -> Option<&'a str> {
    let rest = key.strip_prefix("bubbleId:")?;
    rest.rmatch_indices(':')
        .map(|(at, _)| &rest[..at])
        .find(|cid| composers.contains_key(*cid))
}

fn key_value(row: &rusqlite::Row<'_>) -> rusqlite::Result<(String, SqlValue)> {
    Ok((row.get::<_, String>(0)?, row.get::<_, SqlValue>(1)?))
}

/// One token scan in progress: a hash per composer, extended by its bubbles, and the census
/// counters when one was asked for.
struct TokenScan {
    census: bool,
    hashes: HashMap<String, u64>,
    references: Option<HashSet<String>>,
    composer_rows: u64,
    bubble_rows: u64,
    unreferenced_bubbles: u64,
    detached_hash: u64,
}

impl TokenScan {
    fn new(census: bool) -> Self {
        Self {
            census,
            hashes: HashMap::new(),
            references: census.then(HashSet::new),
            composer_rows: 0,
            bubble_rows: 0,
            unreferenced_bubbles: 0,
            detached_hash: FNV_OFFSET,
        }
    }

    /// A row no conversation owns still moves the census.
    fn detach(&mut self, key: &str, value: &SqlValue) {
        if self.census {
            fnv_extend(&mut self.detached_hash, key.as_bytes());
            fnv_sql_value(&mut self.detached_hash, value);
        }
    }

    fn composers(&mut self, conn: &Connection) -> rusqlite::Result<()> {
        let mut composers = conn.prepare(TOKEN_SCAN_SQL[0])?;
        for row in composers.query_map([], key_value)? {
            let (key, value) = row?;
            self.composer(&key, value);
        }
        Ok(())
    }

    fn composer(&mut self, key: &str, value: SqlValue) {
        let Some(cid) = key.strip_prefix("composerData:") else {
            return;
        };
        self.composer_rows += 1;
        // NULL composerData rows exist in real stores; they carry no conversation.
        let Some(bytes) = val_bytes(value.clone()) else {
            self.detach(key, &value);
            return;
        };
        if let Some(references) = self.references.as_mut() {
            add_references(cid, &bytes, references);
        }
        let mut hash = FNV_OFFSET;
        fnv_extend(&mut hash, &bytes);
        self.hashes.insert(cid.to_string(), hash);
    }

    fn bubbles(&mut self, conn: &Connection) -> rusqlite::Result<()> {
        let mut bubbles = conn.prepare(TOKEN_SCAN_SQL[1])?;
        for row in bubbles.query_map([], key_value)? {
            let (key, value) = row?;
            self.bubble(&key, value);
        }
        Ok(())
    }

    fn bubble(&mut self, key: &str, value: SqlValue) {
        self.bubble_rows += 1;
        if self
            .references
            .as_ref()
            .is_some_and(|references| !references.contains(key))
        {
            self.unreferenced_bubbles += 1;
        }
        let Some(hash) =
            bubble_composer(key, &self.hashes).and_then(|cid| self.hashes.get_mut(cid))
        else {
            self.detach(key, &value);
            return;
        };
        fnv_extend(hash, key.as_bytes());
        if let Some(value) = val_bytes(value) {
            fnv_extend(hash, &value);
        }
    }

    fn finish(self, projects: &HashMap<String, String>) -> TokenSnapshot {
        let mut out: Vec<_> = self
            .hashes
            .into_iter()
            .map(|(cid, mut hash)| {
                fnv_extend(&mut hash, b"\0project\0");
                fnv_extend(
                    &mut hash,
                    projects
                        .get(&cid)
                        .map(String::as_str)
                        .unwrap_or("cursor")
                        .as_bytes(),
                );
                (cid, fnv_finish(hash))
            })
            .collect();
        out.sort_unstable_by(|left, right| left.0.cmp(&right.0));
        let census = self.references.map(|_| {
            let mut hash = FNV_OFFSET;
            fnv_extend(&mut hash, b"cursor-census-v1\0");
            for (session, token) in &out {
                fnv_extend(&mut hash, session.as_bytes());
                fnv_extend(&mut hash, b"\0");
                fnv_extend(&mut hash, token.as_bytes());
                fnv_extend(&mut hash, b"\0");
            }
            fnv_extend(&mut hash, &self.detached_hash.to_le_bytes());
            fnv_extend(&mut hash, &self.composer_rows.to_le_bytes());
            fnv_extend(&mut hash, &self.bubble_rows.to_le_bytes());
            fnv_extend(&mut hash, &self.unreferenced_bubbles.to_le_bytes());
            CursorCensus {
                token: fnv_finish(hash),
                composer_rows: self.composer_rows,
                unreferenced_bubbles: self.unreferenced_bubbles,
            }
        });
        TokenSnapshot {
            sessions: out,
            census,
            schema_absent: false,
        }
    }
}

/// Every session's token plus an optional raw-record census.
fn token_snapshot(
    conn: &Connection,
    projects: &HashMap<String, String>,
    census: bool,
) -> rusqlite::Result<TokenSnapshot> {
    if !has_cursor_table(conn)? {
        return Ok(TokenSnapshot {
            sessions: vec![(
                SCHEMA_ABSENT_SESSION.to_string(),
                SCHEMA_ABSENT_TOKEN.to_string(),
            )],
            census: None,
            schema_absent: true,
        });
    }
    let mut scan = TokenScan::new(census);
    scan.composers(conn)?;
    scan.bubbles(conn)?;
    Ok(scan.finish(projects))
}

fn stable_generation_read<T>(
    opened_generation: Option<&str>,
    path: &Path,
    read: impl FnOnce() -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let before = crate::ingest::sqlite_generation_token(path)?;
    if opened_generation.is_some_and(|generation| generation != before) {
        return Err(crate::ingest::SourceMoved(format!(
            "SQLite generation moved before reading {}",
            path.display()
        ))
        .into());
    }
    let value = read()?;
    let after = crate::ingest::sqlite_generation_token(path)?;
    if before != after {
        return Err(crate::ingest::SourceMoved(format!(
            "SQLite generation changed while reading {}",
            path.display()
        ))
        .into());
    }
    Ok(value)
}

pub(super) fn stable_token_snapshot(
    conn: &crate::ingest::ReadOnlyConnection,
    path: &Path,
    projects: &HashMap<String, String>,
    census: bool,
) -> anyhow::Result<TokenSnapshot> {
    stable_generation_read(Some(conn.source_generation()), path, || {
        let transaction = conn.unchecked_transaction()?;
        let snapshot = token_snapshot(&transaction, projects, census)?;
        transaction.commit()?;
        Ok(snapshot)
    })
}

pub(super) fn record_census(census: &CursorCensus) {
    let tally = crate::intake::tally();
    tally.seen_n(census.composer_rows + census.unreferenced_bubbles);
    tally.skip_n(crate::intake::Skip::Meta, census.composer_rows);
    tally.skip_n(
        crate::intake::Skip::Unreferenced,
        census.unreferenced_bubbles,
    );
}

pub(super) fn freshness_tokens_at(
    discovery: Result<Option<PathBuf>, UserDirError>,
) -> TokenAvailability {
    tokens_at(discovery, false, Some(&HashMap::new()))
}

pub(super) fn intake_tokens_at(
    discovery: Result<Option<PathBuf>, UserDirError>,
    published: Option<&HashMap<String, String>>,
) -> TokenAvailability {
    tokens_at(discovery, true, published)
}

/// `published` is the project the last-good rows gave each conversation (the parse cache's
/// view), which an unreadable workspace source then leaves in place, as `collect` does. None
/// when that cache could not be read: the pending conversations' tokens are then unknown, since
/// the index may have kept projects this census cannot see, and are hidden rather than listed.
fn tokens_at(
    discovery: Result<Option<PathBuf>, UserDirError>,
    intake_keys: bool,
    published: Option<&HashMap<String, String>>,
) -> TokenAvailability {
    let user = match discovery {
        Ok(Some(user)) => user,
        Ok(None) => return TokenAvailability::Empty,
        Err(error) => {
            let reason = format!("source discovery failed: {error}");
            return TokenAvailability::Unreadable(vec![TokenReadIssue::new(error.path, reason)]);
        }
    };
    let path = user.join("globalStorage").join("state.vscdb");
    let conn = match crate::ingest::open_sqlite_ro(&path) {
        Ok(conn) => conn,
        Err(error) => {
            let reason = format!("cannot open {}: {error}", path.display());
            let issue = match crate::ingest::sqlite_open_defect(&path, &error) {
                Some(kind) => TokenReadIssue::with_kind(&path, kind, reason),
                None => TokenReadIssue::new(&path, reason),
            };
            return TokenAvailability::Unreadable(vec![issue]);
        }
    };
    let mut attribution = match workspace_projects_if_conversations(&conn, &user) {
        Ok(attribution) => attribution,
        Err(error) => {
            return TokenAvailability::Unreadable(vec![census_read_issue(&path, &error)]);
        }
    };
    attribution.retain_published(published.unwrap_or(&HashMap::new()));
    match stable_token_snapshot(&conn, &path, &attribution.projects, intake_keys) {
        Ok(snapshot) => observed_tokens(
            &path,
            snapshot,
            attribution,
            intake_keys,
            published.is_none(),
        ),
        Err(error) => TokenAvailability::Unreadable(vec![census_read_issue(&path, &error)]),
    }
}

/// The snapshot's tokens, keyed for intake or freshness. Without the published projects
/// (`unpublished`), the conversations waiting on an unreadable workspace source are hidden.
fn observed_tokens(
    path: &Path,
    snapshot: TokenSnapshot,
    attribution: Attribution,
    intake_keys: bool,
    unpublished: bool,
) -> TokenAvailability {
    let pending = attribution.pending();
    let hidden = unpublished && !pending.is_empty();
    let mut tokens: Vec<_> = snapshot
        .sessions
        .into_iter()
        .filter(|(session, _)| !hidden || !pending.contains(session))
        .map(|(session, token)| {
            if intake_keys {
                (crate::intake::token_id(path, &session), token)
            } else {
                (session, token)
            }
        })
        .collect();
    if let (true, false, Some(census)) = (intake_keys, hidden, snapshot.census) {
        tokens.push((crate::intake::token_id(path, CENSUS_SESSION), census.token));
    }
    // Every conversation is listed; the unread workspace source only leaves some of
    // them under the fallback or their published project until it is readable.
    let issues = attribution
        .incomplete
        .map(|gap| vec![gap_issue(gap, hidden, pending)])
        .unwrap_or_default();
    TokenAvailability::from_observation(tokens, issues)
}

fn gap_issue(gap: AttributionGap, hidden: bool, pending: Vec<String>) -> TokenReadIssue {
    if hidden {
        let reason = format!(
            "{}; the parse cache cannot say which project {} conversation(s) \
             kept, so their tokens are unlisted",
            gap.reason,
            pending.len()
        );
        return TokenReadIssue::new(gap.path, reason).hiding(pending);
    }
    let reason = format!(
        "{}; {} conversation(s) keep project \"cursor\" and {} their published \
         project until it is readable",
        gap.reason,
        gap.unmapped.len(),
        gap.retained.len()
    );
    TokenReadIssue::with_kind(gap.path, ATTRIBUTION_ISSUE_KIND, reason).provisional(pending)
}

/// A census read that failed: on pages SQLite reads as corrupt, a defect no retry heals.
fn census_read_issue(path: &Path, error: &anyhow::Error) -> TokenReadIssue {
    let reason = format!("cannot read {}: {error}", path.display());
    let corrupt = error.chain().any(|cause| {
        cause
            .downcast_ref::<rusqlite::Error>()
            .is_some_and(|error| {
                crate::ingest::sqlite_corrupt(error)
                    || crate::ingest::sqlite_code(error) == Some(rusqlite::ErrorCode::NotADatabase)
            })
    });
    if corrupt {
        TokenReadIssue::with_kind(path, "corrupt-database", reason)
    } else {
        TokenReadIssue::new(path, reason)
    }
}
