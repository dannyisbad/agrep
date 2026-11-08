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

mod discovery;
mod session;
mod tokens;

use std::cell::RefCell;
use std::path::{Path, PathBuf};

use crate::intake_batch::{IntakeBatch, ReadOutcome, SourceReadIssue};
use crate::model::{Event, Message};
use discovery::{data_roots, discover, runtime_root, Discovery};
use session::{conversation_digest, parse_session};
use tokens::{live_tokens, open_with_tokens, Opened};

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
        crate::intake::diagnostic(format_args!(
            "  ! crush: cannot open {}: {}",
            crate::ingest::terminal_safe(path.display()),
            crate::ingest::terminal_safe(&error)
        ));
        open_failure(path, &error)
    })
}

/// Records what discovery could not read and every database that would not list its sessions.
fn record_open_issues(
    cache: &mut IntakeBatch,
    discovery: Option<&Discovery>,
    opened: Option<&Opened>,
) {
    if let Some(discovery) = discovery {
        for (path, reason) in &discovery.issues {
            cache.record_source_read_issue("crush", path, "source-read-failed", reason.clone());
        }
    }
    if let Some(opened) = opened {
        for (_, path, failure) in &opened.unavailable {
            let issue =
                SourceReadIssue::new("crush", path, "source-read-failed", failure.reason.clone());
            cache.record_read_issue(if failure.durable() {
                issue
            } else {
                issue.retryable()
            });
        }
    }
}

/// What the token cache is told about this pass's listing.
struct Listing {
    tokens: Option<Vec<(String, String, String)>>,
    unavailable: Vec<String>,
    preserve_unlisted: bool,
    has_readable_source: bool,
}

fn listing(discovery: Option<&Discovery>, opened: Option<&Opened>) -> Listing {
    let all_failed =
        opened.is_some_and(|opened| opened.databases.is_empty() && !opened.unavailable.is_empty());
    let incomplete_discovery = discovery.is_some_and(|discovery| discovery.incomplete);
    let no_healthy_database = opened
        .map(|opened| opened.databases.is_empty())
        .unwrap_or(true);
    // Every database failed with a defect no retry heals and the published generation held nothing
    // there: an empty listing that keeps every cached conversation, as for an incomplete discovery,
    // instead of failing closed and taking every other agent's retry lane down with it.
    let durably_blind = all_failed
        && !incomplete_discovery
        && opened.is_some_and(|opened| {
            opened
                .unavailable
                .iter()
                .all(|(_, _path, failure)| failure.durable())
        });
    let tokens = if durably_blind {
        Some(Vec::new())
    } else {
        opened
            .filter(|_| !all_failed && !(incomplete_discovery && no_healthy_database))
            .map(|opened| opened.tokens.clone())
    };
    let unavailable = opened
        .filter(|_| !all_failed)
        .map(|opened| {
            opened
                .unavailable
                .iter()
                .map(|(namespace, _, _)| namespace.clone())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    Listing {
        tokens,
        unavailable,
        preserve_unlisted: incomplete_discovery || durably_blind,
        has_readable_source: opened.is_some_and(|opened| !opened.databases.is_empty()),
    }
}

/// Conversations whose parse failed, gathered inside the cache's `Fn` parse callback.
#[derive(Default)]
struct ParseFailures {
    /// (database, session, panic reason)
    issues: RefCell<Vec<(PathBuf, String, Option<String>)>>,
    // A failed conversation is named by its own rows, never by its busy database's generation,
    // so one stuck chat in a database others keep writing still fails the same way twice.
    digests: RefCell<Vec<(String, String, String)>>,
}

impl ParseFailures {
    fn issue(&self, path: &Path, session: &str, panicked: Option<String>) {
        self.issues
            .borrow_mut()
            .push((path.to_path_buf(), session.to_owned(), panicked));
    }

    /// The record names the conversation whose parse failed: a reader must not charge a
    /// database-wide record to the siblings that merely changed since.
    fn record(self, cache: &mut IntakeBatch) {
        for (path, session, panicked) in self.issues.into_inner() {
            let reason = panicked.unwrap_or_else(|| {
                "the database session query did not produce a complete row".to_owned()
            });
            cache.record_read_issue(
                SourceReadIssue::new("crush", path, "source-read-failed", reason)
                    .for_session(session),
            );
        }
    }
}

fn parse_conversation(
    opened: Option<&Opened>,
    id: &str,
    session: &str,
    failures: &ParseFailures,
) -> (Vec<Message>, Vec<Event>, ReadOutcome) {
    let Some(opened) = opened else {
        return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
    };
    let Some(&index) = opened.locations.get(id) else {
        return (Vec::new(), Vec::new(), ReadOutcome::Invalid);
    };
    let (database, connection) = &opened.databases[index];
    let parsed = crate::ingest::parse_guard::isolate("crush", &database.path, || {
        parse_session(connection, &database.project, session)
    });
    match parsed {
        Ok((messages, events, true)) => {
            if messages.is_empty() && events.is_empty() {
                failures.issue(&database.path, session, None);
            }
            (messages, events, ReadOutcome::Complete)
        }
        // The conversation failed whole: none of its rows are vouched for, and the record
        // below confines the failure to it, so its siblings stay complete.
        Ok((_, _, false)) => {
            failures.issue(&database.path, session, None);
            if let Some(digest) = conversation_digest(connection, session) {
                failures
                    .digests
                    .borrow_mut()
                    .push((id.to_owned(), session.to_owned(), digest));
            }
            (Vec::new(), Vec::new(), ReadOutcome::Invalid)
        }
        Err(panic) => {
            failures.issue(&database.path, session, Some(panic.reason));
            (Vec::new(), Vec::new(), ReadOutcome::Skipped)
        }
    }
}

fn collect_discovered(
    cache: &mut IntakeBatch,
    discovery: std::io::Result<Discovery>,
) -> (Vec<Message>, Vec<Event>) {
    let discovery = match discovery {
        Ok(discovery) => Some(discovery),
        Err(error) => {
            crate::intake::diagnostic(format_args!(
                "  ! crush: discovery failed: {}",
                crate::ingest::terminal_safe(&error)
            ));
            None
        }
    };
    let opened = discovery.as_ref().map(open_with_tokens);
    record_open_issues(cache, discovery.as_ref(), opened.as_ref());
    let Listing {
        tokens,
        unavailable,
        preserve_unlisted,
        has_readable_source,
    } = listing(discovery.as_ref(), opened.as_ref());
    let failures = ParseFailures::default();
    let pass = cache.collect_token_cached_keyed_partial(
        "crush",
        tokens,
        &unavailable,
        preserve_unlisted,
        has_readable_source,
        |id, session| parse_conversation(opened.as_ref(), id, session, &failures),
    );
    failures.record(cache);
    (pass.messages, pass.events)
}

/// Open the crush store read-only and collect the user's messages + tool events, reparsing
/// only conversations whose token moved (Fingerprint::Token). A store that won't open serves
/// the cached conversations, never empty.
pub fn collect(cache: &mut IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    collect_discovered(cache, discover())
}

/// Registry entry (see ingest::registry). Tokens include the live SQLite generation.
pub struct Crush;
impl crate::ingest::registry::Adapter for Crush {
    fn archive_roots(&self) -> Vec<PathBuf> {
        let mut roots = self.store_roots();
        roots.extend(data_roots().into_iter().map(|p| p.join("projects.json")));
        roots
    }
    fn archive_content(&self, path: &Path) -> bool {
        self.store_content(path) || path.file_name().is_some_and(|n| n == "projects.json")
    }
    fn name(&self) -> &'static str {
        "crush"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Token
    }
    fn collect(&self, cache: &mut IntakeBatch) -> (Vec<Message>, Vec<Event>) {
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
        runtime_root()
    }
}
