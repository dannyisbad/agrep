use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Instant, UNIX_EPOCH};

mod index_lock;

use index_lock::IndexLock;

const RELEASE_VERSION_MARKER: &str = concat!("agrep-release-version:", env!("CARGO_PKG_VERSION"));

fn nonempty_env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

#[cfg(windows)]
fn platform_data_dir() -> Option<PathBuf> {
    platform_data_dir_from(
        nonempty_env_path("LOCALAPPDATA"),
        nonempty_env_path("USERPROFILE"),
    )
}

#[cfg(windows)]
fn platform_data_dir_from(local: Option<PathBuf>, home: Option<PathBuf>) -> Option<PathBuf> {
    local
        .or_else(|| home.map(|home| home.join("AppData/Local")))
        .map(|base| base.join("agrep"))
}

#[cfg(target_os = "macos")]
fn platform_data_dir() -> Option<PathBuf> {
    platform_data_dir_from(nonempty_env_path("HOME"))
}

#[cfg(target_os = "macos")]
fn platform_data_dir_from(home: Option<PathBuf>) -> Option<PathBuf> {
    home.map(|home| home.join("Library/Application Support/agrep"))
}

#[cfg(not(any(windows, target_os = "macos")))]
fn platform_data_dir() -> Option<PathBuf> {
    platform_data_dir_from(
        nonempty_env_path("XDG_DATA_HOME"),
        nonempty_env_path("HOME"),
    )
}

#[cfg(not(any(windows, target_os = "macos")))]
fn platform_data_dir_from(xdg: Option<PathBuf>, home: Option<PathBuf>) -> Option<PathBuf> {
    xdg.or_else(|| home.map(|home| home.join(".local/share")))
        .map(|base| base.join("agrep"))
}

fn resolve_data_dir(
    explicit: Option<PathBuf>,
    platform: Option<PathBuf>,
) -> anyhow::Result<PathBuf> {
    explicit.or(platform).ok_or_else(|| {
        anyhow::anyhow!(
            "AGREP_DATA_DIR is unset and no platform user data directory is available; set AGREP_DATA_DIR explicitly"
        )
    })
}

fn absolute_data_dir(path: PathBuf, cwd: &Path) -> PathBuf {
    if path.is_absolute() {
        path
    } else {
        cwd.join(path)
    }
}

fn data_dir() -> anyhow::Result<PathBuf> {
    let selected = resolve_data_dir(nonempty_env_path("AGREP_DATA_DIR"), platform_data_dir())?;
    let cwd = std::env::current_dir()
        .map_err(|error| anyhow::anyhow!("cannot resolve agrep data directory: {error}"))?;
    Ok(absolute_data_dir(selected, &cwd))
}

fn data_dir_is_protected(data: &Path) -> bool {
    let Some(protected) = nonempty_env_path("AGREP_DATA_READONLY") else {
        return false;
    };
    match (fs::canonicalize(data), fs::canonicalize(&protected)) {
        (Ok(actual), Ok(expected)) => actual == expected,
        _ => {
            #[cfg(windows)]
            {
                data.to_string_lossy()
                    .eq_ignore_ascii_case(&protected.to_string_lossy())
            }
            #[cfg(not(windows))]
            {
                data == protected
            }
        }
    }
}

fn lexical_absolute(path: &Path, cwd: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        cwd.join(path)
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                normalized.pop();
            }
            _ => normalized.push(component.as_os_str()),
        }
    }
    normalized
}

fn path_has_component_prefix(path: &Path, prefix: &Path) -> bool {
    let path: Vec<_> = path.components().map(|part| part.as_os_str()).collect();
    let prefix: Vec<_> = prefix.components().map(|part| part.as_os_str()).collect();
    path.len() >= prefix.len()
        && path.iter().zip(prefix).all(|(actual, expected)| {
            #[cfg(windows)]
            {
                actual
                    .to_string_lossy()
                    .eq_ignore_ascii_case(&expected.to_string_lossy())
            }
            #[cfg(not(windows))]
            {
                actual == &expected
            }
        })
}

fn resolve_nearest_existing(path: &Path) -> Option<PathBuf> {
    let mut cursor = path.to_path_buf();
    let mut missing = Vec::new();
    loop {
        if let Ok(mut resolved) = fs::canonicalize(&cursor) {
            for component in missing.iter().rev() {
                resolved.push(component);
            }
            return Some(resolved);
        }
        let component = cursor.file_name()?.to_os_string();
        if !cursor.pop() {
            return None;
        }
        missing.push(component);
    }
}

fn protected_root_contains_path(protected: &Path, target: &Path, cwd: &Path) -> bool {
    let protected = lexical_absolute(protected, cwd);
    let target = lexical_absolute(target, cwd);
    if path_has_component_prefix(&target, &protected) {
        return true;
    }
    match (
        resolve_nearest_existing(&protected),
        resolve_nearest_existing(&target),
    ) {
        (Some(protected), Some(target)) => path_has_component_prefix(&target, &protected),
        _ => false,
    }
}

fn refuse_protected_write_target(target: &Path, operation: &str) -> anyhow::Result<()> {
    let Some(protected) = nonempty_env_path("AGREP_DATA_READONLY") else {
        return Ok(());
    };
    let cwd = std::env::current_dir()
        .map_err(|error| anyhow::anyhow!("cannot resolve {operation} output path: {error}"))?;
    anyhow::ensure!(
        !protected_root_contains_path(&protected, target, &cwd),
        "{operation} refuses to write {} inside AGREP_DATA_READONLY {}",
        target.display(),
        protected.display(),
    );
    Ok(())
}

fn require_semantic_q8_output_ownership(output_dir: &Path) -> anyhow::Result<()> {
    let data = data_dir()?;
    let cwd = std::env::current_dir().map_err(|error| {
        anyhow::anyhow!("cannot resolve semantic-q8-build output path: {error}")
    })?;
    if !protected_root_contains_path(&data, output_dir, &cwd) {
        return Ok(());
    }
    match derived_write_ownership(&data) {
        DerivedWriteOwnership::Current => Ok(()),
        DerivedWriteOwnership::Foreign(reason)
        | DerivedWriteOwnership::Refused(reason)
        | DerivedWriteOwnership::PostAdoptionClobber(reason) => anyhow::bail!(
            "semantic-q8-build refuses to write {} inside AGREP_DATA_DIR {}: {reason}",
            output_dir.display(),
            data.display(),
        ),
        DerivedWriteOwnership::Adoption => anyhow::bail!(
            "semantic-q8-build refuses to write {} inside AGREP_DATA_DIR {}: \
             derived-store ownership is not established for this build",
            output_dir.display(),
            data.display(),
        ),
    }
}

const ROOT_STAGING_ARTIFACTS: &[&str] = &[
    ".boundary_stats.bin",
    ".changed_sessions",
    ".derived-owner.json",
    ".derived_generation.json",
    ".harness_prefixes.snapshot",
    ".ingest.sig",
    ".ingest_cache.bin",
    ".ingest_cache.bin.journal",
    ".ingest_pending.bin",
    ".source_absence_pending",
    ".source-health.json",
    ".source_snapshot.bin",
    "boundary_stats.json",
    "corpus.db",
    "event_stats.json",
    "intake_stats.json",
    "messages.jsonl",
    "replies.jsonl",
    "session_family.meta.json",
    "sessions.jsonl",
];

const DERIVED_OWNER_FILE: &str = ".derived-owner.json";
const DERIVED_OWNER_VERSION: u32 = 1;
const DERIVED_OWNER_MAX_BYTES: u64 = 4096;
const CORPUS_DB_SCHEMA_VERSION: &str = "15";
const CORPUS_DB_RETAINED_SCHEMA_VERSION: &str = "14";
const CORPUS_DB_TRIGGER_SCHEMA: &str = "4";
const CORPUS_DB_TRIGGER_NAMES: [&str; 7] = [
    "msgs_ad",
    "msgs_ai",
    "msgs_au",
    "msgs_prose_ad",
    "msgs_prose_ai",
    "msgs_prose_au_new",
    "msgs_prose_au_old",
];
const DERIVED_ADOPTION_OWNER_TOKEN_ENV: &str = "AGREP_DERIVED_ADOPTION_OWNER_TOKEN";
const DERIVED_ADOPTION_CLAIM_TOKEN_ENV: &str = "AGREP_DERIVED_ADOPTION_CLAIM_TOKEN";
const DERIVED_WRITER_IDENTITY_BLOCKED_ENV: &str = "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED";

#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct DerivedOwnerRecord {
    version: u32,
    build_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    legacy_corpus_db: Option<DerivedFileProof>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    retained_corpus_db: Option<RetainedCorpusDb>,
}

#[derive(Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct RetainedCorpusDb {
    build_id: String,
    proof: DerivedFileProof,
    reader_identity: agrep_core::ingest::registry::RegularFileReaderIdentity,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum DerivedWriteOwnership {
    Current,
    Adoption,
    /// A valid record names another build. Every writer refuses like `Refused`,
    /// except the explicit ingest, which may take over once no live writer holds it.
    Foreign(String),
    PostAdoptionClobber(String),
    Refused(String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum CorpusDbOwnerProbe {
    Missing,
    LegacyUnowned,
    Current,
    Foreign(String),
    Rebuildable(String),
    Uncertain(String),
    Unreadable(String),
}

#[derive(Debug)]
enum CorpusDbAdoptionError {
    Rebuildable(String),
    Retainable {
        detail: String,
        candidate: Box<RetainedCorpusDb>,
    },
    Fatal(String),
}

impl CorpusDbAdoptionError {
    fn retain_with(self, candidate: RetainedCorpusDb) -> Self {
        match self {
            Self::Rebuildable(detail) => Self::Retainable {
                detail,
                candidate: Box::new(candidate),
            },
            error => error,
        }
    }

    fn detail(self) -> String {
        match self {
            Self::Rebuildable(detail) | Self::Fatal(detail) => detail,
            Self::Retainable { detail, .. } => detail,
        }
    }
}

fn valid_derived_build_id(value: &str) -> bool {
    value.len() == 20
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

struct PrivateCorpusDbSnapshot {
    database: PathBuf,
    source: PathBuf,
    source_family: Vec<Option<agrep_core::ingest::registry::RegularFileEdgeSnapshot>>,
    source_identity: agrep_core::ingest::registry::RegularFileReaderIdentity,
    source_proof: DerivedFileProof,
}

fn corpus_db_family_snapshot(
    path: &Path,
) -> Result<Vec<Option<agrep_core::ingest::registry::RegularFileEdgeSnapshot>>, String> {
    ["", "-journal", "-wal", "-shm"]
        .into_iter()
        .map(|suffix| {
            let mut name = path.as_os_str().to_os_string();
            name.push(suffix);
            let member = PathBuf::from(name);
            agrep_core::ingest::registry::regular_file_edge_snapshot(&member, 512).map_err(
                |error| {
                    format!(
                        "cannot inspect corpus.db ownership member {}: {error}",
                        member.display()
                    )
                },
            )
        })
        .collect()
}

impl PrivateCorpusDbSnapshot {
    fn create(path: &Path) -> Result<Option<Self>, String> {
        let family_before = corpus_db_family_snapshot(path)?;
        let source_proof = family_before
            .first()
            .and_then(Option::as_ref)
            .ok_or_else(|| "corpus.db disappeared before its private snapshot".to_string())
            .and_then(|snapshot| {
                derived_file_proof_from_snapshot("corpus.db", snapshot)
                    .map_err(|error| error.to_string())
            })?;
        let identity_before = agrep_core::ingest::registry::regular_file_reader_identity(path)
            .map_err(|error| format!("cannot inspect corpus.db reader identity: {error}"))?
            .ok_or_else(|| "corpus.db disappeared before its private snapshot".to_string())?;
        let database = match private_corpus_stage(path, path).map_err(|error| error.detail())? {
            Some(database) => database,
            None => return Ok(None),
        };
        let snapshot = Self {
            database,
            source: path.to_path_buf(),
            source_family: family_before.clone(),
            source_identity: identity_before.clone(),
            source_proof,
        };
        for suffix in ["-journal", "-wal"] {
            let mut source = path.as_os_str().to_os_string();
            source.push(suffix);
            let source = PathBuf::from(source);
            let mut target = snapshot.database.as_os_str().to_os_string();
            target.push(suffix);
            let target = PathBuf::from(target);
            agrep_core::ingest::registry::copy_regular_file_snapshot(&source, &target).map_err(
                |error| {
                    format!(
                        "cannot copy {} into the private ownership snapshot: {error}",
                        source.display()
                    )
                },
            )?;
        }
        let identity_after = agrep_core::ingest::registry::regular_file_reader_identity(path)
            .map_err(|error| format!("cannot recheck corpus.db reader identity: {error}"))?;
        if corpus_db_family_snapshot(path)? != family_before
            || identity_after.as_ref() != Some(&identity_before)
        {
            return Ok(None);
        }
        Ok(Some(snapshot))
    }

    fn source_unchanged(&self) -> Result<bool, String> {
        let family = corpus_db_family_snapshot(&self.source)?;
        let identity = agrep_core::ingest::registry::regular_file_reader_identity(&self.source)
            .map_err(|error| format!("cannot inspect corpus.db reader identity: {error}"))?;
        Ok(family == self.source_family && identity.as_ref() == Some(&self.source_identity))
    }
}

impl Drop for PrivateCorpusDbSnapshot {
    fn drop(&mut self) {
        for suffix in ["", "-journal", "-wal", "-shm"] {
            let mut name = self.database.as_os_str().to_os_string();
            name.push(suffix);
            let _ = fs::remove_file(PathBuf::from(name));
        }
    }
}

/// Canonicalize the parent only: a NOFOLLOW open of the resolved directory cannot be
/// redirected mid-path, and the database itself is never followed.
fn resolve_sqlite_open_path(path: &Path) -> Result<PathBuf, String> {
    match (path.parent(), path.file_name()) {
        (Some(parent), Some(name)) => parent
            .canonicalize()
            .map(|parent| parent.join(name))
            .map_err(|error| format!("{} cannot be resolved: {error}", path.display())),
        _ => Err(format!("{} is not a database path", path.display())),
    }
}

fn probe_corpus_db_owner_once(path: &Path, current: &str) -> CorpusDbOwnerProbe {
    use rusqlite::OpenFlags;

    let sqlite_error = |error: rusqlite::Error| {
        let busy = matches!(
            &error,
            rusqlite::Error::SqliteFailure(failure, _)
                if matches!(
                    failure.code,
                    rusqlite::ErrorCode::DatabaseBusy
                        | rusqlite::ErrorCode::DatabaseLocked
                )
        );
        let reason = format!(
            "corpus.db ownership is {} at {}: {error}",
            if busy { "busy or moving" } else { "unreadable" },
            path.display()
        );
        if busy {
            CorpusDbOwnerProbe::Uncertain(reason)
        } else {
            CorpusDbOwnerProbe::Unreadable(reason)
        }
    };
    let sqlite_schema_error = |error: rusqlite::Error| {
        let structural = matches!(
            &error,
            rusqlite::Error::InvalidColumnType(..)
                | rusqlite::Error::FromSqlConversionFailure(..)
                | rusqlite::Error::IntegralValueOutOfRange(..)
                | rusqlite::Error::Utf8Error(..)
                | rusqlite::Error::QueryReturnedNoRows
        ) || matches!(
            &error,
            rusqlite::Error::SqliteFailure(failure, _)
                if matches!(
                    failure.code,
                    rusqlite::ErrorCode::DatabaseCorrupt
                        | rusqlite::ErrorCode::NotADatabase
                        | rusqlite::ErrorCode::TypeMismatch
                        | rusqlite::ErrorCode::Unknown
                )
        );
        if structural {
            CorpusDbOwnerProbe::Rebuildable(format!(
                "corpus.db schema is invalid at {}: {error}",
                path.display()
            ))
        } else {
            sqlite_error(error)
        }
    };
    match agrep_core::ingest::registry::regular_file_edge_snapshot(path, 0) {
        Ok(None) => return CorpusDbOwnerProbe::Missing,
        Ok(Some(_)) => {}
        Err(error) => {
            return CorpusDbOwnerProbe::Unreadable(format!(
                "corpus.db ownership is unreadable at {}: {error}",
                path.display()
            ));
        }
    }
    let open_path = match resolve_sqlite_open_path(path) {
        Ok(open_path) => open_path,
        Err(detail) => {
            return CorpusDbOwnerProbe::Unreadable(format!(
                "corpus.db ownership is unreadable: {detail}"
            ));
        }
    };
    let connection = match rusqlite::Connection::open_with_flags(
        open_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    ) {
        Ok(connection) => connection,
        Err(error) => return sqlite_error(error),
    };
    if let Err(error) = connection.busy_timeout(std::time::Duration::ZERO) {
        return sqlite_error(error);
    }
    let mut statement =
        match connection.prepare("SELECT value FROM meta WHERE key = 'build_id' LIMIT 2") {
            Ok(statement) => statement,
            Err(error) => return sqlite_schema_error(error),
        };
    let mut rows = match statement.query([]) {
        Ok(rows) => rows,
        Err(error) => return sqlite_schema_error(error),
    };
    let first = match rows.next() {
        Ok(Some(row)) => match row.get::<_, String>(0) {
            Ok(value) => Some(value),
            Err(error) => return sqlite_schema_error(error),
        },
        Ok(None) => None,
        Err(error) => return sqlite_schema_error(error),
    };
    if let Some(owner) = first {
        match rows.next() {
            Ok(None) if valid_derived_build_id(&owner) && owner == current => {
                CorpusDbOwnerProbe::Current
            }
            Ok(None) if valid_derived_build_id(&owner) => CorpusDbOwnerProbe::Foreign(owner),
            Ok(None) => CorpusDbOwnerProbe::Rebuildable(format!(
                "corpus.db ownership schema is malformed at {}",
                path.display()
            )),
            Ok(Some(_)) => CorpusDbOwnerProbe::Rebuildable(format!(
                "corpus.db has multiple ownership records at {}",
                path.display()
            )),
            Err(error) => sqlite_schema_error(error),
        }
    } else {
        CorpusDbOwnerProbe::LegacyUnowned
    }
}

fn probe_corpus_db_owner(path: &Path, current: &str) -> CorpusDbOwnerProbe {
    let sidecars = ["-journal", "-wal", "-shm"].map(|suffix| {
        let mut name = path.as_os_str().to_os_string();
        name.push(suffix);
        PathBuf::from(name)
    });
    let capture_sidecars = || {
        sidecars
            .iter()
            .map(|sidecar| {
                agrep_core::ingest::registry::regular_file_edge_snapshot(sidecar, 512).map_err(
                    |error| {
                        format!(
                            "corpus.db sidecar ownership is unreadable at {}: {error}",
                            sidecar.display()
                        )
                    },
                )
            })
            .collect::<Result<Vec<_>, _>>()
    };
    let before = match agrep_core::ingest::registry::regular_file_edge_snapshot(path, 512) {
        Ok(None) => {
            return match capture_sidecars() {
                Ok(snapshots) if snapshots.iter().any(Option::is_some) => {
                    CorpusDbOwnerProbe::Rebuildable(format!(
                        "corpus.db is missing while a sidecar remains at {}",
                        path.display()
                    ))
                }
                Ok(_) => CorpusDbOwnerProbe::Missing,
                Err(reason) => CorpusDbOwnerProbe::Uncertain(reason),
            };
        }
        Ok(Some(snapshot)) => snapshot,
        Err(error) => {
            return CorpusDbOwnerProbe::Unreadable(format!(
                "corpus.db ownership is unreadable at {}: {error}",
                path.display()
            ));
        }
    };
    let sidecars_before = match capture_sidecars() {
        Ok(snapshots) => snapshots,
        Err(reason) => return CorpusDbOwnerProbe::Uncertain(reason),
    };
    if sidecars_before[0]
        .as_ref()
        .is_some_and(|snapshot| snapshot.len != 0)
    {
        return CorpusDbOwnerProbe::Uncertain(format!(
            "corpus.db has a live rollback journal at {}",
            sidecars[0].display()
        ));
    }
    let checkpointed_delete = before.head.len() >= 20
        && &before.head[..16] == b"SQLite format 3\0"
        && before.head[18..20] == [1, 1]
        && sidecars_before[1]
            .as_ref()
            .is_none_or(|snapshot| snapshot.len == 0);
    let result = if checkpointed_delete {
        // The common corpus publication is already a locked, checkpointed
        // DELETE-journal database. Querying one metadata row in mode=ro is
        // coherent and does not copy a corpus-scale file or create sidecars.
        probe_corpus_db_owner_once(path, current)
    } else {
        // WAL can hold the only committed owner, and opening the live family
        // can create or mutate -shm. Query only a private main+journal/WAL
        // snapshot for that rare ownership shape.
        let private = match PrivateCorpusDbSnapshot::create(path) {
            Ok(Some(snapshot)) => snapshot,
            Ok(None) => {
                return CorpusDbOwnerProbe::Uncertain(format!(
                    "corpus.db ownership changed before it could be copied at {}",
                    path.display()
                ));
            }
            Err(reason) => return CorpusDbOwnerProbe::Uncertain(reason),
        };
        probe_corpus_db_owner_once(&private.database, current)
    };
    let after = match agrep_core::ingest::registry::regular_file_edge_snapshot(path, 512) {
        Ok(Some(snapshot)) => snapshot,
        Ok(None) => {
            return CorpusDbOwnerProbe::Uncertain(format!(
                "corpus.db ownership changed while it was read at {}",
                path.display()
            ));
        }
        Err(error) => {
            return CorpusDbOwnerProbe::Unreadable(format!(
                "corpus.db ownership is unreadable at {}: {error}",
                path.display()
            ));
        }
    };
    if before != after {
        return CorpusDbOwnerProbe::Uncertain(format!(
            "corpus.db ownership changed while it was read at {}",
            path.display()
        ));
    }
    let sidecars_after = match capture_sidecars() {
        Ok(snapshots) => snapshots,
        Err(reason) => return CorpusDbOwnerProbe::Uncertain(reason),
    };
    if sidecars_before != sidecars_after {
        return CorpusDbOwnerProbe::Uncertain(format!(
            "corpus.db sidecar ownership changed while it was read at {}",
            path.display()
        ));
    }
    result
}

fn derived_write_ownership(data: &Path) -> DerivedWriteOwnership {
    use agrep_core::ingest_cache::CacheOwnerProbe;

    let current = agrep_core::ingest_cache::current_cache_writer_build_id();
    let owner_path = data.join(DERIVED_OWNER_FILE);
    let owner = match read_optional_bytes(&owner_path, DERIVED_OWNER_MAX_BYTES) {
        Ok(Some(bytes)) => match serde_json::from_slice::<DerivedOwnerRecord>(&bytes) {
            Ok(record)
                if record.version == DERIVED_OWNER_VERSION
                    && valid_derived_build_id(&record.build_id) =>
            {
                if (record.legacy_corpus_db.is_some() && record.retained_corpus_db.is_some())
                    || record.retained_corpus_db.as_ref().is_some_and(|retained| {
                        !valid_derived_build_id(&retained.build_id)
                            || retained.proof.name != "corpus.db"
                            || retained.proof.len != retained.reader_identity.len
                            || retained.proof.modified_ns != retained.reader_identity.modified_ns
                    })
                {
                    return DerivedWriteOwnership::Refused(format!(
                        "derived-store ownership record {} is malformed",
                        owner_path.display()
                    ));
                }
                Some(record)
            }
            _ => {
                return DerivedWriteOwnership::Refused(format!(
                    "derived-store ownership record {} is malformed",
                    owner_path.display()
                ));
            }
        },
        Ok(None) => None,
        Err(error) => {
            return DerivedWriteOwnership::Refused(format!(
                "derived-store ownership record {} is unreadable: {error}",
                owner_path.display()
            ));
        }
    };
    if let Some(owner) = owner.as_ref() {
        if owner.build_id != current {
            return DerivedWriteOwnership::Foreign(format!(
                "derived stores owned-by {}; this build is {current}",
                owner.build_id
            ));
        }
    }

    let cache_path = data.join(".ingest_cache.bin");
    let cache_owner = agrep_core::ingest_cache::probe_cache_owner(&cache_path);
    match &cache_owner {
        CacheOwnerProbe::Foreign { build_id } | CacheOwnerProbe::Current { build_id }
            if build_id != &current =>
        {
            return DerivedWriteOwnership::Foreign(format!(
                "parse cache owned-by {build_id}; this build is {current}"
            ));
        }
        _ => {}
    }
    if let Some(owner) = owner.as_ref() {
        match &cache_owner {
            CacheOwnerProbe::Missing => {}
            CacheOwnerProbe::Current { build_id } if build_id == &current => {}
            CacheOwnerProbe::LegacyUnowned => {
                return DerivedWriteOwnership::Refused(format!(
                    "derived stores owned-by {}, but parse cache has no writing-build identity at {}",
                    owner.build_id,
                    cache_path.display()
                ));
            }
            CacheOwnerProbe::Unreadable => {
                return DerivedWriteOwnership::Refused(format!(
                    "parse cache ownership is unreadable at {}",
                    cache_path.display()
                ));
            }
            CacheOwnerProbe::Malformed => {
                return DerivedWriteOwnership::Refused(format!(
                    "parse cache ownership is malformed at {}",
                    cache_path.display()
                ));
            }
            CacheOwnerProbe::Foreign { .. } | CacheOwnerProbe::Current { .. } => unreachable!(),
        }
    }
    let corpus_owner = probe_corpus_db_owner(&data.join("corpus.db"), &current);
    if let CorpusDbOwnerProbe::Foreign(build_id) = &corpus_owner {
        let retained = owner.as_ref().and_then(|record| {
            record.retained_corpus_db.as_ref().filter(|retained| {
                record.build_id == current
                    && retained.build_id == *build_id
                    && agrep_core::ingest::registry::regular_file_reader_identity(
                        &data.join("corpus.db"),
                    )
                    .is_ok_and(|observed| observed.as_ref() == Some(&retained.reader_identity))
            })
        });
        if retained.is_none() {
            return DerivedWriteOwnership::Foreign(format!(
                "corpus.db owned-by {build_id}; this build is {current}"
            ));
        }
    }
    if let CorpusDbOwnerProbe::Uncertain(reason) = &corpus_owner {
        return DerivedWriteOwnership::Refused(reason.clone());
    }
    if let Some(owner) = owner.as_ref() {
        return match corpus_owner {
            CorpusDbOwnerProbe::Missing | CorpusDbOwnerProbe::Current => {
                DerivedWriteOwnership::Current
            }
            CorpusDbOwnerProbe::LegacyUnowned
                if owner.legacy_corpus_db.as_ref().is_some_and(|proof| {
                    derived_file_proof(data, "corpus.db").is_ok_and(|observed| &observed == proof)
                }) =>
            {
                DerivedWriteOwnership::Current
            }
            CorpusDbOwnerProbe::LegacyUnowned => {
                DerivedWriteOwnership::PostAdoptionClobber(format!(
                    "derived stores owned-by {}, but corpus.db has no writing-build identity and \
                     does not match the one legacy publication authorized by the ownership anchor; \
                     automatic repair is disabled because replacing corpus.db could destroy the \
                     last-good searchable snapshot; run agrep doctor for the safe backup-and-reindex \
                     remedy",
                    owner.build_id
                ))
            }
            CorpusDbOwnerProbe::Rebuildable(_) | CorpusDbOwnerProbe::Unreadable(_) => {
                // An explicit foreign owner and every moving/busy family were
                // refused above. The durable same-build anchor retains the
                // existing atomic repair path for damaged database bytes.
                DerivedWriteOwnership::Current
            }
            CorpusDbOwnerProbe::Foreign(_) => DerivedWriteOwnership::Current,
            CorpusDbOwnerProbe::Uncertain(_) => unreachable!(),
        };
    }
    match corpus_owner {
        CorpusDbOwnerProbe::Rebuildable(_) | CorpusDbOwnerProbe::Unreadable(_)
            if matches!(
                &cache_owner,
                CacheOwnerProbe::Current { build_id } if build_id == &current
            ) => {}
        CorpusDbOwnerProbe::Rebuildable(reason) => {
            return DerivedWriteOwnership::Refused(reason);
        }
        CorpusDbOwnerProbe::Unreadable(reason) => {
            return DerivedWriteOwnership::Refused(reason);
        }
        CorpusDbOwnerProbe::Missing
        | CorpusDbOwnerProbe::LegacyUnowned
        | CorpusDbOwnerProbe::Current
        | CorpusDbOwnerProbe::Foreign(_)
        | CorpusDbOwnerProbe::Uncertain(_) => {}
    }
    match cache_owner {
        CacheOwnerProbe::Missing | CacheOwnerProbe::LegacyUnowned => {
            DerivedWriteOwnership::Adoption
        }
        CacheOwnerProbe::Current { build_id } if build_id == current => {
            DerivedWriteOwnership::Adoption
        }
        CacheOwnerProbe::Foreign { build_id } => DerivedWriteOwnership::Foreign(format!(
            "parse cache owned-by {build_id}; this build is {current}"
        )),
        CacheOwnerProbe::Unreadable => DerivedWriteOwnership::Refused(format!(
            "parse cache ownership is unreadable at {}",
            cache_path.display()
        )),
        CacheOwnerProbe::Malformed => DerivedWriteOwnership::Refused(format!(
            "parse cache ownership is malformed at {}",
            cache_path.display()
        )),
        CacheOwnerProbe::Current { build_id } => DerivedWriteOwnership::Foreign(format!(
            "parse cache owned-by {build_id}; this build is {current}"
        )),
    }
}

fn daemon_owner_name(name: &str) -> bool {
    name == ".indexd.lock"
        || (name.starts_with(".indexd.v")
            && name.ends_with(".lock")
            && name
                .strip_prefix(".indexd.v")
                .and_then(|value| value.strip_suffix(".lock"))
                .is_some_and(|version| {
                    !version.is_empty() && version.bytes().all(|byte| byte.is_ascii_digit())
                }))
}

fn read_daemon_owner_fields(path: &Path, name: &str) -> Result<HashMap<String, String>, String> {
    let body = match read_optional_bytes(path, DERIVED_OWNER_MAX_BYTES) {
        Ok(Some(body)) => body,
        Ok(None) => {
            return Err(format!(
                "freshness-daemon owner {name} disappeared during legacy adoption"
            ));
        }
        Err(error) => {
            return Err(format!(
                "freshness-daemon owner {name} is unreadable: {error}"
            ));
        }
    };
    let text = match std::str::from_utf8(&body) {
        Ok(text) if text.ends_with('\n') => text,
        _ => return Err(format!("freshness-daemon owner {name} is malformed")),
    };
    let mut fields = HashMap::new();
    for field in text.split_ascii_whitespace() {
        let Some((key, value)) = field.split_once('=') else {
            return Err(format!("freshness-daemon owner {name} is malformed"));
        };
        if fields.insert(key.to_owned(), value.to_owned()).is_some() {
            return Err(format!(
                "freshness-daemon owner {name} has duplicate fields"
            ));
        }
    }
    Ok(fields)
}

fn adoption_daemon_fence(data: &Path) -> Option<String> {
    let entries = match fs::read_dir(data) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return None,
        Err(error) => {
            return Some(format!(
                "cannot inspect freshness-daemon ownership in {}: {error}",
                data.display()
            ));
        }
    };
    let mut owners = Vec::new();
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                return Some(format!(
                    "cannot inspect freshness-daemon ownership in {}: {error}",
                    data.display()
                ));
            }
        };
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if daemon_owner_name(&name) {
            let path = entry.path();
            match index_lock::reclaim_dead_owner_record(&path) {
                Ok(true) => continue,
                Ok(false) => owners.push((name, path)),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Some(format!(
                        "cannot inspect freshness-daemon owner {}: {error}",
                        path.display()
                    ));
                }
            }
        }
    }
    if owners.is_empty() {
        return None;
    }
    owners.sort_by(|left, right| left.0.cmp(&right.0));
    let current = agrep_core::ingest_cache::current_cache_writer_build_id();
    let claim_token = std::env::var(DERIVED_ADOPTION_CLAIM_TOKEN_ENV)
        .ok()
        .filter(|value| {
            value.len() == 32
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        });
    let mut external = Vec::new();
    for (name, path) in owners {
        let claimed = claim_token.as_ref().is_some_and(|token| {
            matches!(name.as_str(), ".indexd.lock" | ".indexd.v2.lock")
                && read_daemon_owner_fields(&path, &name).is_ok_and(|fields| {
                    fields.get("state").map(String::as_str) == Some("derived-adoption")
                        && fields.get("writer").map(String::as_str) == Some(current.as_str())
                        && fields.get("token").map(String::as_str) == Some(token.as_str())
                })
        });
        if !claimed {
            external.push((name, path));
        }
    }
    if external.is_empty() {
        return None;
    }
    if external.len() != 1 || external[0].0 == ".indexd.lock" {
        return Some(format!(
            "legacy or ambiguous freshness-daemon ownership is present ({})",
            external
                .iter()
                .map(|(name, _)| name.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    let fields = match read_daemon_owner_fields(&external[0].1, &external[0].0) {
        Ok(fields) => fields,
        Err(reason) => return Some(reason),
    };
    if fields.get("writer").map(String::as_str) != Some(current.as_str()) {
        return Some(format!(
            "freshness-daemon owner {} belongs to a different writing build",
            external[0].0
        ));
    }
    let expected_token = match std::env::var(DERIVED_ADOPTION_OWNER_TOKEN_ENV) {
        Ok(value)
            if value.len() == 32
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) =>
        {
            value
        }
        _ => {
            return Some(format!(
                "freshness-daemon owner {} is not authorized for this derived write",
                external[0].0
            ));
        }
    };
    if fields.get("token").map(String::as_str) != Some(expected_token.as_str()) {
        return Some(format!(
            "freshness-daemon owner {} belongs to a different writing generation",
            external[0].0
        ));
    }
    None
}

// The compatibility name reflects the ownerless first-adoption protocol, but
// the claim is held around every current writer too. That keeps a pre-R8
// daemon from starting between the ownership check and the final cache commit.
struct AdoptionClaim {
    token: String,
    files: Vec<(PathBuf, Vec<u8>)>,
}

impl AdoptionClaim {
    fn acquire(data: &Path) -> Result<Self, String> {
        if let Some(reason) = adoption_daemon_fence(data) {
            return Err(reason);
        }
        fs::create_dir_all(data).map_err(|error| {
            format!(
                "cannot create derived-store directory {}: {error}",
                data.display()
            )
        })?;
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|error| format!("cannot create derived-adoption claim: {error}"))?
            .as_nanos();
        let token = format!(
            "{:08x}{:024x}",
            std::process::id(),
            nanos & ((1_u128 << 96) - 1)
        );
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        let start =
            index_lock::current_process_start_identity().unwrap_or_else(|| "unknown".to_owned());
        let raw = format!(
            "state=derived-adoption pid={} start={start} writer={current} token={token}\n",
            std::process::id()
        )
        .into_bytes();
        let mut claim = Self {
            token,
            files: Vec::new(),
        };
        for name in [".indexd.lock", ".indexd.v2.lock"] {
            let path = data.join(name);
            match fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&path)
            {
                Ok(mut file) => {
                    if let Err(error) = file.write_all(&raw).and_then(|()| file.sync_all()) {
                        let _ = fs::remove_file(&path);
                        return Err(format!(
                            "cannot publish derived-adoption claim {}: {error}",
                            path.display()
                        ));
                    }
                    claim.files.push((path, raw.clone()));
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => {
                    return Err(format!(
                        "cannot publish derived-adoption claim {}: {error}",
                        path.display()
                    ));
                }
            }
        }
        std::env::set_var(DERIVED_ADOPTION_CLAIM_TOKEN_ENV, &claim.token);
        if let Some(reason) = adoption_daemon_fence(data) {
            return Err(reason);
        }
        Ok(claim)
    }
}

impl Drop for AdoptionClaim {
    fn drop(&mut self) {
        if std::env::var(DERIVED_ADOPTION_CLAIM_TOKEN_ENV).as_deref() == Ok(self.token.as_str()) {
            std::env::remove_var(DERIVED_ADOPTION_CLAIM_TOKEN_ENV);
        }
        for (path, expected) in self.files.iter().rev() {
            if read_optional_bytes(path, DERIVED_OWNER_MAX_BYTES)
                .is_ok_and(|body| body.as_deref() == Some(expected.as_slice()))
            {
                let _ = fs::remove_file(path);
            }
        }
    }
}

fn disclose_read_only_ownership(reason: &str) {
    eprintln!(
        "{}; serving the published snapshot read-only",
        agrep_core::ingest::terminal_safe(reason)
    );
}

fn remove_derived_artifact(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("cannot discard {}: {error}", path.display())),
    }
}

/// The rollback journal a writer leaves on disk only while it is mid-transaction - or after
/// it died there. An unreadable one is reported as present so the reclaim names it.
fn hot_rollback_journal(database: &Path) -> Result<Option<PathBuf>, String> {
    let mut name = database.as_os_str().to_os_string();
    name.push("-journal");
    let journal = PathBuf::from(name);
    match agrep_core::ingest::registry::regular_file_edge_snapshot(&journal, 0) {
        Ok(Some(snapshot)) if snapshot.len != 0 => Ok(Some(journal)),
        Ok(_) => Ok(None),
        Err(error) => Err(format!(
            "corpus.db rollback journal at {} is unreadable: {error}",
            journal.display()
        )),
    }
}

/// Finish a dead writer's rollback so a cold journal stops blocking every later run.
///
/// Nothing else reaps one. The ownership probe refuses to read a database mid-transaction, so a
/// journal left behind by a killed writer wedges every later build on the full-scan fallback
/// until a human deletes it. SQLite rolls a hot journal back on the first write-capable open,
/// and a writer that is still alive holds RESERVED against exactly that open: the exclusive
/// transaction below IS the liveness proof the owner claims use - it either completes the dead
/// writer's rollback, or it names the live writer and declines. A journal that survives that
/// transaction is one SQLite itself ruled not hot (an invalidated header, which is what an
/// exclusive-locking-mode writer leaves on every commit); no rollback will ever consume it, so
/// under the lock we just took it is inert debris and gets discarded.
fn reclaim_cold_rollback_journal(database: &Path) -> Result<(), String> {
    use rusqlite::OpenFlags;

    let Some(journal) = hot_rollback_journal(database)? else {
        return Ok(());
    };
    let open_path = resolve_sqlite_open_path(database).map_err(|detail| {
        format!(
            "corpus.db rollback journal at {} cannot be rolled back: {detail}",
            journal.display()
        )
    })?;
    rusqlite::Connection::open_with_flags(
        open_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )
    .and_then(|connection| {
        connection.busy_timeout(std::time::Duration::ZERO)?;
        connection.execute_batch("BEGIN EXCLUSIVE; COMMIT")
    })
    .map_err(|error| {
        let busy = matches!(
            &error,
            rusqlite::Error::SqliteFailure(failure, _)
                if matches!(
                    failure.code,
                    rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
                )
        );
        if busy {
            format!(
                "corpus.db has a live rollback journal at {}: {error}",
                journal.display()
            )
        } else {
            format!(
                "corpus.db rollback journal at {} cannot be rolled back: {error}",
                journal.display()
            )
        }
    })?;
    if hot_rollback_journal(database)?.is_some() {
        remove_derived_artifact(&journal)?;
    }
    if hot_rollback_journal(database)?.is_some() {
        return Err(format!(
            "corpus.db rollback journal at {} cannot be rolled back: it survived an exclusive \
             transaction and a discard",
            journal.display()
        ));
    }
    Ok(())
}

/// Successor takeover for the upgrade-day family: the derived stores name another build, but
/// no live writer holds them (the caller verified the daemon fence, reclaimed any cold rollback
/// journal, and holds both daemon-lock generations plus the index lock). Everything discarded here is a derivation of the
/// build-neutral published transcripts; only a record that provably names another build
/// authorizes a discard, and any uncertain probe aborts fail-closed. The run then proceeds
/// through the ordinary adoption corner, which publishes this build's ownership on success.
fn take_over_foreign_derived_stores(data: &Path, reason: &str) -> Result<(), String> {
    use agrep_core::ingest_cache::CacheOwnerProbe;

    let current = agrep_core::ingest_cache::current_cache_writer_build_id();
    let corpus = data.join("corpus.db");
    let cache_path = data.join(".ingest_cache.bin");
    let corpus_probe = probe_corpus_db_owner(&corpus, &current);
    let cache_probe = agrep_core::ingest_cache::probe_cache_owner(&cache_path);
    if let CorpusDbOwnerProbe::Uncertain(detail) | CorpusDbOwnerProbe::Unreadable(detail) =
        &corpus_probe
    {
        return Err(format!("{reason}; takeover declined: {detail}"));
    }
    if matches!(
        &cache_probe,
        CacheOwnerProbe::Unreadable | CacheOwnerProbe::Malformed
    ) {
        return Err(format!(
            "{reason}; takeover declined: parse cache ownership is not provably foreign at {}",
            cache_path.display()
        ));
    }
    let discard_corpus = || {
        for suffix in ["", "-journal", "-wal", "-shm"] {
            let mut name = corpus.as_os_str().to_os_string();
            name.push(suffix);
            remove_derived_artifact(&PathBuf::from(name))
                .map_err(|error| format!("{reason}; takeover declined: {error}"))?;
        }
        Ok::<(), String>(())
    };
    let cache_story = match cache_probe {
        CacheOwnerProbe::Missing => String::from("parse cache absent"),
        CacheOwnerProbe::LegacyUnowned => String::from("legacy parse cache kept"),
        CacheOwnerProbe::Current { build_id } if build_id == current => {
            String::from("parse cache already current")
        }
        CacheOwnerProbe::Current { .. } | CacheOwnerProbe::Foreign { .. } => {
            match agrep_core::ingest_cache::adopt_foreign_cache(&cache_path) {
                Ok(adopted) => {
                    format!("foreign parse cache verified and adopted ({adopted} sources)")
                }
                Err(_) => {
                    for path in [&cache_path, &data.join(".ingest_cache.bin.journal")] {
                        remove_derived_artifact(path)
                            .map_err(|error| format!("{reason}; takeover declined: {error}"))?;
                    }
                    String::from("foreign parse cache discarded")
                }
            }
        }
        CacheOwnerProbe::Unreadable | CacheOwnerProbe::Malformed => unreachable!(),
    };
    let mut retained_corpus = None;
    let corpus_story = match corpus_probe {
        CorpusDbOwnerProbe::Missing => String::from("search db absent"),
        CorpusDbOwnerProbe::Current => String::from("search db already current"),
        CorpusDbOwnerProbe::LegacyUnowned => String::from("legacy search db kept"),
        CorpusDbOwnerProbe::Foreign(foreign) => {
            match adopt_foreign_corpus_db(&corpus, &current, &foreign) {
                Ok(()) => String::from("search db verified and adopted"),
                Err(CorpusDbAdoptionError::Retainable { detail, candidate }) => {
                    retained_corpus = Some(*candidate);
                    format!(
                        "search db retained read-only for atomic rebuild ({})",
                        agrep_core::ingest::terminal_safe(&detail)
                    )
                }
                Err(CorpusDbAdoptionError::Rebuildable(detail)) => {
                    discard_corpus()?;
                    format!(
                        "search db discarded for rebuild ({})",
                        agrep_core::ingest::terminal_safe(&detail)
                    )
                }
                Err(error @ CorpusDbAdoptionError::Fatal(_)) => {
                    return Err(format!("{reason}; takeover declined: {}", error.detail()));
                }
            }
        }
        CorpusDbOwnerProbe::Rebuildable(detail) => {
            discard_corpus()?;
            format!(
                "search db discarded for rebuild ({})",
                agrep_core::ingest::terminal_safe(&detail)
            )
        }
        CorpusDbOwnerProbe::Uncertain(_) | CorpusDbOwnerProbe::Unreadable(_) => unreachable!(),
    };
    if let Some(candidate) = retained_corpus {
        publish_retained_corpus_owner(data, candidate)
            .map_err(|error| format!("{reason}; takeover declined: {error}"))?;
    } else {
        remove_derived_artifact(&data.join(DERIVED_OWNER_FILE))
            .map_err(|error| format!("{reason}; takeover declined: {error}"))?;
    }
    match derived_write_ownership(data) {
        DerivedWriteOwnership::Adoption | DerivedWriteOwnership::Current => {
            eprintln!(
                "{}; no live writer holds them - this build took over: {cache_story}, \
                 {corpus_story}, published transcripts kept",
                agrep_core::ingest::terminal_safe(reason)
            );
            Ok(())
        }
        DerivedWriteOwnership::Foreign(detail)
        | DerivedWriteOwnership::Refused(detail)
        | DerivedWriteOwnership::PostAdoptionClobber(detail) => {
            Err(format!("{reason}; takeover declined: {detail}"))
        }
    }
}

fn publish_retained_corpus_owner(data: &Path, candidate: RetainedCorpusDb) -> Result<(), String> {
    use agrep_core::ingest_cache::CacheOwnerProbe;

    let current = agrep_core::ingest_cache::current_cache_writer_build_id();
    let cache = data.join(".ingest_cache.bin");
    let cache_owned = match agrep_core::ingest_cache::probe_cache_owner(&cache) {
        CacheOwnerProbe::Missing => true,
        CacheOwnerProbe::Current { build_id } => build_id == current,
        _ => false,
    };
    if !cache_owned {
        return Err("retained search db has no current parse-cache owner".to_string());
    }
    let corpus = data.join("corpus.db");
    if probe_corpus_db_owner(&corpus, &current)
        != CorpusDbOwnerProbe::Foreign(candidate.build_id.clone())
    {
        return Err("retained search db changed before ownership publication".to_string());
    }
    let reader_identity = agrep_core::ingest::registry::regular_file_reader_identity(&corpus)
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "retained search db disappeared after reader validation".to_string())?;
    if reader_identity != candidate.reader_identity {
        return Err("retained search db changed after reader validation".to_string());
    }
    if candidate.proof.name != "corpus.db"
        || candidate.proof.len != candidate.reader_identity.len
        || candidate.proof.modified_ns != candidate.reader_identity.modified_ns
    {
        return Err("retained search db candidate is malformed".to_string());
    }
    let record = DerivedOwnerRecord {
        version: DERIVED_OWNER_VERSION,
        build_id: current,
        legacy_corpus_db: None,
        retained_corpus_db: Some(candidate),
    };
    cache::write_bytes_atomic(
        &data.join(DERIVED_OWNER_FILE),
        &serde_json::to_vec(&record).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    if derived_write_ownership(data) != DerivedWriteOwnership::Current {
        return Err("retained search db changed during ownership publication".to_string());
    }
    Ok(())
}

fn corpus_adoption_sqlite_error(
    context: &str,
    error: rusqlite::Error,
    schema_context: bool,
) -> CorpusDbAdoptionError {
    let structural = match &error {
        rusqlite::Error::InvalidColumnType(..)
        | rusqlite::Error::FromSqlConversionFailure(..)
        | rusqlite::Error::IntegralValueOutOfRange(..)
        | rusqlite::Error::Utf8Error(..)
        | rusqlite::Error::QueryReturnedNoRows => true,
        rusqlite::Error::SqliteFailure(failure, _) => {
            matches!(
                failure.code,
                rusqlite::ErrorCode::DatabaseCorrupt
                    | rusqlite::ErrorCode::NotADatabase
                    | rusqlite::ErrorCode::TypeMismatch
                    | rusqlite::ErrorCode::Unknown
            ) || (schema_context
                && matches!(failure.code, rusqlite::ErrorCode::ConstraintViolation))
        }
        _ => false,
    };
    let detail = format!("corpus.db adoption declined: {context}: {error}");
    if structural {
        CorpusDbAdoptionError::Rebuildable(detail)
    } else {
        CorpusDbAdoptionError::Fatal(detail)
    }
}

fn corpus_meta_value(
    connection: &rusqlite::Connection,
    key: &str,
) -> Result<Option<String>, CorpusDbAdoptionError> {
    let mut statement = connection
        .prepare("SELECT value FROM meta WHERE key = ?1 LIMIT 2")
        .map_err(|error| corpus_adoption_sqlite_error("invalid ownership schema", error, true))?;
    let mut rows = statement
        .query([key])
        .map_err(|error| corpus_adoption_sqlite_error("invalid ownership schema", error, true))?;
    let value = match rows
        .next()
        .map_err(|error| corpus_adoption_sqlite_error("invalid ownership schema", error, true))?
    {
        Some(row) => Some(row.get::<_, String>(0).map_err(|error| {
            corpus_adoption_sqlite_error("invalid ownership schema", error, true)
        })?),
        None => None,
    };
    if rows
        .next()
        .map_err(|error| corpus_adoption_sqlite_error("invalid ownership schema", error, true))?
        .is_some()
    {
        return Err(CorpusDbAdoptionError::Rebuildable(format!(
            "corpus.db adoption declined: multiple {key} rows"
        )));
    }
    Ok(value)
}

fn normalized_corpus_schema_sql(sql: &str) -> String {
    sql.split_ascii_whitespace()
        .collect::<String>()
        .to_ascii_lowercase()
}

fn expected_corpus_fts_text(text: Option<&str>) -> Option<String> {
    text.filter(|value| value.as_bytes().contains(&0))
        .map(|value| value.replace('\0', " "))
}

fn corpus_fts_text_is_valid(text: Option<&str>, fts_text: Option<&str>) -> bool {
    let expected = expected_corpus_fts_text(text);
    fts_text == expected.as_deref() && fts_text.is_none_or(|value| !value.as_bytes().contains(&0))
}

fn require_corpus_schema_sql(
    connection: &rusqlite::Connection,
    kind: &str,
    name: &str,
    expected: &str,
    compatible: Option<&str>,
    schema_version: &str,
) -> Result<(), CorpusDbAdoptionError> {
    let observed: String = connection
        .query_row(
            "SELECT sql FROM sqlite_schema WHERE type=?1 AND name=?2",
            [kind, name],
            |row| row.get(0),
        )
        .map_err(|error| {
            corpus_adoption_sqlite_error("search schema inventory failed", error, true)
        })?;
    let observed = normalized_corpus_schema_sql(&observed);
    if observed != normalized_corpus_schema_sql(expected)
        && compatible.is_none_or(|sql| observed != normalized_corpus_schema_sql(sql))
    {
        return Err(CorpusDbAdoptionError::Rebuildable(format!(
            "corpus.db adoption declined: {kind} {name} does not match schema {schema_version}"
        )));
    }
    Ok(())
}

fn validate_corpus_trigger_inventory(
    connection: &rusqlite::Connection,
) -> Result<(), CorpusDbAdoptionError> {
    let mut statement = connection
        .prepare("SELECT name FROM sqlite_schema WHERE type='trigger' ORDER BY name")
        .map_err(|error| corpus_adoption_sqlite_error("trigger inventory failed", error, true))?;
    let mut rows = statement
        .query([])
        .map_err(|error| corpus_adoption_sqlite_error("trigger inventory failed", error, true))?;
    let mut observed = Vec::new();
    while let Some(row) = rows
        .next()
        .map_err(|error| corpus_adoption_sqlite_error("trigger inventory failed", error, true))?
    {
        observed.push(row.get::<_, String>(0).map_err(|error| {
            corpus_adoption_sqlite_error("trigger inventory failed", error, true)
        })?);
    }
    if observed != CORPUS_DB_TRIGGER_NAMES {
        return Err(CorpusDbAdoptionError::Rebuildable(format!(
            "corpus.db adoption declined: trigger inventory does not match schema {CORPUS_DB_SCHEMA_VERSION}"
        )));
    }
    Ok(())
}

fn validate_corpus_search_schema_with(
    connection: &rusqlite::Connection,
    reader_only: bool,
) -> Result<(), CorpusDbAdoptionError> {
    let schema = corpus_meta_value(connection, "schema")?;
    let retained_reader =
        reader_only && schema.as_deref() == Some(CORPUS_DB_RETAINED_SCHEMA_VERSION);
    if schema.as_deref() != Some(CORPUS_DB_SCHEMA_VERSION) && !retained_reader {
        return Err(CorpusDbAdoptionError::Rebuildable(format!(
            "corpus.db adoption declined: search schema is not {CORPUS_DB_SCHEMA_VERSION}"
        )));
    }
    if corpus_meta_value(connection, "stamp")?.is_none_or(|stamp| stamp.is_empty()) {
        return Err(CorpusDbAdoptionError::Rebuildable(
            "corpus.db adoption declined: search generation stamp is missing".to_string(),
        ));
    }
    if !reader_only
        && corpus_meta_value(connection, "fts_triggers")?.as_deref()
            != Some(CORPUS_DB_TRIGGER_SCHEMA)
    {
        return Err(CorpusDbAdoptionError::Rebuildable(format!(
            "corpus.db adoption declined: FTS trigger schema is not {CORPUS_DB_TRIGGER_SCHEMA}"
        )));
    }
    if !reader_only {
        validate_corpus_trigger_inventory(connection)?;
    }
    for (kind, name, sql) in [
        (
            "table",
            "meta",
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)",
        ),
        (
            "table",
            "msgs",
            "CREATE TABLE msgs(
                id INTEGER PRIMARY KEY,
                session TEXT NOT NULL, turn INTEGER, ts INTEGER,
                agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
                who TEXT, text TEXT,
                fts_text TEXT CHECK(
                    fts_text IS NULL OR instr(fts_text, char(0)) = 0),
                content_digest TEXT CHECK(
                    content_digest IS NULL OR
                    (length(content_digest) = 4 AND
                     content_digest NOT GLOB '*[^0-9a-f]*')))",
        ),
        (
            "view",
            "msgs_fts_content",
            "CREATE VIEW msgs_fts_content AS
                SELECT id, coalesce(fts_text, text) AS text FROM msgs",
        ),
        (
            "view",
            "msgs_prose_fts_content",
            "CREATE VIEW msgs_prose_fts_content AS
                SELECT id, coalesce(fts_text, text) AS text
                FROM msgs WHERE who <> 'tool'",
        ),
        (
            "index",
            "msgs_session",
            "CREATE INDEX msgs_session ON msgs(session, turn)",
        ),
        (
            "index",
            "msgs_transcript_session_turn",
            "CREATE INDEX msgs_transcript_session_turn ON msgs(session, turn)
                WHERE who <> 'tool'",
        ),
        (
            "index",
            "msgs_who_ts",
            "CREATE INDEX msgs_who_ts ON msgs(who, coalesce(ts, 0) DESC)",
        ),
        (
            "index",
            "msgs_re_i_exceptions",
            "CREATE INDEX msgs_re_i_exceptions ON msgs(id) WHERE
                instr(text, 'İ') > 0 OR instr(text, 'ı') > 0
                OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0",
        ),
        (
            "table",
            "session_sig",
            "CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT)",
        ),
        (
            "table",
            "session_family",
            "CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL,
                side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID",
        ),
        (
            "index",
            "session_family_root",
            "CREATE INDEX session_family_root ON session_family(root)",
        ),
        (
            "table",
            "boundary_stats",
            "CREATE TABLE boundary_stats(
                token TEXT PRIMARY KEY, n INTEGER NOT NULL, s INTEGER NOT NULL,
                q INTEGER NOT NULL) WITHOUT ROWID",
        ),
        (
            "table",
            "msgs_fts",
            "CREATE VIRTUAL TABLE msgs_fts USING fts5(
                text, content='msgs_fts_content', content_rowid='id', tokenize='trigram')",
        ),
        (
            "table",
            "msgs_prose_fts",
            "CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
                text, content='msgs_prose_fts_content', content_rowid='id', tokenize='trigram')",
        ),
        (
            "trigger",
            "msgs_ai",
            "CREATE TRIGGER msgs_ai AFTER INSERT ON msgs BEGIN
                INSERT INTO msgs_fts(rowid, text)
                    VALUES (new.id, coalesce(new.fts_text, new.text));
            END",
        ),
        (
            "trigger",
            "msgs_ad",
            "CREATE TRIGGER msgs_ad AFTER DELETE ON msgs BEGIN
                INSERT INTO msgs_fts(msgs_fts, rowid, text)
                    VALUES('delete', old.id, coalesce(old.fts_text, old.text));
            END",
        ),
        (
            "trigger",
            "msgs_au",
            "CREATE TRIGGER msgs_au AFTER UPDATE OF text, fts_text ON msgs
            WHEN coalesce(old.fts_text, old.text) IS NOT
                 coalesce(new.fts_text, new.text) BEGIN
                INSERT INTO msgs_fts(msgs_fts, rowid, text)
                    VALUES('delete', old.id, coalesce(old.fts_text, old.text));
                INSERT INTO msgs_fts(rowid, text)
                    VALUES (new.id, coalesce(new.fts_text, new.text));
            END",
        ),
        (
            "trigger",
            "msgs_prose_ai",
            "CREATE TRIGGER msgs_prose_ai AFTER INSERT ON msgs WHEN new.who <> 'tool' BEGIN
                INSERT INTO msgs_prose_fts(rowid, text)
                    VALUES (new.id, coalesce(new.fts_text, new.text));
            END",
        ),
        (
            "trigger",
            "msgs_prose_ad",
            "CREATE TRIGGER msgs_prose_ad AFTER DELETE ON msgs WHEN old.who <> 'tool' BEGIN
                INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                    VALUES('delete', old.id, coalesce(old.fts_text, old.text));
            END",
        ),
        (
            "trigger",
            "msgs_prose_au_old",
            "CREATE TRIGGER msgs_prose_au_old AFTER UPDATE OF text, fts_text, who ON msgs
            WHEN old.who <> 'tool'
                 AND (coalesce(old.fts_text, old.text) IS NOT
                      coalesce(new.fts_text, new.text) OR new.who = 'tool') BEGIN
                INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                    VALUES('delete', old.id, coalesce(old.fts_text, old.text));
                INSERT INTO msgs_prose_fts(rowid, text)
                    SELECT new.id, coalesce(new.fts_text, new.text)
                    WHERE new.who <> 'tool';
            END",
        ),
        (
            "trigger",
            "msgs_prose_au_new",
            "CREATE TRIGGER msgs_prose_au_new AFTER UPDATE OF text, fts_text, who ON msgs
            WHEN old.who = 'tool' AND new.who <> 'tool' BEGIN
                INSERT INTO msgs_prose_fts(rowid, text)
                    VALUES (new.id, coalesce(new.fts_text, new.text));
            END",
        ),
    ] {
        let reader_object = match kind {
            "table" => matches!(
                name,
                "meta"
                    | "msgs"
                    | "session_family"
                    | "boundary_stats"
                    | "msgs_fts"
                    | "msgs_prose_fts"
            ),
            "view" => matches!(name, "msgs_fts_content" | "msgs_prose_fts_content"),
            _ => false,
        };
        if reader_only && !reader_object {
            continue;
        }
        require_corpus_schema_sql(
            connection,
            kind,
            name,
            sql,
            (retained_reader && name == "session_family").then_some(
                "CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL) WITHOUT ROWID",
            ),
            if retained_reader {
                CORPUS_DB_RETAINED_SCHEMA_VERSION
            } else {
                CORPUS_DB_SCHEMA_VERSION
            },
        )?;
    }
    let mut columns = HashSet::new();
    let mut statement = connection
        .prepare("PRAGMA table_info(msgs)")
        .map_err(|error| {
            corpus_adoption_sqlite_error("msgs columns are unreadable", error, true)
        })?;
    let mut rows = statement.query([]).map_err(|error| {
        corpus_adoption_sqlite_error("msgs columns are unreadable", error, true)
    })?;
    while let Some(row) = rows
        .next()
        .map_err(|error| corpus_adoption_sqlite_error("msgs columns are unreadable", error, true))?
    {
        columns.insert(row.get::<_, String>(1).map_err(|error| {
            corpus_adoption_sqlite_error("msgs columns are unreadable", error, true)
        })?);
    }
    for column in [
        "id",
        "session",
        "turn",
        "ts",
        "agent",
        "project",
        "concept",
        "model",
        "model_source",
        "who",
        "text",
        "content_digest",
    ] {
        if !columns.contains(column) {
            return Err(CorpusDbAdoptionError::Rebuildable(format!(
                "corpus.db adoption declined: msgs.{column} is missing"
            )));
        }
    }
    if !columns.contains("fts_text") {
        return Err(CorpusDbAdoptionError::Rebuildable(
            "corpus.db adoption declined: msgs.fts_text is missing".to_string(),
        ));
    }
    let mut statement = connection
        .prepare(
            "SELECT id, text, fts_text FROM msgs
             WHERE fts_text IS NOT NULL
                OR instr(CAST(text AS BLOB), x'00') > 0",
        )
        .map_err(|error| {
            corpus_adoption_sqlite_error("FTS sidecar validation failed", error, true)
        })?;
    let mut rows = statement.query([]).map_err(|error| {
        corpus_adoption_sqlite_error("FTS sidecar validation failed", error, true)
    })?;
    while let Some(row) = rows.next().map_err(|error| {
        corpus_adoption_sqlite_error("FTS sidecar validation failed", error, true)
    })? {
        let id = row.get::<_, i64>(0).map_err(|error| {
            corpus_adoption_sqlite_error("FTS sidecar validation failed", error, true)
        })?;
        let text = row.get::<_, Option<String>>(1).map_err(|error| {
            corpus_adoption_sqlite_error("FTS sidecar validation failed", error, true)
        })?;
        let fts_text = row.get::<_, Option<String>>(2).map_err(|error| {
            corpus_adoption_sqlite_error("FTS sidecar validation failed", error, true)
        })?;
        if !corpus_fts_text_is_valid(text.as_deref(), fts_text.as_deref()) {
            return Err(CorpusDbAdoptionError::Rebuildable(format!(
                "corpus.db adoption declined: msgs.fts_text is invalid for row {id}"
            )));
        }
    }
    for probe in [
        "SELECT rowid FROM msgs_fts WHERE msgs_fts MATCH 'agrepownershipprobe' LIMIT 1",
        "SELECT rowid FROM msgs_prose_fts
             WHERE msgs_prose_fts MATCH 'agrepownershipprobe' LIMIT 1",
    ] {
        let mut statement = connection
            .prepare(probe)
            .map_err(|error| corpus_adoption_sqlite_error("FTS probe failed", error, true))?;
        let mut rows = statement
            .query([])
            .map_err(|error| corpus_adoption_sqlite_error("FTS probe failed", error, true))?;
        rows.next()
            .map_err(|error| corpus_adoption_sqlite_error("FTS probe failed", error, true))?;
    }
    connection
        .execute(
            "INSERT INTO msgs_fts(msgs_fts, rank) VALUES('integrity-check', 1)",
            [],
        )
        .map_err(|error| {
            corpus_adoption_sqlite_error("trigram FTS integrity failed", error, true)
        })?;
    connection
        .execute(
            "INSERT INTO msgs_prose_fts(msgs_prose_fts, rank)
                 VALUES('integrity-check', 1)",
            [],
        )
        .map_err(|error| corpus_adoption_sqlite_error("prose FTS integrity failed", error, true))?;
    if retained_reader {
        for sql in [
            "SELECT EXISTS(SELECT id FROM msgs
                 EXCEPT SELECT id FROM msgs_fts_docsize)",
            "SELECT EXISTS(SELECT id FROM msgs_fts_docsize
                 EXCEPT SELECT id FROM msgs)",
            "SELECT EXISTS(SELECT id FROM msgs WHERE who <> 'tool'
                 EXCEPT SELECT id FROM msgs_prose_fts_docsize)",
            "SELECT EXISTS(SELECT id FROM msgs_prose_fts_docsize
                 EXCEPT SELECT id FROM msgs WHERE who <> 'tool')",
        ] {
            let differs: i64 =
                connection
                    .query_row(sql, [], |row| row.get(0))
                    .map_err(|error| {
                        corpus_adoption_sqlite_error(
                            "retained FTS row coverage failed",
                            error,
                            true,
                        )
                    })?;
            if differs != 0 {
                return Err(CorpusDbAdoptionError::Rebuildable(
                    "corpus.db adoption declined: retained FTS row coverage differs from content"
                        .to_string(),
                ));
            }
        }
    }
    Ok(())
}

fn validate_corpus_search_schema(
    connection: &rusqlite::Connection,
) -> Result<(), CorpusDbAdoptionError> {
    validate_corpus_search_schema_with(connection, false)
}

fn validate_corpus_reader_schema(
    connection: &rusqlite::Connection,
) -> Result<(), CorpusDbAdoptionError> {
    validate_corpus_search_schema_with(connection, true)
}

fn corpus_db_checkpointed_delete(path: &Path) -> Result<bool, CorpusDbAdoptionError> {
    let main = agrep_core::ingest::registry::regular_file_edge_snapshot(path, 64)
        .map_err(|error| {
            CorpusDbAdoptionError::Fatal(format!(
                "corpus.db adoption declined: cannot inspect {}: {error}",
                path.display()
            ))
        })?
        .ok_or_else(|| {
            CorpusDbAdoptionError::Fatal(format!(
                "corpus.db adoption declined: {} disappeared",
                path.display()
            ))
        })?;
    if main.head.len() < 20 || &main.head[..16] != b"SQLite format 3\0" {
        return Err(CorpusDbAdoptionError::Rebuildable(format!(
            "corpus.db adoption declined: {} is not a SQLite database",
            path.display()
        )));
    }
    if main.head[18..20] != [1, 1] {
        return Ok(false);
    }
    for suffix in ["-journal", "-wal", "-shm"] {
        let mut name = path.as_os_str().to_os_string();
        name.push(suffix);
        let sidecar = PathBuf::from(name);
        let snapshot = agrep_core::ingest::registry::regular_file_edge_snapshot(&sidecar, 0)
            .map_err(|error| {
                CorpusDbAdoptionError::Fatal(format!(
                    "corpus.db adoption declined: cannot inspect {}: {error}",
                    sidecar.display()
                ))
            })?;
        if snapshot.is_some_and(|snapshot| snapshot.len != 0) {
            return Ok(false);
        }
    }
    Ok(true)
}

fn private_corpus_stage(
    source: &Path,
    destination: &Path,
) -> Result<Option<PathBuf>, CorpusDbAdoptionError> {
    static SERIAL: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

    let parent = destination.parent().ok_or_else(|| {
        CorpusDbAdoptionError::Fatal(format!(
            "corpus.db adoption declined: {} has no parent directory",
            destination.display()
        ))
    })?;
    let file_name = destination
        .file_name()
        .unwrap_or_else(|| std::ffi::OsStr::new("corpus.db"))
        .to_string_lossy();
    let clock = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0);
    for _ in 0_u32..64 {
        let serial = SERIAL.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let staged = parent.join(format!(
            "{file_name}.tmp.{}.{clock}.{serial}",
            std::process::id(),
        ));
        match agrep_core::ingest::registry::copy_regular_file_snapshot(source, &staged) {
            Ok(true) => return Ok(Some(staged)),
            Ok(false) => {
                let _ = fs::remove_file(&staged);
                return Ok(None);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                let _ = fs::remove_file(&staged);
                return Err(CorpusDbAdoptionError::Fatal(format!(
                    "corpus.db adoption declined: cannot create private staging sibling: {error}"
                )));
            }
        }
    }
    Err(CorpusDbAdoptionError::Fatal(
        "corpus.db adoption declined: cannot allocate a private staging sibling".to_string(),
    ))
}

fn sync_private_corpus_stage(path: &Path) -> std::io::Result<()> {
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)?
        .sync_all()
}

#[cfg(not(windows))]
fn promote_corpus_takeover(
    staged: &Path,
    destination: &Path,
    expected: &agrep_core::ingest::registry::RegularFileReaderIdentity,
) -> std::io::Result<()> {
    verify_corpus_takeover_target(destination, expected)?;
    agrep_core::cache::promote_file_atomic(staged, destination)
        .map_err(|error| std::io::Error::other(error.to_string()))
}

#[cfg(any(windows, test))]
const CORPUS_TAKEOVER_RETRY_DELAYS_MS: [u64; 8] = [10, 20, 40, 80, 160, 320, 500, 500];

#[cfg(any(windows, test))]
fn retry_corpus_takeover_replace<V, F, S>(
    mut verify: V,
    mut replace: F,
    mut sleep: S,
) -> std::io::Result<()>
where
    V: FnMut() -> std::io::Result<()>,
    F: FnMut() -> std::io::Result<()>,
    S: FnMut(std::time::Duration),
{
    let mut delays = CORPUS_TAKEOVER_RETRY_DELAYS_MS.into_iter();
    loop {
        verify()?;
        match replace() {
            Ok(()) => return Ok(()),
            Err(error) => match delays.next() {
                Some(delay) => sleep(std::time::Duration::from_millis(delay)),
                None => return Err(error),
            },
        }
    }
}

fn verify_corpus_takeover_target(
    destination: &Path,
    expected: &agrep_core::ingest::registry::RegularFileReaderIdentity,
) -> std::io::Result<()> {
    let observed = agrep_core::ingest::registry::regular_file_reader_identity(destination)?;
    if observed.as_ref() != Some(expected) {
        return Err(std::io::Error::other(
            "corpus.db changed before atomic takeover",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn promote_corpus_takeover(
    staged: &Path,
    destination: &Path,
    expected: &agrep_core::ingest::registry::RegularFileReaderIdentity,
) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let source: Vec<u16> = staged.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    retry_corpus_takeover_replace(
        || verify_corpus_takeover_target(destination, expected),
        || {
            let replaced = unsafe {
                MoveFileExW(
                    source.as_ptr(),
                    target.as_ptr(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                )
            };
            if replaced != 0 {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        },
        std::thread::sleep,
    )
}

fn remove_corpus_sidecars(path: &Path) -> Result<(), CorpusDbAdoptionError> {
    for suffix in ["-journal", "-wal", "-shm"] {
        let mut name = path.as_os_str().to_os_string();
        name.push(suffix);
        let sidecar = PathBuf::from(name);
        remove_derived_artifact(&sidecar).map_err(CorpusDbAdoptionError::Fatal)?;
    }
    Ok(())
}

/// A verified private copy breaks aliases and reader snapshots before ownership changes.
fn adopt_foreign_corpus_db(
    path: &Path,
    current: &str,
    expected: &str,
) -> Result<(), CorpusDbAdoptionError> {
    adopt_foreign_corpus_db_with(path, current, expected, promote_corpus_takeover)
}

fn adopt_foreign_corpus_db_with<F>(
    path: &Path,
    current: &str,
    expected: &str,
    promote: F,
) -> Result<(), CorpusDbAdoptionError>
where
    F: FnOnce(
        &Path,
        &Path,
        &agrep_core::ingest::registry::RegularFileReaderIdentity,
    ) -> std::io::Result<()>,
{
    if !corpus_db_checkpointed_delete(path)? {
        return Err(CorpusDbAdoptionError::Rebuildable(
            "corpus.db adoption declined: WAL or rollback-journal families rebuild safely"
                .to_string(),
        ));
    }
    let snapshot = PrivateCorpusDbSnapshot::create(path)
        .map_err(CorpusDbAdoptionError::Fatal)?
        .ok_or_else(|| {
            CorpusDbAdoptionError::Fatal(
                "corpus.db adoption declined: database changed while it was copied".to_string(),
            )
        })?;
    if !corpus_db_checkpointed_delete(&snapshot.database)? {
        return Err(CorpusDbAdoptionError::Fatal(
            "corpus.db adoption declined: private snapshot is not checkpointed DELETE".to_string(),
        ));
    }
    let open_path = resolve_sqlite_open_path(&snapshot.database).map_err(|detail| {
        CorpusDbAdoptionError::Fatal(format!("corpus.db adoption declined: {detail}"))
    })?;
    let connection = rusqlite::Connection::open_with_flags(
        open_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
            | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX
            | rusqlite::OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )
    .map_err(|error| corpus_adoption_sqlite_error("open failed", error, false))?;
    connection
        .busy_timeout(std::time::Duration::ZERO)
        .map_err(|error| corpus_adoption_sqlite_error("busy policy failed", error, false))?;
    let verdict: String = connection
        .query_row("PRAGMA quick_check(1)", [], |row| row.get(0))
        .map_err(|error| corpus_adoption_sqlite_error("quick_check failed", error, false))?;
    if verdict != "ok" {
        return Err(CorpusDbAdoptionError::Rebuildable(format!(
            "corpus.db adoption declined: quick_check: {verdict}"
        )));
    }
    let snapshot_owner = corpus_meta_value(&connection, "build_id")?.ok_or_else(|| {
        CorpusDbAdoptionError::Rebuildable(
            "corpus.db adoption declined: missing build_id row".to_string(),
        )
    })?;
    if !valid_derived_build_id(&snapshot_owner) {
        return Err(CorpusDbAdoptionError::Rebuildable(
            "corpus.db adoption declined: malformed build_id row".to_string(),
        ));
    }
    if snapshot_owner != expected {
        return Err(CorpusDbAdoptionError::Fatal(
            "corpus.db adoption declined: owner changed while it was copied".to_string(),
        ));
    }
    if let Err(error) = validate_corpus_search_schema(&connection) {
        if validate_corpus_reader_schema(&connection).is_ok() {
            return match snapshot.source_unchanged() {
                Ok(true) => Err(error.retain_with(RetainedCorpusDb {
                    build_id: snapshot_owner.clone(),
                    proof: snapshot.source_proof.clone(),
                    reader_identity: snapshot.source_identity.clone(),
                })),
                Ok(false) => Err(CorpusDbAdoptionError::Fatal(
                    "corpus.db adoption declined: source family changed during reader validation"
                        .to_string(),
                )),
                Err(detail) => Err(CorpusDbAdoptionError::Fatal(format!(
                    "corpus.db adoption declined: source family cannot be revalidated: {detail}"
                ))),
            };
        }
        return Err(error);
    }
    let journal_mode: String = connection
        .query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))
        .map_err(|error| {
            corpus_adoption_sqlite_error("journal normalization failed", error, false)
        })?;
    if !journal_mode.eq_ignore_ascii_case("delete") {
        return Err(CorpusDbAdoptionError::Fatal(format!(
            "corpus.db adoption declined: journal mode remained {journal_mode}"
        )));
    }
    connection
        .execute_batch("BEGIN IMMEDIATE")
        .map_err(|error| corpus_adoption_sqlite_error("owner transaction failed", error, false))?;
    let updated = connection
        .execute(
            "UPDATE meta SET value = ?1 WHERE key = 'build_id' AND value = ?2",
            [current, expected],
        )
        .map_err(|error| corpus_adoption_sqlite_error("owner update failed", error, true))?;
    if updated != 1 {
        return Err(CorpusDbAdoptionError::Fatal(format!(
            "corpus.db adoption declined: {updated} build_id rows"
        )));
    }
    connection
        .execute_batch("COMMIT")
        .map_err(|error| corpus_adoption_sqlite_error("owner commit failed", error, false))?;
    let verdict: String = connection
        .query_row("PRAGMA quick_check('meta')", [], |row| row.get(0))
        .map_err(|error| {
            corpus_adoption_sqlite_error("post-update quick_check failed", error, false)
        })?;
    if verdict != "ok" {
        return Err(CorpusDbAdoptionError::Fatal(format!(
            "corpus.db adoption declined: post-update quick_check: {verdict}"
        )));
    }
    let published_owner = corpus_meta_value(&connection, "build_id")
        .map_err(|error| CorpusDbAdoptionError::Fatal(error.detail()))?;
    if published_owner.as_deref() != Some(current) {
        return Err(CorpusDbAdoptionError::Fatal(
            "corpus.db adoption declined: owner changed after update".to_string(),
        ));
    }
    drop(connection);
    sync_private_corpus_stage(&snapshot.database).map_err(|error| {
        CorpusDbAdoptionError::Fatal(format!(
            "corpus.db adoption declined: cannot sync private snapshot: {error}"
        ))
    })?;
    if !corpus_db_checkpointed_delete(&snapshot.database)? {
        return Err(CorpusDbAdoptionError::Fatal(
            "corpus.db adoption declined: normalized snapshot retained a live journal".to_string(),
        ));
    }
    let staged = snapshot.database.clone();
    match snapshot.source_unchanged() {
        Ok(true) => {}
        Ok(false) => {
            let _ = fs::remove_file(&staged);
            return Err(CorpusDbAdoptionError::Fatal(
                "corpus.db adoption declined: source family changed before publish".to_string(),
            ));
        }
        Err(error) => {
            let _ = fs::remove_file(&staged);
            return Err(CorpusDbAdoptionError::Fatal(format!(
                "corpus.db adoption declined: source family cannot be revalidated: {error}"
            )));
        }
    }
    if let Err(error) = promote(&staged, path, &snapshot.source_identity) {
        let _ = fs::remove_file(&staged);
        return Err(CorpusDbAdoptionError::Fatal(format!(
            "corpus.db adoption declined: atomic publish failed: {error}"
        )));
    }
    remove_corpus_sidecars(path)?;
    if !matches!(corpus_db_checkpointed_delete(path), Ok(true)) {
        return Err(CorpusDbAdoptionError::Fatal(
            "corpus.db adoption declined: published database is not checkpointed DELETE"
                .to_string(),
        ));
    }
    if probe_corpus_db_owner(path, current) != CorpusDbOwnerProbe::Current {
        return Err(CorpusDbAdoptionError::Fatal(
            "corpus.db adoption declined: published owner verification failed".to_string(),
        ));
    }
    Ok(())
}

fn publish_derived_owner(data: &Path) -> anyhow::Result<()> {
    match derived_write_ownership(data) {
        DerivedWriteOwnership::Current => {
            consume_retained_corpus_owner(data)?;
            return Ok(());
        }
        DerivedWriteOwnership::Foreign(reason)
        | DerivedWriteOwnership::Refused(reason)
        | DerivedWriteOwnership::PostAdoptionClobber(reason) => {
            anyhow::bail!("{reason}; refusing to replace derived-store ownership")
        }
        DerivedWriteOwnership::Adoption => {}
    }
    let record = DerivedOwnerRecord {
        version: DERIVED_OWNER_VERSION,
        build_id: agrep_core::ingest_cache::current_cache_writer_build_id(),
        legacy_corpus_db: if data.join("corpus.db").exists() {
            Some(derived_file_proof(data, "corpus.db")?)
        } else {
            None
        },
        retained_corpus_db: None,
    };
    let bytes = serde_json::to_vec(&record)?;
    cache::write_bytes_atomic(&data.join(DERIVED_OWNER_FILE), &bytes)?;
    anyhow::ensure!(
        derived_write_ownership(data) == DerivedWriteOwnership::Current,
        "derived-store ownership changed while it was being published"
    );
    Ok(())
}

fn consume_retained_corpus_owner(data: &Path) -> anyhow::Result<()> {
    use agrep_core::ingest_cache::CacheOwnerProbe;

    let owner_path = data.join(DERIVED_OWNER_FILE);
    let Some(bytes) = read_optional_bytes(&owner_path, DERIVED_OWNER_MAX_BYTES)? else {
        return Ok(());
    };
    let record: DerivedOwnerRecord = serde_json::from_slice(&bytes)?;
    if record.retained_corpus_db.is_none() {
        return Ok(());
    }
    let current = agrep_core::ingest_cache::current_cache_writer_build_id();
    let cache_current = matches!(
        agrep_core::ingest_cache::probe_cache_owner(&data.join(".ingest_cache.bin")),
        CacheOwnerProbe::Current { build_id } if build_id == current
    );
    if !cache_current
        || record.build_id != current
        || probe_corpus_db_owner(&data.join("corpus.db"), &current) != CorpusDbOwnerProbe::Current
    {
        return Ok(());
    }
    anyhow::ensure!(
        regular_file_has_bytes(&owner_path, &bytes)?,
        "derived-store ownership changed before retained authority was consumed"
    );
    let cleared = DerivedOwnerRecord {
        version: DERIVED_OWNER_VERSION,
        build_id: current,
        legacy_corpus_db: None,
        retained_corpus_db: None,
    };
    let cleared_bytes = serde_json::to_vec(&cleared)?;
    cache::write_bytes_atomic(&owner_path, &cleared_bytes)?;
    anyhow::ensure!(
        regular_file_has_bytes(&owner_path, &cleared_bytes)?
            && derived_write_ownership(data) == DerivedWriteOwnership::Current,
        "derived-store ownership changed while retained authority was consumed"
    );
    Ok(())
}

fn staging_temp_owner(name: &str) -> Option<u32> {
    let (base, raw_suffix) = name.rsplit_once(".tmp.")?;
    if !ROOT_STAGING_ARTIFACTS.contains(&base) {
        return None;
    }
    let suffix = if base == "corpus.db" {
        ["-journal", "-wal", "-shm"]
            .into_iter()
            .find_map(|ending| raw_suffix.strip_suffix(ending))
            .unwrap_or(raw_suffix)
    } else {
        raw_suffix
    };
    let mut parts = suffix.split('.');
    let pid = parts.next()?.parse().ok()?;
    parts.next()?.parse::<u128>().ok()?;
    if let Some(serial) = parts.next() {
        serial.parse::<u64>().ok()?;
    }
    if parts.next().is_some() {
        return None;
    }
    Some(pid)
}

fn sweep_staging_temps_with<F>(data: &Path, mut pid_alive: F) -> std::io::Result<(u64, u64)>
where
    F: FnMut(u32) -> Option<bool>,
{
    let mut removed = 0u64;
    let mut bytes = 0u64;
    let entries = match fs::read_dir(data) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok((0, 0));
        }
        Err(error) => return Err(error),
    };
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => continue,
        };
        let name = entry.file_name();
        let Some(pid) = name.to_str().and_then(staging_temp_owner) else {
            continue;
        };
        let metadata = match fs::symlink_metadata(entry.path()) {
            Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => metadata,
            _ => continue,
        };
        if pid_alive(pid) != Some(false) {
            continue;
        }
        match fs::remove_file(entry.path()) {
            Ok(()) => {
                removed += 1;
                bytes = bytes.saturating_add(metadata.len());
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok((removed, bytes))
}

fn sweep_staging_temps(data: &Path) -> std::io::Result<(u64, u64)> {
    sweep_staging_temps_with(data, index_lock::pid_alive_for_cleanup)
}

fn default_data_dir_selected() -> bool {
    match std::env::var("AGREP_DATA_DIR_SOURCE").ok().as_deref() {
        Some("default") => true,
        Some("env") => false,
        _ => nonempty_env_path("AGREP_DATA_DIR").is_none(),
    }
}

#[cfg(unix)]
fn protect_default_data_dir(path: &Path) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::metadata(path).map_err(|error| {
        anyhow::anyhow!(
            "could not inspect agrep data directory {}: {error}",
            path.display()
        )
    })?;
    let mut permissions = metadata.permissions();
    permissions.set_mode((permissions.mode() | 0o700) & !0o077);
    fs::set_permissions(path, permissions).map_err(|error| {
        anyhow::anyhow!(
            "could not protect agrep data directory {}: {error}",
            path.display()
        )
    })
}

#[cfg(not(unix))]
fn protect_default_data_dir(_path: &Path) -> anyhow::Result<()> {
    Ok(())
}

use agrep_core::cache;
use agrep_core::ingest;
use agrep_core::ingest_cache::{IngestCache, StagedCache};
use agrep_core::model::{Event, Message};
use clap::{Parser, Subcommand};
use rayon::prelude::*;
use sha2::{Digest as _, Sha256};

#[derive(Parser)]
#[command(
    name = "agrep-rs",
    version,
    about = "ingest agent chat transcripts into the agrep index"
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Ingest transcripts and write data/messages.jsonl + data/replies.jsonl (the input the
    /// Python sidecar embeds/scores/clusters from).
    Index {
        /// Which agent to ingest: claude | codex | opencode | antigravity | kimi | cline |
        /// gemini | crush | cursor | pi (includes oh-my-pi/omp) | all.
        #[arg(long, default_value = "all")]
        agent: String,
        /// Ignore the per-file parse cache and re-parse every source file from scratch.
        #[arg(long)]
        full: bool,
        /// Stream every materialized message to stdout as `{"row":{...}}` JSON lines while
        /// the ingest runs (plus `{"progress":...}` per parsed file) - the first-run search
        /// greps this stream so hits print during the cold parse, not after it.
        #[arg(long)]
        emit_rows: bool,
    },
    /// Probe for detected-but-not-indexed stores and print `[{"name","count"}]` as JSON.
    Detect,
    /// Per-adapter store presence + newest file mtime as JSON
    /// `[{"name","files","newest_mtime_ms"}]` - raw material for the doctor drift canary.
    Stores {
        /// List every content FILE path per adapter instead of the summary
        /// (`[{"name","path"}]` rows), using the registered Rust store roots
        /// and content predicates.
        #[arg(long)]
        paths: bool,
        /// Emit exact live token-store fingerprints keyed like intake tallies.
        #[arg(long, hide = true)]
        tokens: bool,
        /// Emit one versioned audit boundary containing paths, tokens, issues,
        /// completeness, and collision-resistant source-state digests.
        #[arg(long, hide = true, conflicts_with_all = ["paths", "tokens"])]
        audit: bool,
        /// Limit the hidden audit boundary to one registered adapter.
        #[arg(long, hide = true, requires = "audit")]
        agent: Option<String>,
    },
    /// Evaluate one query over a JSON batch on stdin and return in-order boundary scores.
    BoundaryRank {
        #[arg(long, hide = true)]
        serve: bool,
    },
    #[command(name = "__fallback-event-scan", hide = true)]
    FallbackEventScan,
    /// Derive an immutable q8 cosine artifact from one committed f32 generation.
    SemanticQ8Build {
        #[arg(long)]
        embeddings: String,
        #[arg(long)]
        meta: String,
        #[arg(long)]
        groups: String,
        #[arg(long)]
        output_dir: String,
        #[arg(long)]
        numeric_groups: bool,
    },
    /// Serve q8 similarity candidates over the internal binary worker protocol.
    SemanticQ8Serve {
        #[arg(long, required_unless_present = "set", conflicts_with = "set")]
        artifact: Option<String>,
        #[arg(long, requires = "artifact", conflicts_with = "set")]
        groups: Option<String>,
        #[arg(long, conflicts_with_all = ["artifact", "groups"])]
        set: Option<String>,
    },
    #[command(hide = true)]
    IndexLockContract {
        #[command(subcommand)]
        command: IndexLockContractCommand,
    },
}

#[derive(Subcommand)]
enum IndexLockContractCommand {
    Describe,
    Render {
        #[arg(long)]
        pid: u32,
        #[arg(long)]
        start: String,
        #[arg(long)]
        token: String,
        #[arg(long)]
        label: String,
        #[arg(long)]
        time_ms: u64,
    },
    Hold {
        #[arg(long)]
        path: PathBuf,
        #[arg(long)]
        label: String,
        #[arg(long, default_value_t = index_lock::TIMEOUT_MS)]
        timeout_ms: u64,
    },
}

fn run() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Index {
            agent,
            full,
            emit_rows,
        } => {
            if emit_rows {
                agrep_core::emit::enable();
            }
            index_cmd(&agent, full)
        }
        Cmd::Detect => detect_cmd(),
        Cmd::Stores {
            paths,
            tokens,
            audit,
            agent,
        } => stores_cmd(paths, tokens, audit, agent.as_deref().unwrap_or("all")),
        Cmd::BoundaryRank { serve } => boundary_rank_cmd(serve),
        Cmd::FallbackEventScan => fallback_event_scan_cmd(),
        Cmd::SemanticQ8Build {
            embeddings,
            meta,
            groups,
            output_dir,
            numeric_groups,
        } => semantic_q8_build_cmd(&embeddings, &meta, &groups, &output_dir, numeric_groups),
        Cmd::SemanticQ8Serve {
            artifact,
            groups,
            set,
        } => semantic_q8_serve_cmd(artifact.as_deref(), groups.as_deref(), set.as_deref()),
        Cmd::IndexLockContract { command } => index_lock_contract_cmd(command),
    }
}

fn main() {
    std::hint::black_box(RELEASE_VERSION_MARKER);
    // An explicit stack erases platform main-thread stack-size variance while
    // the join preserves the worker's panic.
    let worker = std::thread::Builder::new()
        .name("agrep-main".into())
        .stack_size(16 * 1024 * 1024)
        .spawn(run)
        .expect("spawn main worker thread");
    match worker.join() {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            eprintln!(
                "Error: {}",
                agrep_core::ingest::terminal_safe(format!("{error:#}"))
            );
            std::process::exit(1);
        }
        Err(panic) => std::panic::resume_unwind(panic),
    }
}

fn index_lock_contract_cmd(command: IndexLockContractCommand) -> anyhow::Result<()> {
    match command {
        IndexLockContractCommand::Describe => {
            serde_json::to_writer(std::io::stdout().lock(), &index_lock::describe_contract())?;
            println!();
        }
        IndexLockContractCommand::Render {
            pid,
            start,
            token,
            label,
            time_ms,
        } => {
            let raw = index_lock::render_record(pid, &start, &token, &label, time_ms)?;
            std::io::stdout().lock().write_all(&raw)?;
        }
        IndexLockContractCommand::Hold {
            path,
            label,
            timeout_ms,
        } => {
            refuse_protected_write_target(&path, "index-lock-contract hold")?;
            let mut lock =
                IndexLock::acquire(&path, &label, std::time::Duration::from_millis(timeout_ms))?;
            serde_json::to_writer(
                std::io::stdout().lock(),
                &serde_json::json!({"ready": true, "pid": std::process::id()}),
            )?;
            println!();
            std::io::stdout().flush()?;
            std::io::copy(&mut std::io::stdin().lock(), &mut std::io::sink())?;
            lock.release()?;
        }
    }
    Ok(())
}

fn boundary_rank_cmd(serve: bool) -> anyhow::Result<()> {
    if serve {
        return boundary_rank_serve_cmd();
    }

    use std::io::{Read, Write};

    let started = std::time::Instant::now();
    let mut input = Vec::new();
    std::io::stdin().lock().read_to_end(&mut input)?;
    let read = started.elapsed();
    let request: agrep_core::boundary_rank::BatchRequest = serde_json::from_slice(&input)?;
    let timing = std::env::var_os("AGREP_BOUNDARY_TIMING").is_some();
    let item_count = timing.then_some(request.items.len());
    let parsed = started.elapsed();
    let response = agrep_core::boundary_rank::evaluate_batch(request)?;
    let evaluated = started.elapsed();
    let mut output = serde_json::to_vec(&response)?;
    output.push(b'\n');
    let serialized = started.elapsed();
    std::io::stdout().lock().write_all(&output)?;
    if let Some(item_count) = item_count {
        eprintln!(
            "boundary-rank read={:.3}ms parse={:.3}ms evaluate={:.3}ms serialize={:.3}ms total={:.3}ms input={} output={} items={}",
            read.as_secs_f64() * 1000.0,
            (parsed - read).as_secs_f64() * 1000.0,
            (evaluated - parsed).as_secs_f64() * 1000.0,
            (serialized - evaluated).as_secs_f64() * 1000.0,
            started.elapsed().as_secs_f64() * 1000.0,
            input.len(),
            output.len(),
            item_count,
        );
    }
    Ok(())
}

fn read_fallback_event_scan_request<R: std::io::Read>(
    reader: R,
    limit: u64,
) -> anyhow::Result<Vec<u8>> {
    use std::io::Read as _;

    let mut input = Vec::new();
    reader
        .take(limit.saturating_add(1))
        .read_to_end(&mut input)?;
    anyhow::ensure!(
        input.len() as u64 <= limit,
        "fallback event-scan request is too large"
    );
    Ok(input)
}

fn fallback_event_scan_cmd() -> anyhow::Result<()> {
    use std::io::Write;

    const REQUEST_MAX_BYTES: u64 = 16 * 1024 * 1024;
    let input = read_fallback_event_scan_request(std::io::stdin().lock(), REQUEST_MAX_BYTES)?;
    let request: agrep_core::fallback_scan::ScanRequest = serde_json::from_slice(&input)?;
    let response = agrep_core::fallback_scan::run_verified_scan(&data_dir()?, request);
    let mut output = std::io::stdout().lock();
    serde_json::to_writer(&mut output, &response)?;
    output.write_all(b"\n")?;
    Ok(())
}

fn catch_protocol_panic<T>(operation: impl FnOnce() -> T) -> Result<T, ()> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(operation)).map_err(|_| ())
}

fn boundary_rank_serve_cmd() -> anyhow::Result<()> {
    use std::io::{BufRead, Write};

    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut input = stdin.lock();
    let mut output = stdout.lock();
    let mut line = String::new();

    loop {
        line.clear();
        if input.read_line(&mut line)? == 0 {
            break;
        }
        match catch_protocol_panic(|| -> anyhow::Result<_> {
            let request: agrep_core::boundary_rank::BatchRequest = serde_json::from_str(&line)?;
            Ok(agrep_core::boundary_rank::evaluate_batch(request)?)
        }) {
            Ok(Ok(response)) => serde_json::to_writer(&mut output, &response)?,
            Ok(Err(error)) => serde_json::to_writer(
                &mut output,
                &serde_json::json!({"error": error.to_string()}),
            )?,
            Err(()) => output.write_all(br#"{"error":"boundary-rank request panicked"}"#)?,
        }
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}

fn semantic_q8_build_cmd(
    embeddings: &str,
    meta: &str,
    groups: &str,
    output_dir: &str,
    numeric_groups: bool,
) -> anyhow::Result<()> {
    refuse_protected_write_target(Path::new(output_dir), "semantic-q8-build")?;
    require_semantic_q8_output_ownership(Path::new(output_dir))?;
    let result = agrep_core::semantic_q8::build(embeddings, meta, output_dir)?;
    let group_map = if numeric_groups {
        agrep_core::semantic_q8::build_numeric_group_map(
            groups,
            output_dir,
            &result.f32_generation,
            result.rows,
        )?
    } else {
        agrep_core::semantic_q8::build_group_map(
            groups,
            output_dir,
            &result.f32_generation,
            result.rows,
        )?
    };
    serde_json::to_writer(
        std::io::stdout().lock(),
        &serde_json::json!({
            "version": result.version,
            "score_kind": result.score_kind,
            "f32_generation": result.f32_generation,
            "rows": result.rows,
            "dim": result.dim,
            "artifact": result.artifact,
            "artifact_size": result.artifact_size,
            "checksum": result.checksum,
            "group_version": group_map.version,
            "group_artifact": group_map.artifact,
            "group_artifact_size": group_map.artifact_size,
            "group_count": group_map.groups,
            "group_checksum": group_map.checksum,
        }),
    )?;
    println!();
    Ok(())
}

enum SemanticQ8Source {
    Legacy {
        matrix: agrep_core::semantic_q8::Q8Matrix,
        groups: Option<agrep_core::semantic_q8::GroupMap>,
    },
    Set(agrep_core::semantic_q8::SegmentSet),
}

impl SemanticQ8Source {
    fn rows(&self) -> usize {
        match self {
            Self::Legacy { matrix, .. } => matrix.rows(),
            Self::Set(set) => set.rows(),
        }
    }

    fn dim(&self) -> usize {
        match self {
            Self::Legacy { matrix, .. } => matrix.dim(),
            Self::Set(set) => set.dim(),
        }
    }

    fn generation(&self) -> [u8; 16] {
        match self {
            Self::Legacy { matrix, .. } => matrix.generation(),
            Self::Set(set) => set.generation(),
        }
    }

    fn scores(&self, query: &[f32]) -> Result<Vec<f32>, agrep_core::semantic_q8::Q8Error> {
        match self {
            Self::Legacy { matrix, .. } => matrix.scores(query),
            Self::Set(set) => set.scores(query),
        }
    }

    fn top_scores(
        &self,
        query: &[f32],
        k: usize,
        eligibility: Option<&[u8]>,
    ) -> Result<Vec<(u64, f32)>, agrep_core::semantic_q8::Q8Error> {
        match self {
            Self::Legacy { matrix, .. } => eligibility.map_or_else(
                || matrix.top_scores(query, k),
                |bits| matrix.top_scores_eligible(query, k, bits),
            ),
            Self::Set(set) => eligibility.map_or_else(
                || set.top_scores(query, k),
                |bits| set.top_scores_eligible(query, k, bits),
            ),
        }
    }

    fn top_group_scores(
        &self,
        query: &[f32],
        k: usize,
        heads: usize,
        eligibility: Option<&[u8]>,
    ) -> Result<Vec<(u64, f32)>, agrep_core::semantic_q8::Q8Error> {
        match self {
            Self::Legacy { matrix, groups } => groups.as_ref().map_or_else(
                || {
                    Err(agrep_core::semantic_q8::Q8Error::Format(
                        "q8 group map is unavailable".into(),
                    ))
                },
                |groups| {
                    eligibility.map_or_else(
                        || matrix.top_group_scores(groups, query, k, heads),
                        |bits| matrix.top_group_scores_eligible(groups, query, k, heads, bits),
                    )
                },
            ),
            Self::Set(set) => eligibility.map_or_else(
                || set.top_group_scores(query, k, heads),
                |bits| set.top_group_scores_eligible(query, k, heads, bits),
            ),
        }
    }
}

fn semantic_q8_serve_cmd(
    artifact: Option<&str>,
    group_artifact: Option<&str>,
    set_path: Option<&str>,
) -> anyhow::Result<()> {
    use std::io::{Read, Write};

    const READY_MAGIC: &[u8; 4] = b"AQ8R";
    const QUERY_MAGIC: &[u8; 4] = b"AQ8Q";
    const TOP_MAGIC: &[u8; 4] = b"AQ8K";
    const GROUP_TOP_MAGIC: &[u8; 4] = b"AQ8G";
    const FILTER_TOP_MAGIC: &[u8; 4] = b"AQ8F";
    const FILTER_GROUP_TOP_MAGIC: &[u8; 4] = b"AQ8H";
    const SCORE_MAGIC: &[u8; 4] = b"AQ8S";
    const CANDIDATE_MAGIC: &[u8; 4] = b"AQ8T";
    const PROTOCOL: u32 = 1;
    const MAX_CANDIDATES: usize = 4096;

    let source = match (artifact, set_path) {
        (Some(artifact), None) => {
            let matrix = agrep_core::semantic_q8::Q8Matrix::open(artifact)
                .map_err(|error| anyhow::anyhow!("q8 artifact open: {error}"))?;
            let groups = group_artifact
                .map(agrep_core::semantic_q8::GroupMap::open)
                .transpose()
                .map_err(|error| anyhow::anyhow!("q8 group map open: {error}"))?;
            if let Some(groups) = &groups {
                if groups.rows() != matrix.rows() || groups.generation() != matrix.generation() {
                    anyhow::bail!("q8 group map does not match the matrix");
                }
            }
            SemanticQ8Source::Legacy { matrix, groups }
        }
        (None, Some(set_path)) if group_artifact.is_none() => SemanticQ8Source::Set(
            agrep_core::semantic_q8::SegmentSet::open(set_path)
                .map_err(|error| anyhow::anyhow!("q8 segment set open: {error}"))?,
        ),
        _ => anyhow::bail!("exactly one of --artifact or --set is required"),
    };
    let dim = u32::try_from(source.dim())?;
    let rows = u64::try_from(source.rows())?;
    let generation = source.generation();
    let mut output = std::io::stdout().lock();
    output.write_all(READY_MAGIC)?;
    output.write_all(&PROTOCOL.to_le_bytes())?;
    output.write_all(&rows.to_le_bytes())?;
    output.write_all(&dim.to_le_bytes())?;
    output.write_all(&generation)?;
    output.flush()?;

    let mut input = std::io::stdin().lock();
    let mut magic = [0u8; 4];
    loop {
        match input.read_exact(&mut magic) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => break,
            Err(error) => return Err(error.into()),
        }
        let top = magic == *TOP_MAGIC
            || magic == *GROUP_TOP_MAGIC
            || magic == *FILTER_TOP_MAGIC
            || magic == *FILTER_GROUP_TOP_MAGIC;
        let grouped = magic == *GROUP_TOP_MAGIC || magic == *FILTER_GROUP_TOP_MAGIC;
        let filtered = magic == *FILTER_TOP_MAGIC || magic == *FILTER_GROUP_TOP_MAGIC;
        if magic != *QUERY_MAGIC && !top {
            anyhow::bail!("q8 request has unknown magic");
        }
        let mut request_header = [0u8; 8];
        input.read_exact(&mut request_header)?;
        let request_version = u32::from_le_bytes(request_header[..4].try_into().unwrap());
        let request_dim = u32::from_le_bytes(request_header[4..].try_into().unwrap());
        let mut raw_k = [0u8; 4];
        let requested_k = if top {
            input.read_exact(&mut raw_k)?;
            usize::try_from(u32::from_le_bytes(raw_k))?
        } else {
            0
        };
        let requested_heads = if grouped {
            input.read_exact(&mut raw_k)?;
            usize::try_from(u32::from_le_bytes(raw_k))?
        } else {
            1
        };
        let mut request_generation = [0u8; 16];
        input.read_exact(&mut request_generation)?;
        let request_bytes = usize::try_from(request_dim)?
            .checked_mul(4)
            .ok_or_else(|| anyhow::anyhow!("q8 request length overflow"))?;
        if request_bytes > 16_384 * 4 {
            anyhow::bail!("q8 request dimension exceeds the protocol limit");
        }
        let mut raw_query = vec![0u8; request_bytes];
        input.read_exact(&mut raw_query)?;
        let mut eligibility = if filtered {
            vec![
                0u8;
                source
                    .rows()
                    .checked_add(7)
                    .ok_or_else(|| { anyhow::anyhow!("q8 eligibility length overflow") })?
                    / 8
            ]
        } else {
            Vec::new()
        };
        if filtered {
            input.read_exact(&mut eligibility)?;
        }

        let mut status = 0u32;
        let mut scores = Vec::new();
        let mut candidates = Vec::new();
        if request_version != PROTOCOL
            || request_dim != dim
            || (top
                && (requested_k == 0
                    || requested_heads == 0
                    || requested_k
                        .checked_mul(requested_heads)
                        .is_none_or(|count| count > MAX_CANDIDATES)))
        {
            status = 2;
        } else if request_generation != generation {
            status = 1;
        } else {
            if catch_protocol_panic(|| {
                let query: Vec<f32> = raw_query
                    .chunks_exact(4)
                    .map(|bytes| f32::from_le_bytes(bytes.try_into().unwrap()))
                    .collect();
                if top {
                    let mask = filtered.then_some(eligibility.as_slice());
                    let result = if grouped {
                        source.top_group_scores(&query, requested_k, requested_heads, mask)
                    } else {
                        source.top_scores(&query, requested_k, mask)
                    };
                    match result {
                        Ok(result) => candidates = result,
                        Err(_) => status = 2,
                    }
                } else {
                    match source.scores(&query) {
                        Ok(result) => scores = result,
                        Err(_) => status = 2,
                    }
                }
            })
            .is_err()
            {
                status = 3;
                scores.clear();
                candidates.clear();
            }
        }

        output.write_all(if top { CANDIDATE_MAGIC } else { SCORE_MAGIC })?;
        output.write_all(&PROTOCOL.to_le_bytes())?;
        output.write_all(&status.to_le_bytes())?;
        output.write_all(&0u32.to_le_bytes())?;
        let response_len = if top { candidates.len() } else { scores.len() };
        output.write_all(&(response_len as u64).to_le_bytes())?;
        output.write_all(&generation)?;
        if status == 0 {
            if top {
                for (ordinal, score) in candidates {
                    output.write_all(&ordinal.to_le_bytes())?;
                    output.write_all(&score.to_le_bytes())?;
                }
            } else {
                #[cfg(target_endian = "little")]
                output.write_all(unsafe {
                    std::slice::from_raw_parts(scores.as_ptr().cast::<u8>(), scores.len() * 4)
                })?;
                #[cfg(not(target_endian = "little"))]
                for score in scores {
                    output.write_all(&score.to_le_bytes())?;
                }
            }
        }
        output.flush()?;
    }
    Ok(())
}

/// Presence-only probes for stores we can see but don't parse yet (registry::DETECTED).
fn detect_cmd() -> anyhow::Result<()> {
    let items: Vec<serde_json::Value> = ingest::registry::detected()
        .into_iter()
        .map(|(name, count)| serde_json::json!({ "name": name, "count": count }))
        .collect();
    println!("{}", serde_json::to_string(&items)?);
    Ok(())
}

const STORE_AUDIT_SCHEMA: &str = "agrep.store-audit";
const STORE_AUDIT_VERSION: u32 = 1;
const STORE_AUDIT_BOUNDARY_DOMAIN: &[u8] = b"agrep-store-audit-boundary-v1\0";
const STORE_AUDIT_OUTPUT_MAX_BYTES: usize = 8 * 1024 * 1024;

fn digest_hex(digest: [u8; 32]) -> String {
    use std::fmt::Write as _;

    let mut rendered = String::with_capacity(64);
    for byte in digest {
        write!(&mut rendered, "{byte:02x}").expect("writing to String cannot fail");
    }
    rendered
}

fn source_health_issue(reason: impl std::fmt::Display, path: &Path) -> serde_json::Value {
    serde_json::json!({
        "agent": "all",
        "path": path.to_string_lossy(),
        "kind": "health-record-unreadable",
        "reason": reason.to_string(),
    })
}

fn normalized_durable_store_issues(selection: &str) -> Vec<serde_json::Value> {
    let data = match data_dir() {
        Ok(data) => data,
        Err(error) => {
            return vec![source_health_issue(
                error,
                Path::new("agent-history data directory"),
            )]
        }
    };
    let health_path = data.join(SOURCE_HEALTH_FILE);
    let records = match source_health_records(&data) {
        Ok(records) => records,
        Err(error) => return vec![source_health_issue(error, &health_path)],
    };
    let mut normalized = Vec::with_capacity(records.len());
    for record in records {
        let Some(record) = record.as_object() else {
            normalized.push(source_health_issue(
                "source health record has a non-object issue",
                &health_path,
            ));
            continue;
        };
        let fields = ["agent", "path", "kind", "reason"].map(|key| {
            record
                .get(key)
                .and_then(serde_json::Value::as_str)
                .filter(|value| !value.is_empty())
        });
        let [Some(agent), Some(path), Some(kind), Some(reason)] = fields else {
            normalized.push(source_health_issue(
                "source health record has an invalid issue entry",
                &health_path,
            ));
            continue;
        };
        normalized.push(serde_json::json!({
            "agent": agent,
            "path": path,
            "kind": kind,
            "reason": reason,
        }));
    }
    normalized.sort_by_cached_key(|issue| serde_json::to_string(issue).unwrap_or_default());
    normalized.dedup();
    if selection != "all" {
        normalized.retain(|issue| {
            issue.get("agent").and_then(serde_json::Value::as_str) == Some("all")
                || issue.get("agent").and_then(serde_json::Value::as_str) == Some(selection)
        });
    }
    normalized
}

fn store_audit_payload(
    snapshot_bytes: &[u8],
    selection: &str,
    view: ingest::registry::SourceSnapshotAuditView,
    mut durable_issues: Vec<serde_json::Value>,
) -> anyhow::Result<serde_json::Value> {
    let ingest::registry::SourceSnapshotAuditView {
        paths: source_paths,
        tokens: source_tokens,
        issues: source_issues,
        complete: source_complete,
    } = view;
    if selection != "all" {
        durable_issues.retain(|issue| {
            issue.get("agent").and_then(serde_json::Value::as_str) == Some("all")
                || issue.get("agent").and_then(serde_json::Value::as_str) == Some(selection)
        });
    }
    durable_issues.sort_by_cached_key(|issue| serde_json::to_string(issue).unwrap_or_default());
    durable_issues.dedup();
    let durable_bytes = serde_json::to_vec(&durable_issues)?;

    let mut boundary = Sha256::new();
    boundary.update(STORE_AUDIT_BOUNDARY_DOMAIN);
    boundary.update((snapshot_bytes.len() as u64).to_le_bytes());
    boundary.update(snapshot_bytes);
    boundary.update((durable_bytes.len() as u64).to_le_bytes());
    boundary.update(&durable_bytes);
    let boundary_sha256: [u8; 32] = boundary.finalize().into();

    let mut issues: Vec<_> = source_issues
        .into_iter()
        .map(|issue| {
            serde_json::json!({
                "agent": issue.agent(),
                "path": issue.path(),
                "kind": issue.kind(),
                "reason": issue.reason(),
            })
        })
        .collect();
    let mut paths = Vec::with_capacity(source_paths.len());
    for (index, source) in source_paths.into_iter().enumerate() {
        if let Some(path) = source.path.to_str() {
            paths.push(serde_json::json!({
                "name": source.agent,
                "path": path,
                "stat_key": source.stat_key,
                "identity_sha256": digest_hex(source.identity_sha256),
            }));
        } else {
            issues.push(serde_json::json!({
                "agent": source.agent,
                "path": format!("<unrepresentable-source-path:{index}>"),
                "kind": "path-unrepresentable",
                "reason": "content path is not valid Unicode; its exact native identity remains in the boundary digest",
            }));
        }
    }
    let tokens: Vec<_> = source_tokens
        .into_iter()
        .map(|(name, id, key)| serde_json::json!({"name": name, "id": id, "key": key}))
        .collect();
    issues.extend(durable_issues);
    issues.sort_by_cached_key(|issue| serde_json::to_string(issue).unwrap_or_default());
    issues.dedup();
    let complete = source_complete && issues.is_empty();

    Ok(serde_json::json!({
        "schema": STORE_AUDIT_SCHEMA,
        "version": STORE_AUDIT_VERSION,
        "selection": selection,
        "snapshot_sha256": digest_hex(ingest::registry::source_snapshot_sha256(snapshot_bytes)),
        "boundary_sha256": digest_hex(boundary_sha256),
        "paths": paths,
        "tokens": tokens,
        "issues": issues,
        "complete": complete,
    }))
}

fn decoded_store_audit_payload(
    snapshot: &[u8],
    selection: &str,
    durable_issues: Vec<serde_json::Value>,
) -> anyhow::Result<serde_json::Value> {
    let view = ingest::registry::source_snapshot_view(snapshot)
        .ok_or_else(|| anyhow::anyhow!("current source snapshot could not be decoded"))?
        .audit_view()
        .ok_or_else(|| anyhow::anyhow!("source snapshot is not an audit projection"))?;
    store_audit_payload(snapshot, selection, view, durable_issues)
}

fn collect_store_audit_payload(selection: &str) -> anyhow::Result<serde_json::Value> {
    let snapshot = ingest::registry::source_audit_snapshot(selection)?;
    decoded_store_audit_payload(
        &snapshot,
        selection,
        normalized_durable_store_issues(selection),
    )
}

fn serialize_store_audit_payload(
    payload: &serde_json::Value,
    max_bytes: usize,
) -> anyhow::Result<Vec<u8>> {
    let mut encoded = serde_json::to_vec(payload)?;
    anyhow::ensure!(
        encoded
            .len()
            .checked_add(1)
            .is_some_and(|size| size <= max_bytes),
        "audit store snapshot output exceeds the {} byte limit",
        max_bytes
    );
    encoded.push(b'\n');
    Ok(encoded)
}

/// Store presence + newest mtime per registered adapter (registry::stores), or - with
/// --paths - every content file each adapter would discover (the audit census input).
fn stores_cmd(paths: bool, tokens: bool, audit: bool, agent: &str) -> anyhow::Result<()> {
    if audit {
        let payload = collect_store_audit_payload(agent)?;
        let encoded = serialize_store_audit_payload(&payload, STORE_AUDIT_OUTPUT_MAX_BYTES)?;
        std::io::stdout().lock().write_all(&encoded)?;
        return Ok(());
    }
    if tokens {
        let mut items = Vec::new();
        for adapter in ingest::registry::ADAPTERS {
            if adapter.fingerprint() != ingest::registry::Fingerprint::Token {
                continue;
            }
            match adapter.intake_tokens() {
                ingest::registry::TokenAvailability::Data(rows) => {
                    items.extend(rows.into_iter().map(|(id, key)| {
                        serde_json::json!({
                            "name": adapter.name(), "id": id, "key": key,
                            "state": "token",
                        })
                    }));
                }
                ingest::registry::TokenAvailability::Empty => {}
                ingest::registry::TokenAvailability::Unreadable(issues) => {
                    items.extend(issues.into_iter().map(|issue| {
                        serde_json::json!({
                            "name": adapter.name(),
                            "path": issue.path,
                            "state": "source-unreadable",
                            "kind": "token-census-unreadable",
                            "reason": issue.reason,
                        })
                    }));
                }
            }
        }
        println!("{}", serde_json::to_string(&items)?);
        return Ok(());
    }
    let durable_issues = match data_dir() {
        Ok(data) => match source_health_records(&data) {
            Ok(issues) => issues,
            Err(error) => vec![serde_json::json!({
                "agent": "all",
                "path": data.join(SOURCE_HEALTH_FILE).to_string_lossy(),
                "kind": "health-record-unreadable",
                "reason": error.to_string(),
            })],
        },
        Err(error) => vec![serde_json::json!({
            "agent": "all",
            "path": "agent-history data directory",
            "kind": "health-record-unreadable",
            "reason": error.to_string(),
        })],
    };
    if paths {
        let mut items = Vec::new();
        for diagnostic in ingest::registry::store_diagnostics() {
            items.extend(diagnostic.paths().map(|path| {
                serde_json::json!({
                    "name": diagnostic.name(), "path": path, "state": "available"
                })
            }));
            items.extend(diagnostic.issues().iter().map(|issue| {
                serde_json::json!({
                    "name": diagnostic.name(),
                    "path": issue.path(),
                    "state": "source-unreadable",
                    "kind": issue.kind(),
                    "reason": issue.reason(),
                })
            }));
        }
        items.extend(durable_issues.iter().map(|issue| {
            serde_json::json!({
                "name": issue.get("agent").and_then(|value| value.as_str()).unwrap_or("all"),
                "path": issue.get("path").and_then(|value| value.as_str()).unwrap_or(""),
                "state": "source-unreadable",
                "kind": issue.get("kind").and_then(|value| value.as_str()).unwrap_or("source-read-failed"),
                "reason": issue.get("reason").and_then(|value| value.as_str()).unwrap_or("read failed"),
            })
        }));
        println!("{}", serde_json::to_string(&items)?);
        return Ok(());
    }
    let mut items: Vec<serde_json::Value> = ingest::registry::store_diagnostics()
        .into_iter()
        .map(|diagnostic| {
            let issues: Vec<_> = diagnostic
                .issues()
                .iter()
                .map(|issue| {
                    serde_json::json!({
                        "agent": issue.agent(),
                        "path": issue.path(),
                        "kind": issue.kind(),
                        "reason": issue.reason(),
                    })
                })
                .collect();
            serde_json::json!({
                "name": diagnostic.name(),
                "files": diagnostic.files(),
                "newest_mtime_ms": diagnostic.newest_mtime_ms(),
                "mtime_tracks_content": diagnostic.mtime_tracks_content(),
                "state": diagnostic.state(),
                "issues": issues,
            })
        })
        .collect();
    apply_durable_store_issues(&mut items, durable_issues);
    println!("{}", serde_json::to_string(&items)?);
    Ok(())
}

fn apply_durable_store_issues(
    items: &mut Vec<serde_json::Value>,
    durable_issues: Vec<serde_json::Value>,
) {
    for issue in durable_issues {
        let agent = issue
            .get("agent")
            .and_then(|value| value.as_str())
            .unwrap_or("all");
        if agent == "all" && !items.is_empty() {
            for item in items.iter_mut() {
                item["state"] = serde_json::json!("source-unreadable");
                if let Some(issues) = item
                    .get_mut("issues")
                    .and_then(|value| value.as_array_mut())
                {
                    if !issues.contains(&issue) {
                        issues.push(issue.clone());
                    }
                }
            }
            continue;
        }
        if let Some(item) = items
            .iter_mut()
            .find(|item| item.get("name").and_then(|value| value.as_str()) == Some(agent))
        {
            item["state"] = serde_json::json!("source-unreadable");
            if let Some(issues) = item
                .get_mut("issues")
                .and_then(|value| value.as_array_mut())
            {
                if !issues.contains(&issue) {
                    issues.push(issue);
                }
            }
        } else {
            items.push(serde_json::json!({
                "name": agent,
                "files": 0,
                "newest_mtime_ms": null,
                "state": "source-unreadable",
                "issues": [issue],
            }));
        }
    }
}

const SOURCE_HEALTH_FILE: &str = ".source-health.json";
const SOURCE_HEALTH_MAX_BYTES: u64 = 1024 * 1024;

fn source_health_records(data: &Path) -> anyhow::Result<Vec<serde_json::Value>> {
    let path = data.join(SOURCE_HEALTH_FILE);
    let Some(body) = ingest::registry::read_bounded_regular_file(&path, SOURCE_HEALTH_MAX_BYTES)?
    else {
        return Ok(Vec::new());
    };
    let value: serde_json::Value = serde_json::from_slice(&body)?;
    let records = value
        .as_object()
        .filter(|record| {
            record.get("code").and_then(|code| code.as_str()) == Some("source-unreadable")
        })
        .and_then(|record| record.get("issues"))
        .and_then(|issues| issues.as_array())
        .filter(|issues| issues.iter().all(serde_json::Value::is_object))
        .cloned()
        .ok_or_else(|| anyhow::anyhow!("source health record is malformed"))?;
    Ok(records)
}

fn write_source_health(data: &Path, mut records: Vec<serde_json::Value>) -> anyhow::Result<()> {
    records.sort_by_cached_key(|record| serde_json::to_string(record).unwrap_or_default());
    records.dedup();
    let body = serde_json::to_vec(&serde_json::json!({
        "code": "source-unreadable",
        "issues": records,
    }))?;
    cache::write_bytes_atomic(&data.join(SOURCE_HEALTH_FILE), &body)
}

fn publish_source_unreadable(
    data: &Path,
    agent: &str,
    issues: &[ingest::registry::SourceIssue],
    runtime_issues: &[agrep_core::ingest_cache::SourceReadIssue],
    fallback_reason: &str,
) -> anyhow::Result<()> {
    let mut records = source_health_records(data)?;
    if agent == "all" {
        records.clear();
    } else {
        records
            .retain(|record| record.get("agent").and_then(|value| value.as_str()) != Some(agent));
    }
    let mut current: Vec<_> = issues
        .iter()
        .map(|issue| {
            serde_json::json!({
                "agent": issue.agent(),
                "path": issue.path(),
                "kind": issue.kind(),
                "reason": issue.reason(),
            })
        })
        .collect();
    current.extend(runtime_issues.iter().map(runtime_source_issue_value));
    if current.is_empty() {
        current.extend(
            ingest::registry::runtime_issue_roots(agent)
                .into_iter()
                .map(|(source_agent, path)| {
                    serde_json::json!({
                        "agent": source_agent,
                        "path": path.to_string_lossy(),
                        "kind": "ingest-incomplete",
                        "reason": fallback_reason,
                    })
                }),
        );
    }
    records.extend(current);
    write_source_health(data, records)
}

fn runtime_source_issue_value(
    issue: &agrep_core::ingest_cache::SourceReadIssue,
) -> serde_json::Value {
    serde_json::json!({
        "agent": issue.agent,
        "path": issue.path.to_string_lossy(),
        "kind": issue.kind,
        "reason": issue.reason,
    })
}

fn first_source_issue_label(
    issues: &[ingest::registry::SourceIssue],
    runtime_issues: &[agrep_core::ingest_cache::SourceReadIssue],
) -> Option<String> {
    if let Some(issue) = issues.first() {
        return Some(format!(
            "agent {}: {} ({}: {})",
            issue.agent(),
            issue.path(),
            issue.kind(),
            issue.reason()
        ));
    }
    runtime_issues.first().map(|issue| {
        format!(
            "agent {}: {} ({}: {})",
            issue.agent,
            issue.path.to_string_lossy(),
            issue.kind,
            issue.reason
        )
    })
}

/// Source defects the snapshot itself serializes and a retry cannot heal: only the user can
/// grant permission or replace an unsupported file. Content unreadable behind a healthy stamp
/// (torn writes, locked stores) is transient and deliberately absent from this list.
fn durable_source_issue(kind: &str) -> bool {
    matches!(
        kind,
        "permission-denied" | "unsupported-link" | "unsupported-file-type" | "invalid-mtime"
    )
}

/// Law 1, shared by every recovery lane: an undecodable parse cache is agrep's own
/// rebuildable artifact, so its discard is disclosed wherever it happens, never dropped.
/// All three lanes (event repair, source retry, --full retention) load through here.
fn disclose_cache_refusal(
    (cache, refusal): (
        IngestCache,
        Option<agrep_core::ingest_cache::CacheDecodeRefusal>,
    ),
) -> (
    IngestCache,
    Option<agrep_core::ingest_cache::CacheDecodeRefusal>,
) {
    if let Some(refusal) = refusal.filter(|refusal| refusal.is_undecodable()) {
        agrep_core::emit::human_line(format_args!(
            "  discarded an undecodable parse cache ({refusal}); rebuilding it from sources"
        ));
    }
    (cache, refusal)
}

fn source_detail_suffix(detail: Option<&str>) -> String {
    detail
        .map(|detail| format!(" ({})", agrep_core::ingest::terminal_safe(detail)))
        .unwrap_or_default()
}

fn clear_source_health(data: &Path, agent: &str) -> anyhow::Result<()> {
    let path = data.join(SOURCE_HEALTH_FILE);
    if agent == "all" {
        return cache::remove_if_exists(&path);
    }
    let mut records = source_health_records(data)?;
    records.retain(|record| record.get("agent").and_then(|value| value.as_str()) != Some(agent));
    if records.is_empty() {
        cache::remove_if_exists(&path)
    } else {
        write_source_health(data, records)
    }
}

fn preserve_signal_age(
    path: &Path,
    previous: Option<std::time::SystemTime>,
    degraded: bool,
) -> anyhow::Result<()> {
    if degraded {
        if let Some(modified) = previous {
            File::options()
                .write(true)
                .open(path)?
                .set_times(fs::FileTimes::new().set_modified(modified))?;
        }
    }
    Ok(())
}

/// Ingest one named adapter (or "all"), retain last-good rows after incomplete reads, dedupe,
/// and normalize. The registry preserves the adapter concurrency contract:
/// cache-driven adapters (claude/codex/opencode) share the per-file cache sequentially while
/// the full-parse adapters (antigravity/kimi/cline) run in a sibling task. Normalization stays
/// here (it is ingest policy - who/model attribution - not store reading).
fn ingest_agent(
    agent: &str,
    cache: &mut IngestCache,
    harness_prefixes: &[String],
) -> anyhow::Result<(Vec<Message>, Vec<Event>, HashSet<String>)> {
    let started = Instant::now();
    let (msgs, events, repaired_sessions) = ingest::registry::collect(agent, cache)?;
    let collected = started.elapsed();
    let msgs = normalize_messages(msgs, harness_prefixes);
    if std::env::var_os("AGREP_DEBUG").is_some() {
        eprintln!(
            "* [agrep ingest] collect {:.1}ms · normalize {:.1}ms",
            collected.as_secs_f64() * 1000.0,
            (started.elapsed() - collected).as_secs_f64() * 1000.0,
        );
    }
    Ok((msgs, events, repaired_sessions))
}

// Classification lives in agrep_core::row_class: one owned artifact shared by
// this normalize pass and the streamed `--emit-rows` row emission.
use agrep_core::row_class::{row_kind, RowKind};

#[inline]
fn set_arc_str(value: &mut Arc<str>, desired: &str) {
    if value.as_ref() != desired {
        *value = Arc::from(desired);
    }
}

fn normalize_messages(msgs: Vec<Message>, harness_prefixes: &[String]) -> Vec<Message> {
    // row_kind is cheap per row: one sequential pass is faster and less scheduler-sensitive
    // than dispatching thousands of tiny rayon jobs.
    let kinds: Vec<RowKind> = msgs
        .iter()
        .map(|message| row_kind(message, harness_prefixes))
        .collect();
    let needs_model_backfill = msgs.iter().zip(&kinds).any(|(m, kind)| {
        matches!(kind, RowKind::User | RowKind::Subagent) && m.model.trim().is_empty()
    });

    type SessionKey = (&'static str, Arc<str>);
    type ModelTimeline = Vec<(u32, Arc<str>)>;
    let mut session_models: HashMap<SessionKey, HashSet<Arc<str>>> = HashMap::new();
    let mut session_timeline: HashMap<SessionKey, ModelTimeline> = HashMap::new();
    // The common case is an adapter that already attached a model to every real row. In that
    // case no lookup below is possible, so avoid hashing/cloning every session and model just
    // to build two maps that would never be read.
    if needs_model_backfill {
        for (m, kind) in msgs.iter().zip(&kinds) {
            let model = m.model.trim();
            if model.is_empty() || !matches!(kind, RowKind::User | RowKind::Subagent) {
                continue;
            }
            session_models
                .entry((m.agent, m.session.clone()))
                .or_default()
                .insert(m.model.clone());
            session_timeline
                .entry((m.agent, m.session.clone()))
                .or_default()
                .push((m.turn, m.model.clone()));
        }
        for models in session_timeline.values_mut() {
            models.sort_by_key(|(turn, _)| *turn);
        }
    }

    msgs.into_iter()
        .zip(kinds)
        .map(|(mut m, kind)| {
            let raw_model_empty = m.model.trim().is_empty();
            let (who, model_source): (&str, &str) = match kind {
                RowKind::Synthetic => ("synthetic", "synthetic"),
                RowKind::Control => {
                    set_arc_str(&mut m.model, "<control>");
                    ("control", "control")
                }
                RowKind::Recap => {
                    set_arc_str(&mut m.model, "<recap>");
                    ("recap", "recap")
                }
                RowKind::Harness => {
                    if raw_model_empty {
                        set_arc_str(&mut m.model, "<harness>");
                        ("harness", "harness")
                    } else {
                        ("harness", "explicit_harness")
                    }
                }
                RowKind::User | RowKind::Subagent => {
                    // side turns are real work: same model attribution as user
                    // turns (their child sessions carry their own model pool)
                    let who = if kind == RowKind::Subagent {
                        "subagent"
                    } else {
                        "user"
                    };
                    if !raw_model_empty {
                        (who, "explicit")
                    } else {
                        let models = session_models.get(&(m.agent, m.session.clone()));
                        match models.map(|s| s.len()).unwrap_or(0) {
                            1 => {
                                m.model = models
                                    .and_then(|s| s.iter().next())
                                    .cloned()
                                    .unwrap_or_else(|| Arc::from(""));
                                (who, "session")
                            }
                            0 => (who, "unknown"),
                            _ => {
                                let timeline = session_timeline.get(&(m.agent, m.session.clone()));
                                if let Some(model) =
                                    timeline.and_then(|rows| temporal_backfill(rows, m.turn))
                                {
                                    m.model = model;
                                    (who, "temporal_session")
                                } else {
                                    (who, "ambiguous_session")
                                }
                            }
                        }
                    }
                }
            };
            // Normalize in place. Parsed/cache rows already carry these labels in the common
            // explicit-user case, so this avoids rebuilding every Message and allocating two
            // short Arc strings per row. A changed classification still overwrites stale labels.
            set_arc_str(&mut m.who, who);
            set_arc_str(&mut m.model_source, model_source);
            m
        })
        .collect()
}

fn temporal_backfill(rows: &[(u32, Arc<str>)], turn: u32) -> Option<Arc<str>> {
    let before = rows
        .iter()
        .rev()
        .find(|(known_turn, _)| *known_turn < turn)
        .map(|(_, model)| model);
    let after = rows
        .iter()
        .find(|(known_turn, _)| *known_turn > turn)
        .map(|(_, model)| model);
    match (before, after) {
        (Some(a), Some(b)) if a == b => Some(a.clone()),
        (None, Some(model)) => Some(model.clone()),
        (Some(model), None) => Some(model.clone()),
        _ => None,
    }
}

/// Order-independent FNV-1a fingerprint of the deduped+normalized message set. Each message
/// hashes its full identity+content; the per-message hashes are combined with `wrapping_add`
/// (commutative) so a run-to-run reordering from the parallel ingest can never masquerade as a
/// change. Computed straight off the in-memory messages, which is far cheaper than serializing
/// them - and serializing IS the write cost we're trying to skip when nothing moved.
fn content_sig(msgs: &[Message]) -> u64 {
    fn fnv(mut h: u64, bytes: &[u8]) -> u64 {
        for &b in bytes {
            h ^= b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        h
    }
    msgs.par_iter()
        .map(|m| {
            let mut h: u64 = 0xcbf29ce484222325;
            h = fnv(h, m.agent.as_bytes());
            h = fnv(h, b"\0");
            h = fnv(h, m.session.as_bytes());
            h = fnv(h, &m.turn.to_le_bytes());
            h = fnv(h, &m.ts.to_le_bytes());
            h = fnv(h, m.text.as_bytes());
            h = fnv(h, m.who.as_bytes());
            h = fnv(h, m.model.as_bytes());
            h = fnv(h, m.model_source.as_bytes());
            h = fnv(h, m.project.as_bytes()); // project is a written column; a relabel must re-sig
            h = fnv(h, m.reply.as_bytes());
            h = fnv(h, &m.reply_chars.to_le_bytes());
            h = fnv(h, &[u8::from(m.side)]);
            h = fnv(h, m.parent.as_bytes()); // sessions.jsonl linkage is derived content too
            h
        })
        .reduce(|| 0u64, |a, b| a.wrapping_add(b))
}

const SOURCE_SNAPSHOT_FILE: &str = ".source_snapshot.bin";
const INGEST_PENDING_FILE: &str = ".ingest_pending.bin";
const SOURCE_ABSENCE_FILE: &str = ".source_absence_pending";
const HARNESS_POLICY_SNAPSHOT_FILE: &str = ".harness_prefixes.snapshot";
const DERIVED_PROOF_FILE: &str = ".derived_generation.json";
const DERIVED_PROOF_VERSION: u32 = 6;
const LEGACY_DERIVED_PROOF_VERSION: u32 = 4;
const HARNESS_POLICY_MAX_BYTES: u64 = 8 * 1024 * 1024;
const SOURCE_SNAPSHOT_MAX_BYTES: u64 = 256 * 1024 * 1024;
const CHANGED_SESSIONS_MAX_BYTES: u64 = 256 * 1024 * 1024;
const DERIVED_PROOF_MAX_BYTES: u64 = 1024 * 1024;
const INGEST_SIGNATURE_MAX_BYTES: u64 = 4096;
const SOURCE_ABSENCE_MAX_BYTES: u64 = 64 * 1024;
const SOURCE_ABSENCE_HEADER: &str = "agrep-source-absence-v1\n";
// Any ingest policy change that can alter derived rows must bump this version: a bump
// voids the source-identical shortcut even when every source file is byte-identical.
const HARNESS_POLICY_HEADER: &[u8] = b"agrep-harness-policy-v4\0";

/// Load the corpus-local classification policy once per index pass. The exact bytes plus a
/// format version are part of the all-hit permission slip, while parsing is done from that same
/// captured generation so a mid-run edit cannot classify rows from a different policy. Missing
/// means an empty policy; every other read/UTF-8 error aborts before derived output mutation.
fn harness_policy() -> anyhow::Result<(Vec<u8>, Vec<String>)> {
    let path = data_dir()?.join("harness_prefixes.txt");
    let (present, bytes) = match read_optional_bytes(&path, HARNESS_POLICY_MAX_BYTES)? {
        Some(bytes) => (1u8, bytes),
        None => (0u8, Vec::new()),
    };
    let body = String::from_utf8(bytes.clone())
        .map_err(|error| anyhow::anyhow!("invalid UTF-8 in {}: {error}", path.display()))?;
    let prefixes = body
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .map(str::to_string)
        .collect();
    let mut snapshot = Vec::with_capacity(HARNESS_POLICY_HEADER.len() + 1 + bytes.len());
    snapshot.extend_from_slice(HARNESS_POLICY_HEADER);
    snapshot.push(present);
    snapshot.extend_from_slice(&bytes);
    Ok((snapshot, prefixes))
}

fn read_source_absence(path: &Path) -> anyhow::Result<Option<HashSet<String>>> {
    let Some(bytes) = read_optional_bytes(path, SOURCE_ABSENCE_MAX_BYTES)? else {
        return Ok(None);
    };
    let Ok(body) = std::str::from_utf8(&bytes) else {
        return Ok(None);
    };
    let Some(rows) = body.strip_prefix(SOURCE_ABSENCE_HEADER) else {
        return Ok(None);
    };
    Ok(Some(
        rows.lines()
            .filter(|agent| !agent.is_empty())
            .map(str::to_string)
            .collect(),
    ))
}

fn write_source_absence(path: &Path, agents: &HashSet<String>) -> anyhow::Result<()> {
    let mut agents: Vec<_> = agents.iter().collect();
    agents.sort_unstable();
    let mut body = SOURCE_ABSENCE_HEADER.as_bytes().to_vec();
    for agent in agents {
        body.extend_from_slice(agent.as_bytes());
        body.push(b'\n');
    }
    cache::write_bytes_atomic(path, &body)
}

/// Re-read the source snapshot after ingest and return it only when it still matches the
/// preflight view. This intentionally does no publication: callers may run the expensive
/// second walk alongside derived-file rendering, but must publish only after every output
/// succeeded. On an output error the pending preflight remains and the next run retries.
enum SourceValidation {
    Valid(Vec<u8>),
    /// Every read succeeded, but sources kept moving after the preflight. The generation built
    /// from the preflight view is itself coherent; the drift is the next run's ordinary work.
    Drifted,
    Unreadable(Vec<ingest::registry::SourceIssue>, String),
    Moved,
}

fn validated_source_snapshot(
    agent: &str,
    before: Option<&[u8]>,
    source_snapshot_safe: bool,
    accept_stable_incomplete: bool,
) -> SourceValidation {
    if !source_snapshot_safe && !accept_stable_incomplete {
        return SourceValidation::Moved;
    }
    let Some(before) = before else {
        return SourceValidation::Moved;
    };
    if !ingest::registry::source_snapshot_complete(before) && !accept_stable_incomplete {
        let issues = ingest::registry::source_snapshot_issues(before);
        return if issues.is_empty() {
            SourceValidation::Moved
        } else {
            SourceValidation::Unreadable(
                issues,
                "a source was unreadable during validation".to_string(),
            )
        };
    }
    match ingest::registry::source_snapshot(agent) {
        // A byte-identical incomplete pair carries the same serialized issue records: the
        // unreadable subset is a stable disclosed fact, not an in-flight condition.
        Ok(after) if after == before => SourceValidation::Valid(after),
        Ok(_) if accept_stable_incomplete => SourceValidation::Moved,
        Ok(after) => {
            let issues = ingest::registry::source_snapshot_issues(&after);
            if issues.is_empty() {
                if source_snapshot_safe && ingest::registry::source_snapshot_complete(&after) {
                    SourceValidation::Drifted
                } else {
                    SourceValidation::Moved
                }
            } else {
                SourceValidation::Unreadable(
                    issues,
                    "a source was unreadable during postflight validation".to_string(),
                )
            }
        }
        Err(_) if accept_stable_incomplete => SourceValidation::Moved,
        Err(error) => SourceValidation::Unreadable(Vec::new(), error.to_string()),
    }
}

enum GenerationValidation {
    Valid((Vec<u8>, Vec<u8>)),
    Unreadable(Vec<ingest::registry::SourceIssue>, String),
    Moved,
}

/// Validate both transcript inputs and the local classification policy. They are published as
/// one logical permission slip: either one moving keeps the pending marker and forces a retry.
fn validated_generation_snapshots(
    agent: &str,
    source_before: Option<&[u8]>,
    policy_before: &[u8],
    source_snapshot_safe: bool,
    accept_stable_incomplete: bool,
) -> GenerationValidation {
    let validation = validated_source_snapshot(
        agent,
        source_before,
        source_snapshot_safe,
        accept_stable_incomplete,
    );
    let source = match validation {
        SourceValidation::Valid(source) => source,
        // Publishing the preflight view instead of retaining pending turns live-writer drift
        // into next-run incremental work rather than permanent crash-recovery rewrites.
        SourceValidation::Drifted => match source_before {
            Some(before) => before.to_vec(),
            None => return GenerationValidation::Moved,
        },
        SourceValidation::Unreadable(issues, reason) => {
            return GenerationValidation::Unreadable(issues, reason)
        }
        SourceValidation::Moved => return GenerationValidation::Moved,
    };
    let Ok((policy, _)) = harness_policy() else {
        return GenerationValidation::Moved;
    };
    if policy != policy_before {
        return GenerationValidation::Moved;
    }
    GenerationValidation::Valid((source, policy))
}

fn generation_for_publication(
    data: &Path,
    agent: &str,
    validation: GenerationValidation,
) -> anyhow::Result<Option<(Vec<u8>, Vec<u8>)>> {
    match validation {
        GenerationValidation::Valid(snapshots) => Ok(Some(snapshots)),
        GenerationValidation::Moved => Ok(None),
        GenerationValidation::Unreadable(issues, reason) => {
            publish_source_unreadable(data, agent, &issues, &[], &reason)?;
            let detail = issues
                .first()
                .map(|issue| format!("{}: {}", issue.path(), issue.reason()))
                .unwrap_or(reason);
            anyhow::bail!(
                "source-unreadable: {}; retained the old generation",
                agrep_core::ingest::terminal_safe(detail)
            )
        }
    }
}

fn read_optional_bytes(path: &std::path::Path, max_bytes: u64) -> anyhow::Result<Option<Vec<u8>>> {
    agrep_core::ingest::registry::read_bounded_regular_file(path, max_bytes)
        .map_err(|error| anyhow::anyhow!("cannot read {}: {error}", path.display()))
}

fn regular_file_has_bytes(path: &Path, expected: &[u8]) -> anyhow::Result<bool> {
    agrep_core::ingest::registry::regular_file_equals(path, expected).map_err(Into::into)
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct DerivedFileProof {
    name: String,
    len: u64,
    modified_ns: u64,
    change_token: agrep_core::ingest::registry::ChangeToken,
    edge_hash: u64,
}

#[derive(Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct DerivedProof {
    version: u32,
    signature: String,
    files: Vec<DerivedFileProof>,
}

fn derived_names() -> [&'static str; 7] {
    [
        "messages.jsonl",
        "replies.jsonl",
        "sessions.jsonl",
        cache::SESSION_FAMILY_META_FILE,
        agrep_core::boundary_stats::FILE_NAME,
        agrep_core::boundary_stats::CACHE_FILE_NAME,
        "event_stats.json",
    ]
}

fn legacy_derived_names() -> [&'static str; 6] {
    [
        "messages.jsonl",
        "replies.jsonl",
        "sessions.jsonl",
        agrep_core::boundary_stats::FILE_NAME,
        agrep_core::boundary_stats::CACHE_FILE_NAME,
        "event_stats.json",
    ]
}

fn derived_file_proof_from_snapshot(
    name: &str,
    snapshot: &agrep_core::ingest::registry::RegularFileEdgeSnapshot,
) -> anyhow::Result<DerivedFileProof> {
    let modified_ns = snapshot
        .modified
        .duration_since(UNIX_EPOCH)
        .map_err(|error| anyhow::anyhow!("invalid mtime for {name}: {error}"))?
        .as_nanos()
        .min(u64::MAX as u128) as u64;
    let mut bytes = Vec::with_capacity(snapshot.head.len() + snapshot.tail.len() + 8);
    bytes.extend_from_slice(&snapshot.len.to_le_bytes());
    bytes.extend_from_slice(&snapshot.head);
    bytes.extend_from_slice(&snapshot.tail);
    let mut edge_hash = 0xcbf29ce484222325u64;
    for byte in bytes {
        edge_hash ^= byte as u64;
        edge_hash = edge_hash.wrapping_mul(0x100000001b3);
    }
    Ok(DerivedFileProof {
        name: name.to_string(),
        len: snapshot.len,
        modified_ns,
        change_token: snapshot.change_token.clone(),
        edge_hash,
    })
}

fn derived_file_proof(data: &Path, name: &str) -> anyhow::Result<DerivedFileProof> {
    let path = data.join(name);
    let snapshot = agrep_core::ingest::registry::regular_file_edge_snapshot(&path, 512)
        .map_err(|error| anyhow::anyhow!("cannot read {}: {error}", path.display()))?
        .ok_or_else(|| anyhow::anyhow!("derived output is missing: {}", path.display()))?;
    derived_file_proof_from_snapshot(name, &snapshot)
}

fn derived_proof(data: &Path, signature: &str) -> anyhow::Result<DerivedProof> {
    let files = derived_names()
        .into_iter()
        .map(|name| derived_file_proof(data, name))
        .collect::<anyhow::Result<Vec<_>>>()?;
    Ok(DerivedProof {
        version: DERIVED_PROOF_VERSION,
        signature: signature.trim().to_string(),
        files,
    })
}

fn legacy_derived_proof(data: &Path, signature: &str) -> anyhow::Result<DerivedProof> {
    let files = legacy_derived_names()
        .into_iter()
        .map(|name| derived_file_proof(data, name))
        .collect::<anyhow::Result<Vec<_>>>()?;
    Ok(DerivedProof {
        version: LEGACY_DERIVED_PROOF_VERSION,
        signature: signature.trim().to_string(),
        files,
    })
}

fn derived_generation_valid_with(
    data: &Path,
    signature: &str,
    mut current_proof: impl FnMut() -> anyhow::Result<DerivedProof>,
) -> bool {
    let path = data.join(DERIVED_PROOF_FILE);
    let Some(expected) =
        agrep_core::ingest::registry::read_bounded_regular_file(&path, DERIVED_PROOF_MAX_BYTES)
            .ok()
            .flatten()
            .and_then(|bytes| serde_json::from_slice::<DerivedProof>(&bytes).ok())
    else {
        return false;
    };
    if expected.version != DERIVED_PROOF_VERSION || expected.signature != signature.trim() {
        return false;
    }
    let Ok(first) = current_proof() else {
        return false;
    };
    if first != expected {
        return false;
    }
    current_proof()
        .map(|confirmed| confirmed == first)
        .unwrap_or(false)
}

fn derived_generation_valid(data: &Path, signature: &str) -> bool {
    derived_generation_valid_with(data, signature, || derived_proof(data, signature))
}

fn legacy_derived_generation_valid(data: &Path, signature: &str) -> bool {
    // A current publication with a damaged proof is not an upgrade candidate.
    // The narrow window is the exact v4 six-file publication with no family
    // metadata; validate its outputs twice before preserving their identities.
    if !matches!(
        read_optional_bytes(
            &data.join(cache::SESSION_FAMILY_META_FILE),
            DERIVED_PROOF_MAX_BYTES
        ),
        Ok(None)
    ) {
        return false;
    }
    let path = data.join(DERIVED_PROOF_FILE);
    let Some(expected) =
        agrep_core::ingest::registry::read_bounded_regular_file(&path, DERIVED_PROOF_MAX_BYTES)
            .ok()
            .flatten()
            .and_then(|bytes| serde_json::from_slice::<DerivedProof>(&bytes).ok())
    else {
        return false;
    };
    if expected.version != LEGACY_DERIVED_PROOF_VERSION || expected.signature != signature.trim() {
        return false;
    }
    let Ok(first) = legacy_derived_proof(data, signature) else {
        return false;
    };
    first == expected
        && legacy_derived_proof(data, signature)
            .map(|confirmed| confirmed == first)
            .unwrap_or(false)
}

fn legacy_protected_outputs_valid(data: &Path, signature: &str) -> bool {
    let path = data.join(DERIVED_PROOF_FILE);
    let Some(mut expected) =
        agrep_core::ingest::registry::read_bounded_regular_file(&path, DERIVED_PROOF_MAX_BYTES)
            .ok()
            .flatten()
            .and_then(|bytes| serde_json::from_slice::<DerivedProof>(&bytes).ok())
    else {
        return false;
    };
    if expected.version != LEGACY_DERIVED_PROOF_VERSION
        || expected.signature != signature.trim()
        || expected.files.len() != legacy_derived_names().len()
        || expected.files.last().map(|proof| proof.name.as_str()) != Some("event_stats.json")
    {
        return false;
    }
    // Event repair is allowed to republish event_stats.json during this run.
    // The transcript/session/boundary artifacts are the legacy publication
    // whose identities must remain byte-for-byte proven.
    expected.files.pop();
    let current = || {
        let mut proof = legacy_derived_proof(data, signature)?;
        proof.files.pop();
        Ok::<_, anyhow::Error>(proof)
    };
    let Ok(first) = current() else {
        return false;
    };
    first == expected
        && current()
            .map(|confirmed| confirmed == first)
            .unwrap_or(false)
}

fn publish_derived_proof(data: &Path, signature: &str) -> anyhow::Result<()> {
    let proof = derived_proof(data, signature)?;
    cache::write_bytes_atomic(&data.join(DERIVED_PROOF_FILE), &serde_json::to_vec(&proof)?)
}

struct GenerationPublication<'a> {
    data: &'a Path,
    sig_path: &'a Path,
    signature: &'a str,
    complete: bool,
    parse_cache: &'a IngestCache,
    messages: &'a [Message],
    repaired_sessions: &'a HashSet<String>,
    preserve_signal_if_unchanged: bool,
}

fn ensure_derived_write_ownership(data: &Path) -> anyhow::Result<()> {
    match derived_write_ownership(data) {
        DerivedWriteOwnership::Adoption => {
            if let Some(reason) = adoption_daemon_fence(data) {
                anyhow::bail!("{reason}; refusing legacy adoption after source parsing");
            }
        }
        DerivedWriteOwnership::Current => {
            if let Some(reason) = adoption_daemon_fence(data) {
                anyhow::bail!("{reason}; refusing derived publication after source parsing");
            }
        }
        DerivedWriteOwnership::Foreign(reason) | DerivedWriteOwnership::Refused(reason) => {
            anyhow::bail!("{reason}; refusing derived publication after source parsing");
        }
        DerivedWriteOwnership::PostAdoptionClobber(reason) => {
            anyhow::bail!("{reason}; refusing reclobbered publication after source parsing");
        }
    }
    Ok(())
}

fn commit_generation_cache(
    data: &Path,
    event_dir: &Path,
    agents: &[&str],
    staged_cache: StagedCache,
    invalidate_events: bool,
) -> anyhow::Result<()> {
    ensure_derived_write_ownership(data)?;
    if invalidate_events {
        // A crash after cache advancement must force event payload reconstruction on retry.
        cache::invalidate_events_complete(event_dir, agents)?;
    }
    staged_cache.commit()?;
    publish_derived_owner(data)
}

fn publish_generation_markers(publication: GenerationPublication<'_>) -> anyhow::Result<bool> {
    let GenerationPublication {
        data,
        sig_path,
        signature,
        complete,
        parse_cache,
        messages,
        repaired_sessions,
        preserve_signal_if_unchanged,
    } = publication;
    ensure_derived_write_ownership(data)?;
    let started = Instant::now();
    write_changed_sessions(data, complete, parse_cache, messages, repaired_sessions)?;
    let delta_done = started.elapsed();
    publish_derived_proof(data, signature)?;
    let proof_done = started.elapsed();
    let signal_written =
        !(preserve_signal_if_unchanged && regular_file_has_bytes(sig_path, signature.as_bytes())?);
    if signal_written {
        cache::write_bytes_atomic(sig_path, signature.as_bytes())?;
    }
    let signature_done = started.elapsed();
    if std::env::var_os("AGREP_DEBUG").is_some() {
        eprintln!(
            "* [agrep ingest] delta {:.1}ms · proof {:.1}ms · signature {:.1}ms",
            delta_done.as_secs_f64() * 1000.0,
            (proof_done - delta_done).as_secs_f64() * 1000.0,
            (signature_done - proof_done).as_secs_f64() * 1000.0,
        );
    }
    Ok(signal_written)
}

#[derive(Debug, Default, Eq, PartialEq)]
struct SourcePublishOutcome {
    policy_written: bool,
    pending_promoted: bool,
}

/// Commit the validated source generation last. The snapshot is the permission slip for the
/// next all-hit shortcut, so it may never precede messages/replies/sessions/events publication.
/// A failed validation deliberately preserves both the last published snapshot and the pending
/// preflight: the latter disables the shortcut without throwing away the incremental baseline.
fn publish_source_snapshot(
    snapshots: Option<(Vec<u8>, Vec<u8>)>,
    path: &std::path::Path,
    policy_path: &std::path::Path,
    pending_path: &std::path::Path,
) -> anyhow::Result<SourcePublishOutcome> {
    publish_source_snapshot_with(
        snapshots,
        path,
        policy_path,
        pending_path,
        cache::promote_file_atomic,
    )
}

fn publish_source_snapshot_with<F>(
    snapshots: Option<(Vec<u8>, Vec<u8>)>,
    path: &Path,
    policy_path: &Path,
    pending_path: &Path,
    promote: F,
) -> anyhow::Result<SourcePublishOutcome>
where
    F: FnOnce(&Path, &Path) -> anyhow::Result<()>,
{
    let started = Instant::now();
    let has_snapshots = snapshots.is_some();
    let mut outcome = SourcePublishOutcome::default();
    if let Some((source, policy)) = snapshots {
        if !regular_file_has_bytes(policy_path, &policy)? {
            cache::write_bytes_atomic(policy_path, &policy)?;
            outcome.policy_written = true;
        }
        if regular_file_has_bytes(pending_path, &source)? {
            promote(pending_path, path)?;
            outcome.pending_promoted = true;
        } else {
            cache::write_bytes_atomic(path, &source)?;
            cache::remove_if_exists(pending_path)?;
        }
    }
    if std::env::var_os("AGREP_DEBUG").is_some() {
        eprintln!(
            "* [agrep ingest] source policy={} · snapshot={} ({:.1}ms)",
            if !has_snapshots {
                "held"
            } else if outcome.policy_written {
                "write"
            } else {
                "reuse"
            },
            if !has_snapshots {
                "held"
            } else if outcome.pending_promoted {
                "promote"
            } else {
                "write"
            },
            started.elapsed().as_secs_f64() * 1000.0,
        );
    }
    Ok(outcome)
}

/// Accumulate the corpus refresh delta. The event-only path needs this too: advancing the
/// event-store generation without a readable delta forces corpusdb to rebuild both FTS tables.
fn write_changed_sessions(
    data: &std::path::Path,
    complete: bool,
    cache: &IngestCache,
    msgs: &[Message],
    repaired_sessions: &HashSet<String>,
) -> anyhow::Result<()> {
    let changed_path = data.join(".changed_sessions");
    if complete {
        return cache::write_bytes_atomic(&changed_path, b"*\n");
    }
    let mut set: HashSet<String> =
        match read_optional_bytes(&changed_path, CHANGED_SESSIONS_MAX_BYTES)? {
            Some(body) => String::from_utf8(body)
                .map_err(|error| {
                    anyhow::anyhow!("invalid UTF-8 in {}: {error}", changed_path.display())
                })?
                .lines()
                .map(str::to_string)
                .collect(),
            None => HashSet::new(),
        };
    if set.contains("*") {
        return Ok(());
    }
    set.extend(cache.touched.iter().cloned());
    set.extend(repaired_sessions.iter().cloned());
    for m in msgs {
        if matches!(m.agent, "antigravity" | "kimi" | "cline") {
            set.insert(m.session.to_string());
        }
    }
    let mut v: Vec<&String> = set.iter().collect();
    v.sort();
    let body: String = v.iter().map(|s| format!("{s}\n")).collect();
    cache::write_bytes_atomic(&changed_path, body.as_bytes())
}

fn invalidate_turn_enrichment(data: &std::path::Path) -> anyhow::Result<()> {
    cache::remove_if_exists(&data.join("emotions.jsonl"))?;
    cache::remove_if_exists(&data.join("vibe").join("index.json"))?;
    cache::remove_if_exists(&data.join(".reindex.sig"))?;
    Ok(())
}

fn entry_absent(path: &Path) -> bool {
    matches!(
        fs::symlink_metadata(path),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound
    )
}

fn unlocked_owner_has_no_retained_corpus(data: &Path) -> bool {
    match read_optional_bytes(&data.join(DERIVED_OWNER_FILE), DERIVED_OWNER_MAX_BYTES) {
        Ok(Some(bytes)) => serde_json::from_slice::<DerivedOwnerRecord>(&bytes)
            .is_ok_and(|owner| owner.retained_corpus_db.is_none()),
        Ok(None) => true,
        Err(_) => false,
    }
}

fn unlocked_warm_skip(data: &Path, agent: &str) -> Option<(usize, u128)> {
    let started = Instant::now();
    if [INGEST_PENDING_FILE, SOURCE_ABSENCE_FILE, SOURCE_HEALTH_FILE]
        .into_iter()
        .any(|name| !entry_absent(&data.join(name)))
    {
        return None;
    }
    if !unlocked_owner_has_no_retained_corpus(data) {
        return None;
    }
    if derived_write_ownership(data) != DerivedWriteOwnership::Current {
        return None;
    }
    let (policy, _) = harness_policy().ok()?;
    let source = ingest::registry::source_snapshot(agent).ok()?;
    if !ingest::registry::source_snapshot_complete(&source)
        || !ingest::registry::source_snapshot_issues(&source).is_empty()
    {
        return None;
    }
    let sig_snapshot = ingest::registry::read_bounded_regular_file_snapshot(
        &data.join(".ingest.sig"),
        INGEST_SIGNATURE_MAX_BYTES,
    )
    .ok()??;
    let signature = String::from_utf8(sig_snapshot.bytes).ok()?;
    if !derived_generation_valid(data, &signature) {
        return None;
    }
    let count = signature.trim().split_once(':')?.0.parse::<usize>().ok()?;
    let published_source =
        read_optional_bytes(&data.join(SOURCE_SNAPSHOT_FILE), SOURCE_SNAPSHOT_MAX_BYTES).ok()??;
    let published_policy = read_optional_bytes(
        &data.join(HARNESS_POLICY_SNAPSHOT_FILE),
        HARNESS_POLICY_MAX_BYTES,
    )
    .ok()??;
    if source != published_source || policy != published_policy {
        return None;
    }
    let agents: Vec<&str> = ingest::registry::run_agents(agent);
    if cache::read_only_event_publication_authority(&data.join("events"), &agents)
        .ok()?
        .is_none()
        || derived_write_ownership(data) != DerivedWriteOwnership::Current
    {
        return None;
    }
    Some((count, started.elapsed().as_millis()))
}

/// `agrep index` writes the canonical corpus; external enrichment workers repair optional data.
///
/// Sig-skip invariants: when the content fingerprint of the in-memory messages matches the last
/// run's AND the published index is already on disk, every derived file
/// (messages/replies/sessions/events) would be byte-identical - so the whole write cycle is
/// skipped (Windows tmp+rename churn plus the corpusdb FTS rebuild it would trigger). The CLI
/// re-runs this ingest before a search to bound staleness; this skip is what makes that cheap
/// (~stat+hash, no writes). The sig file is rewritten even on a skip, so its mtime is the CLI's
/// "last checked" clock. Skip only when the WHOLE published set is on disk - a matching sig with
/// a derived file missing (deleted out-of-band) would otherwise never regenerate it short of
/// --full.
fn index_cmd(agent: &str, full: bool) -> anyhow::Result<()> {
    let t0 = Instant::now();
    let data = data_dir()?;
    if data_dir_is_protected(&data) {
        disclose_read_only_ownership(
            "AGREP_DATA_READONLY protects this data directory from ingest writes",
        );
        return Ok(());
    }
    if let Ok(reason) = std::env::var(DERIVED_WRITER_IDENTITY_BLOCKED_ENV) {
        if !reason.trim().is_empty() {
            disclose_read_only_ownership(&format!(
                "derived writer identity is unavailable: {reason}"
            ));
            return Ok(());
        }
    }
    if let Some(reason) = agrep_core::ingest_cache::current_cache_writer_identity_refusal() {
        disclose_read_only_ownership(&format!("derived writer identity is unavailable: {reason}"));
        return Ok(());
    }
    // A rollback journal blocks the ownership probe itself and nothing on the
    // box would ever clear it. Reclaim it first, under the index lock the
    // writer takes anyway, then read ownership for real.
    let corpus = data.join("corpus.db");
    let corpus_missing = matches!(
        agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus, 0),
        Ok(None)
    );
    if !corpus_missing && hot_rollback_journal(&corpus).map_or(true, |journal| journal.is_some()) {
        let mut journal_lock = acquire_index_lock()?;
        let reclaimed = reclaim_cold_rollback_journal(&corpus);
        journal_lock.release()?;
        if let Err(detail) = reclaimed {
            disclose_read_only_ownership(&detail);
            return Ok(());
        }
    }
    let initial_ownership = derived_write_ownership(&data);
    let ownership_was_current = initial_ownership == DerivedWriteOwnership::Current;
    let adoption_claim = match initial_ownership {
        DerivedWriteOwnership::Refused(reason)
        | DerivedWriteOwnership::PostAdoptionClobber(reason) => {
            disclose_read_only_ownership(&reason);
            return Ok(());
        }
        // A foreign family may only converge under the index lock; the claim proves no
        // live daemon holds it, and the locked section performs the successor takeover.
        DerivedWriteOwnership::Foreign(reason) => match AdoptionClaim::acquire(&data) {
            Ok(claim) => Some(claim),
            Err(fence) => {
                disclose_read_only_ownership(&format!("{reason}; {fence}"));
                return Ok(());
            }
        },
        DerivedWriteOwnership::Adoption => match AdoptionClaim::acquire(&data) {
            Ok(claim) => Some(claim),
            Err(reason) => {
                disclose_read_only_ownership(&reason);
                return Ok(());
            }
        },
        DerivedWriteOwnership::Current => match AdoptionClaim::acquire(&data) {
            Ok(claim) => Some(claim),
            Err(reason) => {
                disclose_read_only_ownership(&reason);
                return Ok(());
            }
        },
    };
    let lock_path = data.join(".index.lock");
    if ownership_was_current && !full && !agrep_core::emit::on() {
        let holder_label = format!(
            "corpusdb:{}",
            agrep_core::ingest_cache::current_cache_writer_build_id()
        );
        if let Ok(Some(holder)) = index_lock::observe_live_holder(&lock_path, &holder_label) {
            if let Some((count, source_ms)) = unlocked_warm_skip(&data, agent) {
                if index_lock::live_holder_unchanged(&lock_path, &holder)
                    && derived_write_ownership(&data) == DerivedWriteOwnership::Current
                {
                    println!(
                        "  unchanged since last index ({count} messages); skipped ingest + writes ({:.0}ms)",
                        t0.elapsed().as_secs_f64() * 1000.0
                    );
                    println!("  phases: source-check {source_ms}ms");
                    return Ok(());
                }
            }
        }
    }
    let mut index_lock = acquire_index_lock()?;
    let result = index_cmd_locked(agent, full, t0, adoption_claim.as_ref());
    let release = index_lock.release();
    match (result, release) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) => Err(error),
        (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(release_error)) => Err(anyhow::anyhow!(
            "{error:#}; additionally failed to release the index lock: {release_error:#}"
        )),
    }
}

fn index_cmd_locked(
    agent: &str,
    full: bool,
    t0: Instant,
    adoption_claim: Option<&AdoptionClaim>,
) -> anyhow::Result<()> {
    // Per-phase wall clock, printed at the end: optimize from these numbers, not intuition.
    let mut phases: Vec<(&'static str, u128)> = Vec::new();
    let mut mark = Instant::now();
    macro_rules! lap {
        ($name:expr) => {{
            phases.push(($name, mark.elapsed().as_millis()));
            mark = Instant::now();
        }};
    }
    macro_rules! timed {
        ($body:expr) => {{
            let started = Instant::now();
            let result = $body;
            (result, started.elapsed())
        }};
    }
    let data = data_dir()?;
    let ownership_current = match derived_write_ownership(&data) {
        DerivedWriteOwnership::Current => {
            if adoption_claim.is_none() {
                disclose_read_only_ownership(
                    "derived write claim disappeared during writer preflight",
                );
                return Ok(());
            }
            if let Some(reason) = adoption_daemon_fence(&data) {
                disclose_read_only_ownership(&reason);
                return Ok(());
            }
            consume_retained_corpus_owner(&data)?;
            true
        }
        DerivedWriteOwnership::Adoption => {
            if adoption_claim.is_none() {
                disclose_read_only_ownership(
                    "derived ownership changed to ownerless during writer preflight",
                );
                return Ok(());
            }
            if let Some(reason) = adoption_daemon_fence(&data) {
                disclose_read_only_ownership(&reason);
                return Ok(());
            }
            false
        }
        DerivedWriteOwnership::Foreign(reason) => {
            if adoption_claim.is_none() {
                disclose_read_only_ownership(
                    "derived write claim disappeared during writer preflight",
                );
                return Ok(());
            }
            if let Some(fence) = adoption_daemon_fence(&data) {
                disclose_read_only_ownership(&format!("{reason}; {fence}"));
                return Ok(());
            }
            if let Err(refusal) = take_over_foreign_derived_stores(&data, &reason) {
                disclose_read_only_ownership(&refusal);
                return Ok(());
            }
            false
        }
        DerivedWriteOwnership::PostAdoptionClobber(reason) => {
            disclose_read_only_ownership(&reason);
            return Ok(());
        }
        DerivedWriteOwnership::Refused(reason) => {
            disclose_read_only_ownership(&reason);
            return Ok(());
        }
    };
    if let Err(error) = sweep_staging_temps(&data) {
        eprintln!(
            "warning: could not sweep stale ingest staging files in {}: {error}",
            data.display()
        );
    }
    let cache_path = data.join(".ingest_cache.bin");
    let source_path = data.join(SOURCE_SNAPSHOT_FILE);
    let policy_path = data.join(HARNESS_POLICY_SNAPSHOT_FILE);
    let pending_path = data.join(INGEST_PENDING_FILE);
    let absence_path = data.join(SOURCE_ABSENCE_FILE);
    let sig_path = data.join(".ingest.sig");
    let path = data.join("messages.jsonl");
    let boundary_path = data.join(agrep_core::boundary_stats::FILE_NAME);
    let boundary_cache_path = data.join(agrep_core::boundary_stats::CACHE_FILE_NAME);
    let edir = data.join("events");
    let run_agents: Vec<&str> = ingest::registry::run_agents(agent);
    // Source-identical run => identical outputs, so fingerprint-compare before deserializing
    // the parse cache or deduping rows (the every-search warm path). --full must force a parse
    // and --emit-rows must stream rows even when unchanged, so both bypass the shortcut.
    let emit_rows = agrep_core::emit::on();
    let (policy_before, harness_prefixes) = harness_policy()?;
    if emit_rows {
        // streamed rows must classify exactly as this run's normalize pass will
        agrep_core::emit::set_harness_prefixes(&harness_prefixes);
    }
    let (source_before, source_preflight_error) = if emit_rows {
        (None, None)
    } else {
        match ingest::registry::source_snapshot(agent) {
            Ok(snapshot) => (Some(snapshot), None),
            Err(error) => {
                let reason = error.to_string();
                (None, Some(reason))
            }
        }
    };
    let source_issues = source_before
        .as_deref()
        .map(ingest::registry::source_snapshot_issues)
        .unwrap_or_default();
    let durable_blocked = source_issues
        .iter()
        .any(|issue| durable_source_issue(issue.kind()));
    if !source_issues.is_empty() || source_preflight_error.is_some() {
        publish_source_unreadable(
            &data,
            agent,
            &source_issues,
            &[],
            source_preflight_error
                .as_deref()
                .unwrap_or("a source was unreadable during preflight"),
        )?;
    }
    let (previous_sig, previous_sig_mtime) =
        match agrep_core::ingest::registry::read_bounded_regular_file_snapshot(
            &sig_path,
            INGEST_SIGNATURE_MAX_BYTES,
        ) {
            Ok(Some(snapshot)) => match String::from_utf8(snapshot.bytes) {
                Ok(signature) => (Some(signature), Some(snapshot.modified)),
                Err(error) => {
                    eprintln!(
                        "warning: ignoring invalid ingest signature {}: {error}",
                        agrep_core::ingest::terminal_safe(sig_path.display())
                    );
                    (None, None)
                }
            },
            Ok(None) => (None, None),
            Err(error) => {
                eprintln!(
                    "warning: ignoring unreadable ingest signature {}: {error}",
                    agrep_core::ingest::terminal_safe(sig_path.display())
                );
                (None, None)
            }
        };
    let derived_valid = previous_sig
        .as_deref()
        .is_some_and(|signature| derived_generation_valid(&data, signature));
    let legacy_derived_valid = !derived_valid
        && previous_sig
            .as_deref()
            .is_some_and(|signature| legacy_derived_generation_valid(&data, signature));
    let previous_count = previous_sig
        .as_deref()
        .and_then(|s| s.trim().split_once(':'))
        .and_then(|(n, _)| n.parse::<usize>().ok());
    let published_source = read_optional_bytes(&source_path, SOURCE_SNAPSHOT_MAX_BYTES)?;
    let published_policy = read_optional_bytes(&policy_path, HARNESS_POLICY_MAX_BYTES)?;
    let pending_source = read_optional_bytes(&pending_path, SOURCE_SNAPSHOT_MAX_BYTES)?;
    let transcripts_identical = source_before
        .as_deref()
        .zip(published_source.as_deref())
        .map(|(now, prior)| now == prior)
        .unwrap_or(false);
    let policy_identical = published_policy
        .as_deref()
        .map(|prior| prior == policy_before.as_slice())
        .unwrap_or(false);
    let source_identical = transcripts_identical && policy_identical;
    // The source snapshot proves transcripts only; an incomplete scoped event-store proof
    // forces a complete source parse.
    let event_authority = cache::event_publication_authority(&edir, &run_agents)?;
    let events_complete = event_authority.is_some();
    // An emit-rows pass writes the published bytes as its pending handoff without touching
    // outputs; once the published generation is proven current there is no crash to recover.
    let pending_residue =
        pending_source.is_some() && pending_source.as_deref() == published_source.as_deref();
    if !full
        && !emit_rows
        && ownership_current
        && derived_valid
        && previous_count.is_some()
        && source_identical
        && events_complete
        && (pending_source.is_none() || pending_residue)
    {
        // Preserve the existing content signature; its mtime is the freshness clock Python
        // checks before every search.
        cache::write_bytes_atomic(
            &sig_path,
            previous_sig.as_deref().unwrap_or_default().as_bytes(),
        )?;
        cache::remove_if_exists(&absence_path)?;
        if pending_residue {
            cache::remove_if_exists(&pending_path)?;
        }
        if source_issues.is_empty() {
            clear_source_health(&data, agent)?;
        }
        lap!("source-check");
        println!(
            "  unchanged since last index ({} messages); skipped ingest + writes ({:.0}ms)",
            previous_count.unwrap_or(0),
            t0.elapsed().as_secs_f64() * 1000.0
        );
        let breakdown: Vec<String> = phases.iter().map(|(k, ms)| format!("{k} {ms}ms")).collect();
        println!("  phases: {}", breakdown.join(" · "));
        let _ = mark;
        return Ok(());
    }
    let prior_generation = cache_path.exists()
        || source_path.exists()
        || previous_sig.is_some()
        || path.exists()
        || data.join("replies.jsonl").exists()
        || data.join("sessions.jsonl").exists();
    let repair_events = !full && !events_complete && prior_generation;
    let recover_corrupt_events = agent == "all" && (full || repair_events);
    // Pending preflight (or a legacy generation with no snapshot) means no stable source
    // generation was published; retry through the same guarded path. Unlike event repair this
    // preserves cache hits - its only job is catching sources that moved during the prior pass.
    let retry_sources = !full
        && !emit_rows
        && (pending_source.is_some() || (prior_generation && published_source.is_none()));
    let source_before_view = source_before
        .as_deref()
        .and_then(ingest::registry::source_snapshot_view);
    // A byte-identical pending/preflight pair is the equivalence publication already trusts:
    // an identical incomplete pair carries the same issue records, so the unreadable subset
    // is a stable disclosed fact and completeness is not required of the second observation.
    let repeated_stable_preflight =
        source_before.is_some() && pending_source.as_deref() == source_before.as_deref();
    // A token store enumerating cleanly to zero conversations is the same deletion-shaped
    // observation as clean ENOENT: present, readable, validly empty. Two agreeing passes
    // confirm either; torn/garbage reads record issues and never enter these sets.
    let deletion_observed_agents = |snapshot: &ingest::registry::SourceSnapshotView| {
        let mut agents = snapshot.cleanly_absent_agents();
        agents.extend(snapshot.cleanly_empty_token_store_agents());
        agents
    };
    let current_absent_agents = source_before_view
        .as_ref()
        .map(deletion_observed_agents)
        .unwrap_or_default();
    let previous_absent_agents = if emit_rows {
        None
    } else {
        read_source_absence(&absence_path)?.or_else(|| {
            pending_source
                .as_deref()
                .and_then(ingest::registry::source_snapshot_view)
                .map(|snapshot| deletion_observed_agents(&snapshot))
        })
    };
    // Adapter-local observations confirm ENOENT even while another adapter stays unreadable.
    let repeated_absent_agents: HashSet<String> = previous_absent_agents
        .unwrap_or_default()
        .into_iter()
        .filter(|agent| current_absent_agents.contains(agent))
        .collect();
    if !emit_rows {
        write_source_absence(&absence_path, &current_absent_agents)?;
    }
    if repair_events {
        agrep_core::emit::human_line(format_args!(
            "  event publication incomplete; reparsing sources to reconstruct it"
        ));
    } else if retry_sources {
        agrep_core::emit::human_line(format_args!(
            "  source validation incomplete; retrying changed sources before publication"
        ));
    }

    // Mark the attempted preflight before cache/output mutation. A retry advances the pending
    // snapshot to the current view - only a complete one - so the next run compares absence
    // against a fresh second observation; this run's expectations were already read above.
    if full || pending_source.is_none() {
        // A preflight with nothing to record writes no marker: an empty one is a handoff no
        // later run can match, which turns first use into a guarded rewrite of everything.
        match source_before.as_deref().or(published_source.as_deref()) {
            Some(pending_preflight) if !pending_preflight.is_empty() => {
                cache::write_bytes_atomic(&pending_path, pending_preflight)?;
            }
            _ => cache::remove_if_exists(&pending_path)?,
        }
    } else if source_before_view
        .as_ref()
        .is_some_and(|snapshot| snapshot.complete())
        && pending_source.as_deref() != source_before.as_deref()
    {
        cache::write_bytes_atomic(&pending_path, source_before.as_deref().unwrap_or_default())?;
    }

    // Event recovery must force parsing without discarding last-good cache rows because event
    // payloads are not serialized. A source-drift retry enables the same absence guards but
    // keeps existing cache hits, avoiding a full-store parse storm under continuous writers.
    let recovery_cache = if !full && repair_events {
        if source_before.is_none() {
            anyhow::bail!(
                "ingest recovery needs a complete preflight source snapshot{}; retry once the sources can be enumerated",
                source_detail_suffix(source_preflight_error.as_deref())
            );
        }
        let (cache, refusal) = disclose_cache_refusal(IngestCache::repair(&cache_path));
        if let Some(refusal) = refusal {
            // An undecodable base is discarded and reparsed; a missing/foreign one keeps the
            // two-snapshot protocol. The pending marker rotated above when it could, and the
            // next byte-identical preflight - complete or stably incomplete - completes it.
            if !refusal.is_undecodable() && !source_identical && !repeated_stable_preflight {
                anyhow::bail!(
                    "ingest recovery needs a valid parse cache ({refusal}) or two stable source snapshots; retry{}",
                    if durable_blocked {
                        " - the index recovers automatically once the source is readable again"
                    } else {
                        " or run --full"
                    }
                );
            }
        }
        Some(cache)
    } else if retry_sources {
        if source_before.is_none() {
            anyhow::bail!(
                "source retry needs a complete preflight source snapshot{}; retry once the sources can be enumerated",
                source_detail_suffix(source_preflight_error.as_deref())
            );
        }
        Some(disclose_cache_refusal(IngestCache::guarded_retry(&cache_path)).0)
    } else {
        None
    };
    lap!("source-check");

    // --full recomputes derived outputs; it does not forget what the sources contained. When
    // the preflight already names a durably unreadable scope, start from last-good rows and
    // force-parse every survivor, so the rebuild retains what it cannot re-read.
    let full_retains_unreadable = full && prior_generation && durable_blocked;
    let mut pcache = if full_retains_unreadable {
        disclose_cache_refusal(IngestCache::repair(&cache_path)).0
    } else if full {
        IngestCache::cold()
    } else if let Some(cache) = recovery_cache {
        cache
    } else {
        IngestCache::load(&cache_path)
    };
    let mut expected_agents = HashSet::new();
    let mut expected_paths = HashSet::new();
    // Implicit runs retain the last published identities when a whole store transiently
    // disappears. Explicit --full is the reset path, so old identities must not veto a
    // confirmed deletion; only its current preflight contributes expectations.
    let expectation_snapshots = if full {
        [None, None, source_before.as_deref()]
    } else {
        [
            published_source.as_deref(),
            pending_source.as_deref(),
            source_before.as_deref(),
        ]
    };
    for snapshot in expectation_snapshots.into_iter().flatten() {
        let (agents, paths) = ingest::registry::source_snapshot_expectations(snapshot);
        expected_agents.extend(agents);
        expected_paths.extend(paths);
    }
    pcache.set_repair_expectations(expected_agents, expected_paths);
    // The publication guard's own inventory, independent of the deletion machinery above so
    // --full still converges real deletions: what the last published generation contained,
    // or a proven-empty set when nothing was ever published.
    if let Some(published) = published_source.as_deref() {
        let (_, paths) = ingest::registry::source_snapshot_expectations(published);
        pcache.set_published_material(paths);
        // That inventory lists only what it could read. Name the scopes it could not, so a
        // scope it published as an issue record cannot be mistaken for one it proved empty.
        pcache.set_published_blind_scopes(
            ingest::registry::source_snapshot_issues(published)
                .into_iter()
                .map(|issue| (issue.agent().to_owned(), PathBuf::from(issue.path())))
                .collect(),
        );
    } else if !prior_generation {
        pcache.set_published_material(HashSet::new());
    }
    if let Some(snapshot) = source_before_view.as_ref() {
        pcache.set_current_source_snapshot(snapshot);
    }
    // --full participates: its cold cache blocks empty-store convergence otherwise, and the
    // durable absence marker plus this run's preflight still supply two observations.
    if (retry_sources || full) && !repeated_absent_agents.is_empty() {
        pcache.allow_repeated_missing_roots(repeated_absent_agents);
    }
    // Event payloads are absent from the parse cache, so repair force-parses every survivor;
    // exact preflight coverage and adapter reads prove individual tombstones.
    if repair_events {
        pcache.allow_validated_repair_deletions();
    }
    // The one site that arms a discarded base's deletion evidence, whichever lane built it:
    // a repeated stable observation stands in for the last-good rows the base lost.
    if pcache.discarded_base() && (source_identical || repeated_stable_preflight) {
        pcache.confirm_discarded_base_observation();
    }
    // Census the rows the last-good base attributes to each unreadable scope, before ingest
    // mutates the cache. Empty when no base decoded - then the guards above must carry it.
    let first_unread_scope = source_issues
        .iter()
        .find(|issue| durable_source_issue(issue.kind()))
        .map(|issue| issue.path().to_owned());
    let retained_unread_sessions: HashSet<String> = source_issues
        .iter()
        .filter(|issue| durable_source_issue(issue.kind()))
        .flat_map(|issue| pcache.sessions_under(Path::new(issue.path())))
        .collect();
    lap!("load-cache");
    // a complete parse (cold cache or --full) yields the full event set; a warm run only
    // carries touched sessions' events, so the pulse rollup waits for the next complete run.
    let complete = full || repair_events || !pcache.warm;
    let (msgs, evts, repaired_sessions) = ingest_agent(agent, &mut pcache, &harness_prefixes)?;
    lap!("ingest+dedupe");
    let source_snapshot_safe = pcache.source_snapshot_safe()
        && source_issues.is_empty()
        && source_preflight_error.is_none();
    let stable_unreadable = !source_snapshot_safe
        && source_preflight_error.is_none()
        && !source_issues.is_empty()
        && source_before.is_some()
        && source_issues.iter().all(|issue| {
            durable_source_issue(issue.kind())
                // Only a positive Retained verdict may publish past an unreadable scope.
                && pcache.published_material_under(issue.agent(), Path::new(issue.path()))
                    == agrep_core::ingest_cache::MaterialVerdict::Retained
        })
        && pcache.source_read_issues().iter().all(|read| {
            source_issues
                .iter()
                .any(|issue| issue.agent() == read.agent && read.path.starts_with(issue.path()))
        });
    // This pass read every source, so its verdict is the whole truth about
    // their health. Publication can still be declined for reasons that say
    // nothing about readability, and a record no pass retires outlives its bug.
    if !source_snapshot_safe {
        publish_source_unreadable(
            &data,
            agent,
            &source_issues,
            pcache.source_read_issues(),
            "a source became unavailable while it was being read",
        )?;
    } else {
        clear_source_health(&data, agent)?;
    }
    // A stably-denied store with nothing expected under it publishes empty with the denial
    // on source health; every other incomplete output keeps the fail-closed retry.
    if !pcache.output_complete() && !stable_unreadable {
        let detail = source_detail_suffix(
            first_source_issue_label(&source_issues, pcache.source_read_issues())
                .or_else(|| source_preflight_error.clone())
                .as_deref(),
        );
        // The tail names only satisfiable levers: retries and --full both re-read sources,
        // so a durably unreadable scope with no cache witness recovers only with its access.
        let recovery = if durable_blocked {
            " - the index recovers automatically once the source is readable again"
        } else {
            ""
        };
        if prior_generation {
            anyhow::bail!(
                "ingest could not read a source{detail} and had no complete cache fallback; retained the old generation{recovery}"
            );
        }
        anyhow::bail!(
            "ingest could not read a source{detail} and had no complete cache fallback; no generation was published{recovery}"
        );
    }
    if complete && !emit_rows && source_before.is_none() {
        anyhow::bail!(
            "complete ingest could not capture a preflight source snapshot; no generation was published"
        );
    }
    // Incremental retries may publish cache-merged healthy sessions while the pending marker
    // forces another attempt; a complete pass has no safe partial fallback. A stably
    // unreadable source is a disclosed fact, not a retryable condition, so it may publish.
    if prior_generation && complete && !source_snapshot_safe && !stable_unreadable {
        let detail = source_detail_suffix(
            first_source_issue_label(&source_issues, pcache.source_read_issues())
                .or_else(|| source_preflight_error.clone())
                .as_deref(),
        );
        // "will retry" is a promise; it is only made when a retry can differ. A durably
        // unreadable scope recovers exactly when its access does, and the tail says so.
        let recovery = if durable_blocked {
            " - the index recovers automatically once the source is readable again"
        } else {
            " and will retry"
        };
        if repair_events {
            anyhow::bail!(
                "event repair observed an unavailable/incomplete source{detail}; retained the old generation{recovery}"
            );
        }
        anyhow::bail!(
            "complete ingest observed an unavailable/incomplete source{detail}; retained the old generation{recovery}"
        );
    }
    // Cache serialization and the corpus fingerprint are independent, CPU-heavy walks over
    // different data. Do them together; on the one-file-changed path this hides the complete
    // bincode cache rewrite behind the message hash instead of paying both serially.
    let (cache_stage, sig_line) = rayon::join(
        || pcache.stage_save(&cache_path),
        || format!("{}:{}\n", msgs.len(), content_sig(&msgs)),
    );
    // The staged cache stays private until validation; event-proof revocation makes its later
    // commit crash-recoverable even though event payloads are not serialized in the cache.
    let staged_cache = cache_stage?;
    // fold this run's per-file intake tallies into the accounting book; files served
    // from the cache keep the tally from the run that actually parsed them
    let intake = agrep_core::intake::commit(&data.join("intake_stats.json"))?;
    for (name, s) in &intake {
        let errs = if s.errors > 0 {
            format!(" · {} PARSE ERROR(S)", s.errors)
        } else {
            String::new()
        };
        agrep_core::emit::human_line(format_args!(
            "  [{name}] {} parsed files: {} seen -> {} rows · {} agent-side · {} skipped{errs}",
            s.files, s.seen, s.rows, s.agent_rows, s.skips
        ));
    }
    lap!("stage-cache+content-sig");
    let keep = pcache.live_event_files();
    let prune_files = pcache.event_prune_files(&keep);
    let proven_event_delta = (events_complete && !complete)
        .then(|| {
            cache::event_delta_matches_proven_store(&evts, &edir, &keep, &run_agents, &prune_files)
        })
        .flatten();
    let refresh_events = || -> anyhow::Result<((usize, usize, usize), bool)> {
        if let Some(event_result) = proven_event_delta {
            if std::env::var_os("AGREP_DEBUG").is_some() {
                eprintln!("* [agrep ingest] event delta unchanged; preserved store proof");
            }
            return Ok((event_result, true));
        }
        let event_result = cache::write_events_recovering_authorized(
            &evts,
            &edir,
            &keep,
            &run_agents,
            complete && source_snapshot_safe,
            &prune_files,
            cache::EventRecovery {
                recover_corrupt: recover_corrupt_events,
                authority: event_authority.as_ref(),
            },
        )?;
        let proof_complete = cache::publish_events_complete(&edir, &run_agents)?;
        Ok((event_result, proof_complete))
    };

    // A current proof excludes partial outputs, so a pending retry may reuse it. Legacy proof
    // stays blocked because it cannot distinguish a complete generation from a crash remnant.
    if !full
        && (derived_valid || (!retry_sources && legacy_derived_valid))
        && previous_sig
            .as_deref()
            .map(|prev| prev.trim() == sig_line.trim())
            .unwrap_or(false)
    {
        if legacy_derived_valid {
            anyhow::ensure!(
                legacy_derived_generation_valid(&data, previous_sig.as_deref().unwrap_or_default()),
                "legacy publication changed before its generation-bound upgrade"
            );
        }
        // Message equality does not imply event equality: a changed transcript may add only a
        // tool call/result. Refresh the incremental event delta before blessing the new source
        // snapshot, while still avoiding the expensive messages/replies/sessions rewrites.
        let source_after = generation_for_publication(
            &data,
            agent,
            validated_generation_snapshots(
                agent,
                source_before.as_deref(),
                &policy_before,
                source_snapshot_safe,
                stable_unreadable,
            ),
        );
        let source_after = source_after?;
        if legacy_derived_valid {
            anyhow::ensure!(
                source_after.is_some()
                    && legacy_protected_outputs_valid(
                        &data,
                        previous_sig.as_deref().unwrap_or_default()
                    ),
                "legacy publication changed during its generation-bound upgrade"
            );
            cache::write_session_family_meta(
                &msgs,
                &data.join(cache::SESSION_FAMILY_META_FILE),
                sig_line.trim(),
            )?;
        }
        // The staged cache commits even when publication is withheld: pending retention
        // forces the retry, and a live-writer pass whose next read fails must serve THIS
        // pass's rows. The clock advances only on a safe or publishing pass.
        let generation_publishes = source_after.is_some();
        commit_generation_cache(
            &data,
            &edir,
            &run_agents,
            staged_cache,
            proven_event_delta.is_none(),
        )?;
        let (event_result, proof_complete) = refresh_events()?;
        let (_n_files, n_events, n_rewritten) = event_result;
        anyhow::ensure!(
            proof_complete,
            "event publication remained incomplete after reconstruction"
        );
        let signal_written = publish_generation_markers(GenerationPublication {
            data: &data,
            sig_path: &sig_path,
            signature: &sig_line,
            complete: complete || !policy_identical,
            parse_cache: &pcache,
            messages: &msgs,
            repaired_sessions: &repaired_sessions,
            preserve_signal_if_unchanged: !(source_snapshot_safe || generation_publishes),
        })?;
        preserve_signal_age(
            &sig_path,
            previous_sig_mtime,
            !(source_snapshot_safe || generation_publishes) && signal_written,
        )?;
        lap!("write-events");
        let source_published = source_after.is_some();
        publish_source_snapshot(source_after, &source_path, &policy_path, &pending_path)?;
        if source_published {
            cache::remove_if_exists(&absence_path)?;
        }
        lap!("source-publish");
        agrep_core::emit::human_line(format_args!(
            "  unchanged message set ({} messages); skipped message writes, refreshed {} event(s) ({} file(s) rewritten) ({:.0}ms)",
            msgs.len(),
            n_events,
            n_rewritten,
            t0.elapsed().as_secs_f64() * 1000.0
        ));
        let breakdown: Vec<String> = phases.iter().map(|(k, ms)| format!("{k} {ms}ms")).collect();
        agrep_core::emit::human_line(format_args!("  phases: {}", breakdown.join(" · ")));
        let _ = mark;
        return Ok(());
    }

    let n = msgs.len();
    let rpath = data.join("replies.jsonl");
    let sessions_path = data.join("sessions.jsonl");
    let family_meta_path = data.join(cache::SESSION_FAMILY_META_FILE);

    // Independent backstop, whatever the guards above concluded: this pass may not shrink the
    // row census of a scope it could not read. Scope-local on purpose - a deletion elsewhere
    // is still free to converge - and it never exits 0 on a drop it cannot account for.
    if !retained_unread_sessions.is_empty() {
        let served: HashSet<&str> = msgs.iter().map(|msg| msg.session.as_ref()).collect();
        let dropped = retained_unread_sessions
            .iter()
            .filter(|session| !served.contains(session.as_str()))
            .count();
        if dropped > 0 {
            anyhow::bail!(
                "ingest would drop {dropped} session(s) belonging to a scope it could not read \
                 ({}); retained the old generation",
                agrep_core::ingest::terminal_safe(first_unread_scope.as_deref().unwrap_or("?"))
            );
        }
    }

    let (source_after, source_elapsed) = timed!(validated_generation_snapshots(
        agent,
        source_before.as_deref(),
        &policy_before,
        source_snapshot_safe,
        stable_unreadable,
    ));
    let source_after = generation_for_publication(&data, agent, source_after)?;
    // See the event-only branch above: the cache commits unconditionally so a later failed
    // read serves this pass's rows; only the freshness clock is gated on publication.
    let generation_publishes = source_after.is_some();
    commit_generation_cache(
        &data,
        &edir,
        &run_agents,
        staged_cache,
        proven_event_delta.is_none(),
    )?;

    // The final signature remains the cross-file permission slip.
    agrep_core::boundary_stats::write(
        &msgs,
        &boundary_path,
        &boundary_cache_path,
        sig_line.trim(),
        previous_sig.as_deref().map(str::trim),
        &pcache.touched,
        full,
    )?;
    lap!("write-boundary-stats");

    let messages_work = || timed!(cache::write_messages(&msgs, &path));
    let replies_work = || timed!(cache::write_replies(&msgs, &rpath));
    let sessions_work = || {
        timed!(cache::write_session_index(
            &msgs,
            &sessions_path,
            &family_meta_path,
            sig_line.trim(),
        ))
    };
    let events_work = || timed!(refresh_events());
    // Concurrent derived I/O triggers pathological Windows content-filter stalls.
    #[cfg(windows)]
    let message_outputs = {
        let messages = messages_work();
        let replies = replies_work();
        let sessions = sessions_work();
        ((messages, replies), sessions)
    };
    #[cfg(not(windows))]
    let message_outputs = rayon::join(|| rayon::join(messages_work, replies_work), sessions_work);
    let (
        ((messages_result, messages_elapsed), (replies_result, replies_elapsed)),
        (sessions_result, sessions_elapsed),
    ) = message_outputs;
    messages_result?;
    replies_result?;
    let n_sessions = sessions_result?;
    invalidate_turn_enrichment(&data)?;
    let (event_proof_result, events_elapsed) = events_work();
    let (event_result, proof_complete) = event_proof_result?;
    let (n_files, n_events, n_rewritten) = event_result;
    if std::env::var_os("AGREP_DEBUG").is_some() {
        eprintln!(
            "* [agrep ingest] messages {:.1}ms · replies {:.1}ms · sessions {:.1}ms · events {:.1}ms · postflight {:.1}ms",
            messages_elapsed.as_secs_f64() * 1000.0,
            replies_elapsed.as_secs_f64() * 1000.0,
            sessions_elapsed.as_secs_f64() * 1000.0,
            events_elapsed.as_secs_f64() * 1000.0,
            source_elapsed.as_secs_f64() * 1000.0,
        );
    }
    lap!("write-derived+source-validate");
    anyhow::ensure!(
        proof_complete,
        "event publication remained incomplete after reconstruction"
    );
    // Union onto any unconsumed prior delta so a skipped corpus refresh can't drop a session.
    let signal_written = publish_generation_markers(GenerationPublication {
        data: &data,
        sig_path: &sig_path,
        signature: &sig_line,
        complete: complete || !policy_identical,
        parse_cache: &pcache,
        messages: &msgs,
        repaired_sessions: &repaired_sessions,
        preserve_signal_if_unchanged: !(source_snapshot_safe || generation_publishes),
    })?;
    preserve_signal_age(
        &sig_path,
        previous_sig_mtime,
        !(source_snapshot_safe || generation_publishes) && signal_written,
    )?;
    lap!("publish-markers");
    let source_published = source_after.is_some();
    publish_source_snapshot(source_after, &source_path, &policy_path, &pending_path)?;
    if source_published {
        cache::remove_if_exists(&absence_path)?;
    }
    lap!("source-publish");
    let _ = mark;
    let with_model = msgs.iter().filter(|m| !m.model.is_empty()).count();
    let with_reply = msgs.iter().filter(|m| !m.reply.trim().is_empty()).count();
    agrep_core::emit::human_line(format_args!(
        "  indexed {} messages across {} sessions -> {} ({:.0}ms)",
        n,
        n_sessions,
        agrep_core::ingest::terminal_safe(path.display()),
        t0.elapsed().as_secs_f64() * 1000.0
    ));
    agrep_core::emit::human_line(format_args!(
        "  {} turns carry a model · {} carry an agent reply -> {}",
        with_model,
        with_reply,
        agrep_core::ingest::terminal_safe(rpath.display())
    ));
    agrep_core::emit::human_line(format_args!(
        "  {} session event streams -> {} ({} rewritten, {} events observed this run)",
        n_files,
        agrep_core::ingest::terminal_safe(edir.display()),
        n_rewritten,
        n_events
    ));
    let breakdown: Vec<String> = phases.iter().map(|(k, ms)| format!("{k} {ms}ms")).collect();
    agrep_core::emit::human_line(format_args!("  phases: {}", breakdown.join(" · ")));
    // first-run onboarding pointer; enrollment (teach.json) retires it
    if let Ok(dir) = data_dir() {
        if !dir.join("teach.json").exists() {
            agrep_core::emit::human_line(format_args!(
                "  next: `agrep setup` - teach your agents to search this history"
            ));
        }
    }
    Ok(())
}

fn acquire_index_lock() -> anyhow::Result<IndexLock> {
    let data = data_dir()?;
    fs::create_dir_all(&data).map_err(|error| {
        anyhow::anyhow!(
            "could not create agrep data directory {}: {error}",
            data.display()
        )
    })?;
    if default_data_dir_selected() {
        protect_default_data_dir(&data)?;
    }
    IndexLock::acquire(
        &data.join(".index.lock"),
        "agrep-rs",
        std::time::Duration::from_millis(index_lock::TIMEOUT_MS),
    )
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;
    use std::path::PathBuf;

    use clap::Parser as _;

    #[cfg(unix)]
    use super::protect_default_data_dir;
    use super::{
        absolute_data_dir, adopt_foreign_corpus_db_with, apply_durable_store_issues,
        clear_source_health, decoded_store_audit_payload, derived_generation_valid,
        derived_generation_valid_with, derived_names, derived_proof, derived_write_ownership,
        normalize_messages, platform_data_dir_from, probe_corpus_db_owner, publish_derived_owner,
        publish_derived_proof, publish_retained_corpus_owner, publish_source_snapshot,
        publish_source_snapshot_with, publish_source_unreadable, read_fallback_event_scan_request,
        read_optional_bytes, reclaim_cold_rollback_journal, regular_file_has_bytes,
        resolve_data_dir, runtime_source_issue_value, serialize_store_audit_payload,
        source_health_records, staging_temp_owner, store_audit_payload, sweep_staging_temps_with,
        unlocked_owner_has_no_retained_corpus, write_changed_sessions, write_source_health, Cli,
        CorpusDbAdoptionError, CorpusDbOwnerProbe, DerivedOwnerRecord, DerivedWriteOwnership,
        RetainedCorpusDb, SourcePublishOutcome, DERIVED_OWNER_FILE, DERIVED_OWNER_VERSION,
    };

    #[test]
    fn protocol_panic_guard_recovers_for_the_next_request() {
        let failed: Result<(), ()> = super::catch_protocol_panic(|| panic!("fixture"));
        assert_eq!(failed, Err(()));
        assert_eq!(super::catch_protocol_panic(|| 42), Ok(42));
    }

    #[test]
    fn corpus_fts_sidecar_is_exact_and_sparse() {
        assert!(super::corpus_fts_text_is_valid(
            Some("abc\0def\0ghi"),
            Some("abc def ghi")
        ));
        assert!(super::corpus_fts_text_is_valid(Some("abcdef"), None));
        assert!(super::corpus_fts_text_is_valid(None, None));
        assert!(!super::corpus_fts_text_is_valid(Some("abc\0def"), None));
        assert!(!super::corpus_fts_text_is_valid(
            Some("abcdef"),
            Some("abcdef")
        ));
        assert!(!super::corpus_fts_text_is_valid(
            Some("abc\0def"),
            Some("abc\0def")
        ));
    }

    #[test]
    fn bundled_sqlite_accepts_nul_safe_external_content() {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE docs(
                 id INTEGER PRIMARY KEY, who TEXT, raw_text TEXT, fts_text TEXT);
             CREATE VIEW docs_fts_content AS
                 SELECT id, coalesce(fts_text, raw_text) AS text FROM docs;
             CREATE VIEW docs_prose_fts_content AS
                 SELECT id, coalesce(fts_text, raw_text) AS text
                 FROM docs WHERE who <> 'tool';
             CREATE VIRTUAL TABLE docs_fts USING fts5(
                 text, content='docs_fts_content', content_rowid='id', tokenize='trigram');
             CREATE VIRTUAL TABLE docs_prose_fts USING fts5(
                 text, content='docs_prose_fts_content',
                 content_rowid='id', tokenize='trigram');",
        )
        .unwrap();
        db.execute(
            "INSERT INTO docs(id, who, raw_text, fts_text)
             VALUES(1, 'user', ?1, ?2)",
            rusqlite::params!["abc\0def", "abc def"],
        )
        .unwrap();
        db.execute(
            "INSERT INTO docs(id, who, raw_text) VALUES(2, 'tool', 'tool output')",
            [],
        )
        .unwrap();
        db.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')", [])
            .unwrap();
        db.execute(
            "INSERT INTO docs_prose_fts(docs_prose_fts) VALUES('rebuild')",
            [],
        )
        .unwrap();
        db.execute(
            "INSERT INTO docs_fts(docs_fts, rank) VALUES('integrity-check', 1)",
            [],
        )
        .unwrap();
        db.execute(
            "INSERT INTO docs_prose_fts(docs_prose_fts, rank)
             VALUES('integrity-check', 1)",
            [],
        )
        .unwrap();
        for query in ["abc", "def"] {
            assert_eq!(
                db.query_row(
                    "SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?1",
                    [query],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
                1
            );
        }
        for query in ["bcd", "cde"] {
            assert_eq!(
                db.query_row(
                    "SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?1",
                    [query],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
                0
            );
        }
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM docs_prose_fts
                 WHERE docs_prose_fts MATCH 'abc'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM docs_prose_fts
                 WHERE docs_prose_fts MATCH 'tool'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            0
        );
        assert_eq!(
            db.query_row("SELECT raw_text FROM docs WHERE id=1", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "abc\0def"
        );
    }

    #[test]
    fn bundled_sqlite_rejects_mismatched_trigram_postings() {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE docs(id INTEGER PRIMARY KEY, raw_text TEXT);
             CREATE VIEW docs_fts_content AS
                 SELECT id, raw_text AS text FROM docs;
             CREATE VIRTUAL TABLE docs_fts USING fts5(
                 text, content='docs_fts_content', content_rowid='id', tokenize='trigram');",
        )
        .unwrap();
        db.execute(
            "INSERT INTO docs(id, raw_text) VALUES(1, ?1)",
            rusqlite::params!["abcdef"],
        )
        .unwrap();
        db.execute(
            "INSERT INTO docs_fts(rowid, text) VALUES(1, ?1)",
            rusqlite::params!["abcxyz"],
        )
        .unwrap();
        let error = db
            .execute(
                "INSERT INTO docs_fts(docs_fts, rank) VALUES('integrity-check', 1)",
                [],
            )
            .unwrap_err();
        assert!(matches!(
            error,
            rusqlite::Error::SqliteFailure(
                rusqlite::ffi::Error {
                    code: rusqlite::ErrorCode::DatabaseCorrupt,
                    ..
                },
                _
            )
        ));
    }

    #[test]
    fn fallback_event_scan_request_limit_is_exact() {
        assert_eq!(
            read_fallback_event_scan_request(std::io::Cursor::new([0_u8; 8]), 8).unwrap(),
            [0_u8; 8]
        );
        let error =
            read_fallback_event_scan_request(std::io::Cursor::new([0_u8; 9]), 8).unwrap_err();
        assert!(error.to_string().contains("request is too large"));
    }

    #[test]
    fn unlocked_skip_refuses_retained_corpus_authority() {
        let data = std::env::temp_dir().join(format!(
            "agrep-unlocked-retained-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        std::fs::write(
            data.join(DERIVED_OWNER_FILE),
            serde_json::to_vec(&serde_json::json!({
                "version": DERIVED_OWNER_VERSION,
                "build_id": current,
                "retained_corpus_db": {
                    "build_id": "bbbbbbbbbbbbbbbbbbbb",
                    "proof": {
                        "name": "corpus.db",
                        "len": 1,
                        "modified_ns": 2,
                        "change_token": {"Metadata": 3},
                        "edge_hash": 4
                    },
                    "reader_identity": {
                        "len": 1,
                        "modified_ns": 2,
                        "changed_ns": 3,
                        "device": 4,
                        "inode": 5
                    }
                }
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(!unlocked_owner_has_no_retained_corpus(&data));
        let _ = std::fs::remove_dir_all(data);
    }

    #[cfg(unix)]
    fn fifo(path: &std::path::Path) {
        let status = std::process::Command::new("mkfifo")
            .arg(path)
            .status()
            .unwrap();
        assert!(status.success());
    }

    #[test]
    fn stale_staging_files_are_removed_only_for_dead_owners() {
        let root = std::env::temp_dir().join(format!(
            "agrep-staging-sweep-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let dead = root.join("messages.jsonl.tmp.41.9");
        let dead_journal = root.join(".ingest_cache.bin.journal.tmp.41.9.4");
        let dead_corpus = root.join("corpus.db.tmp.41.9.5");
        let dead_corpus_wal = root.join("corpus.db.tmp.41.9.5-wal");
        let live = root.join(".ingest_cache.bin.tmp.42.9.3");
        let unknown = root.join("sessions.jsonl.tmp.43.9");
        let malformed = root.join("messages.jsonl.tmp.41.not-a-clock");
        let unrelated = root.join("notes.tmp.41.9");
        std::fs::write(&dead, b"dead").unwrap();
        std::fs::write(&dead_journal, b"journal").unwrap();
        std::fs::write(&dead_corpus, b"corpus").unwrap();
        std::fs::write(&dead_corpus_wal, b"wal").unwrap();
        std::fs::write(&live, b"live").unwrap();
        std::fs::write(&unknown, b"unknown").unwrap();
        std::fs::write(&malformed, b"keep").unwrap();
        std::fs::write(&unrelated, b"keep").unwrap();

        let result = sweep_staging_temps_with(&root, |pid| match pid {
            41 => Some(false),
            42 => Some(true),
            _ => None,
        })
        .unwrap();
        assert_eq!(result, (4, 20));
        assert!(!dead.exists());
        assert!(!dead_journal.exists());
        assert!(!dead_corpus.exists());
        assert!(!dead_corpus_wal.exists());
        assert!(live.exists());
        assert!(unknown.exists());
        assert!(malformed.exists());
        assert!(unrelated.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    /// A rollback journal is a takeover blocker only while its writer might still be alive.
    #[test]
    fn cold_rollback_journals_are_reclaimed_and_a_live_writer_declines() {
        let data = std::env::temp_dir().join(format!(
            "agrep-journal-reclaim-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let corpus = data.join("corpus.db");
        let journal = data.join("corpus.db-journal");
        let seed = rusqlite::Connection::open(&corpus).unwrap();
        seed.execute_batch("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            .unwrap();
        drop(seed);

        // no journal at all: nothing to reclaim, nothing to say
        reclaim_cold_rollback_journal(&corpus).unwrap();

        // a journal SQLite itself ruled inert (invalidated header) is debris
        std::fs::write(&journal, vec![0_u8; 4096]).unwrap();
        reclaim_cold_rollback_journal(&corpus).unwrap();
        assert!(!journal.exists());

        // a live writer holds RESERVED: the exclusive open names it and declines
        let writer = rusqlite::Connection::open(&corpus).unwrap();
        writer.busy_timeout(std::time::Duration::ZERO).unwrap();
        writer
            .execute_batch("BEGIN IMMEDIATE; INSERT INTO meta VALUES('build_id', 'x')")
            .unwrap();
        assert!(journal.exists());
        let refusal = reclaim_cold_rollback_journal(&corpus).unwrap_err();
        assert!(
            refusal.contains("live rollback journal"),
            "unexpected refusal: {refusal}"
        );
        assert!(journal.exists(), "a live writer's journal must survive");

        // and once that writer is gone, the same call clears the way
        writer.execute_batch("ROLLBACK").unwrap();
        drop(writer);
        std::fs::write(&journal, vec![0_u8; 4096]).unwrap();
        reclaim_cold_rollback_journal(&corpus).unwrap();
        assert!(!journal.exists());
        assert!(matches!(
            probe_corpus_db_owner(&corpus, "current"),
            CorpusDbOwnerProbe::LegacyUnowned
        ));
        let _ = std::fs::remove_dir_all(&data);
    }

    #[test]
    fn staging_owner_parser_rejects_ambiguous_suffixes() {
        assert_eq!(staging_temp_owner("messages.jsonl.tmp.41.9"), Some(41));
        assert_eq!(staging_temp_owner("corpus.db.tmp.41.9.5"), Some(41));
        assert_eq!(staging_temp_owner("corpus.db.tmp.41.9.5-wal"), Some(41));
        assert_eq!(staging_temp_owner(".ingest_cache.bin.tmp.41.9.3"), Some(41));
        assert_eq!(
            staging_temp_owner(".source_absence_pending.tmp.41.9"),
            Some(41)
        );
        assert_eq!(
            staging_temp_owner(".ingest_cache.bin.journal.tmp.41.9.3"),
            Some(41)
        );
        assert_eq!(staging_temp_owner(".tmp.41.9"), None);
        assert_eq!(staging_temp_owner("notes.tmp.41.9"), None);
        assert_eq!(staging_temp_owner("messages.jsonl.tmp.41"), None);
        assert_eq!(staging_temp_owner("messages.jsonl.tmp.41.9.3.extra"), None);
    }

    #[test]
    fn harness_projects_require_a_path_segment_prefix() {
        use agrep_core::row_class::is_harness_project;
        for project in [
            "vo-exp",
            "_probe",
            "_probe-run",
            "control_fixture",
            "run_control_42",
            "haiku-control-case",
            "haiku-treatment-case",
            "root/control_fixture",
            r"root\RUN_CONTROL_case",
        ] {
            assert!(is_harness_project(project), "{project}");
        }
        for project in [
            "version_control_tools",
            "my-haiku-control-tools",
            "runbook_run_control",
            "production_probe",
            "vo-expansion",
        ] {
            assert!(!is_harness_project(project), "{project}");
        }
    }

    #[test]
    fn derived_owner_adopts_once_rejects_foreign_and_ownerless_cache() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let cache_path = data.join(".ingest_cache.bin");
        let cache = agrep_core::ingest_cache::IngestCache::cold();
        cache.stage_save(&cache_path).unwrap().commit().unwrap();

        assert_eq!(
            derived_write_ownership(&data),
            DerivedWriteOwnership::Adoption
        );
        publish_derived_owner(&data).unwrap();
        assert_eq!(
            derived_write_ownership(&data),
            DerivedWriteOwnership::Current
        );

        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        let foreign = if current == "aaaaaaaaaaaaaaaaaaaa" {
            "bbbbbbbbbbbbbbbbbbbb"
        } else {
            "aaaaaaaaaaaaaaaaaaaa"
        };
        std::fs::write(
            data.join(DERIVED_OWNER_FILE),
            serde_json::to_vec(&DerivedOwnerRecord {
                version: DERIVED_OWNER_VERSION,
                build_id: foreign.to_string(),
                legacy_corpus_db: None,
                retained_corpus_db: None,
            })
            .unwrap(),
        )
        .unwrap();
        let DerivedWriteOwnership::Foreign(reason) = derived_write_ownership(&data) else {
            panic!("foreign owner must refuse ordinary writers");
        };
        assert!(reason.contains(&format!("owned-by {foreign}")), "{reason}");

        std::fs::write(
            data.join(DERIVED_OWNER_FILE),
            serde_json::to_vec(&DerivedOwnerRecord {
                version: DERIVED_OWNER_VERSION,
                build_id: current.clone(),
                legacy_corpus_db: None,
                retained_corpus_db: None,
            })
            .unwrap(),
        )
        .unwrap();
        std::fs::write(&cache_path, b"legacy writer erased the owned envelope").unwrap();
        let DerivedWriteOwnership::Refused(reason) = derived_write_ownership(&data) else {
            panic!("a current anchor must not adopt an ownerless replacement cache");
        };
        assert!(
            reason.contains("parse cache has no writing-build identity"),
            "{reason}"
        );

        let mut malformed = vec![0_u8; 12];
        malformed.extend_from_slice(b"AGRPCB01");
        std::fs::write(&cache_path, malformed).unwrap();
        let DerivedWriteOwnership::Refused(reason) = derived_write_ownership(&data) else {
            panic!("a current anchor must not replace a malformed cache header");
        };
        assert!(reason.contains("ownership is malformed"), "{reason}");

        std::fs::remove_file(&cache_path).unwrap();
        std::fs::create_dir(&cache_path).unwrap();
        let DerivedWriteOwnership::Refused(reason) = derived_write_ownership(&data) else {
            panic!("a current anchor must not replace an unreadable cache path");
        };
        assert!(reason.contains("ownership is unreadable"), "{reason}");

        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn derived_owner_keeps_matching_cache_payload_damage_repairable() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-cache-repair-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let cache_path = data.join(".ingest_cache.bin");
        agrep_core::ingest_cache::IngestCache::cold()
            .stage_save(&cache_path)
            .unwrap()
            .commit()
            .unwrap();
        publish_derived_owner(&data).unwrap();

        let mut damaged = std::fs::read(&cache_path).unwrap();
        *damaged.last_mut().expect("owned cache must have a payload") ^= 0x80;
        std::fs::write(&cache_path, damaged).unwrap();
        assert_eq!(
            agrep_core::ingest_cache::IngestCache::repair(&cache_path).1,
            Some(agrep_core::ingest_cache::CacheDecodeRefusal::PayloadDigestMismatch),
            "the control must damage the payload without erasing its owner prefix"
        );
        assert_eq!(
            derived_write_ownership(&data),
            DerivedWriteOwnership::Current,
            "a matching owner prefix keeps payload repair within the writing build"
        );
        std::fs::remove_file(&cache_path).unwrap();
        assert_eq!(
            derived_write_ownership(&data),
            DerivedWriteOwnership::Current,
            "a missing cache remains the clean same-build crash window"
        );

        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn derived_owner_rejects_unknown_nested_legacy_proof_fields() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-strict-proof-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        std::fs::write(
            data.join(DERIVED_OWNER_FILE),
            serde_json::to_vec(&serde_json::json!({
                "version": DERIVED_OWNER_VERSION,
                "build_id": current,
                "legacy_corpus_db": {
                    "name": "corpus.db",
                    "len": 1,
                    "modified_ns": 2,
                    "change_token": {"Metadata": 3},
                    "edge_hash": 4,
                    "invented": true
                }
            }))
            .unwrap(),
        )
        .unwrap();
        let DerivedWriteOwnership::Refused(reason) = derived_write_ownership(&data) else {
            panic!("unknown nested ownership proof fields must fail closed");
        };
        assert!(reason.contains("malformed"), "{reason}");
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn derived_owner_rejects_two_migration_authorities() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-dual-authority-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        std::fs::write(
            data.join(DERIVED_OWNER_FILE),
            serde_json::to_vec(&serde_json::json!({
                "version": DERIVED_OWNER_VERSION,
                "build_id": current,
                "legacy_corpus_db": {
                    "name": "corpus.db",
                    "len": 1,
                    "modified_ns": 2,
                    "change_token": {"Metadata": 3},
                    "edge_hash": 4
                },
                "retained_corpus_db": {
                    "build_id": "bbbbbbbbbbbbbbbbbbbb",
                    "proof": {
                        "name": "corpus.db",
                        "len": 1,
                        "modified_ns": 2,
                        "change_token": {"Metadata": 3},
                        "edge_hash": 4
                    },
                    "reader_identity": {
                        "len": 1,
                        "modified_ns": 2,
                        "changed_ns": 3,
                        "device": 4,
                        "inode": 5
                    }
                }
            }))
            .unwrap(),
        )
        .unwrap();
        let DerivedWriteOwnership::Refused(reason) = derived_write_ownership(&data) else {
            panic!("two migration authorities must fail closed");
        };
        assert!(reason.contains("malformed"), "{reason}");
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn retained_owner_rejects_same_owner_database_replacement() {
        let data = std::env::temp_dir().join(format!(
            "agrep-retained-owner-swap-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        agrep_core::ingest_cache::IngestCache::cold()
            .stage_save(&data.join(".ingest_cache.bin"))
            .unwrap()
            .commit()
            .unwrap();
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        let foreign = if current == "aaaaaaaaaaaaaaaaaaaa" {
            "bbbbbbbbbbbbbbbbbbbb"
        } else {
            "aaaaaaaaaaaaaaaaaaaa"
        };
        let write_database = |path: &std::path::Path, marker: &str| {
            let connection = rusqlite::Connection::open(path).unwrap();
            connection
                .execute_batch(&format!(
                    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                     INSERT INTO meta(key, value) VALUES('build_id', '{foreign}');
                     CREATE TABLE marker(value TEXT NOT NULL);
                     INSERT INTO marker(value) VALUES('{marker}');"
                ))
                .unwrap();
        };
        let corpus = data.join("corpus.db");
        write_database(&corpus, "validated-generation");
        let captured = agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus, 512)
            .unwrap()
            .unwrap();
        let proof = super::derived_file_proof_from_snapshot("corpus.db", &captured).unwrap();
        assert_eq!(
            proof,
            super::derived_file_proof(&data, "corpus.db").unwrap()
        );
        let reader_identity = agrep_core::ingest::registry::regular_file_reader_identity(&corpus)
            .unwrap()
            .unwrap();
        let candidate = RetainedCorpusDb {
            build_id: foreign.to_string(),
            proof,
            reader_identity,
        };

        let replacement = data.join("replacement.db");
        write_database(&replacement, "same-owner-replacement");
        let replacement_bytes = std::fs::read(&replacement).unwrap();
        std::fs::remove_file(&corpus).unwrap();
        std::fs::rename(&replacement, &corpus).unwrap();

        let error = publish_retained_corpus_owner(&data, candidate).unwrap_err();
        assert!(error.contains("changed after reader validation"), "{error}");
        assert!(!data.join(DERIVED_OWNER_FILE).exists());
        assert_eq!(std::fs::read(&corpus).unwrap(), replacement_bytes);
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn derived_owner_binds_the_one_legacy_database_adoption_candidate() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-db-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let cache = agrep_core::ingest_cache::IngestCache::cold();
        cache
            .stage_save(&data.join(".ingest_cache.bin"))
            .unwrap()
            .commit()
            .unwrap();
        let legacy_db = data.join("corpus.db");
        let connection = rusqlite::Connection::open(&legacy_db).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                 INSERT INTO meta(key, value) VALUES('schema', 'legacy');",
            )
            .unwrap();
        drop(connection);

        publish_derived_owner(&data).unwrap();
        let record: DerivedOwnerRecord =
            serde_json::from_slice(&std::fs::read(data.join(DERIVED_OWNER_FILE)).unwrap()).unwrap();
        let legacy = record
            .legacy_corpus_db
            .expect("existing legacy db must be bound into the owner record");
        assert_eq!(legacy.name, "corpus.db");
        assert_eq!(legacy.len, std::fs::metadata(legacy_db).unwrap().len());
        assert_ne!(legacy.edge_hash, 0);

        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn post_adoption_clobber_is_detected_without_mutating_last_good_data() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-post-clobber-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        agrep_core::ingest_cache::IngestCache::cold()
            .stage_save(&data.join(".ingest_cache.bin"))
            .unwrap()
            .commit()
            .unwrap();
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        std::fs::write(
            data.join(DERIVED_OWNER_FILE),
            serde_json::to_vec(&DerivedOwnerRecord {
                version: DERIVED_OWNER_VERSION,
                build_id: current,
                legacy_corpus_db: None,
                retained_corpus_db: None,
            })
            .unwrap(),
        )
        .unwrap();

        let corpus = data.join("corpus.db");
        let connection = rusqlite::Connection::open(&corpus).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                 INSERT INTO meta(key, value) VALUES('schema', 'legacy-rewrite');",
            )
            .unwrap();
        drop(connection);
        let before = std::fs::read(&corpus).unwrap();
        let DerivedWriteOwnership::PostAdoptionClobber(reason) = derived_write_ownership(&data)
        else {
            panic!("spent adoption must classify an ownerless replacement separately");
        };
        assert!(reason.contains("automatic repair is disabled"), "{reason}");
        assert!(reason.contains("agrep doctor"), "{reason}");
        assert_eq!(std::fs::read(&corpus).unwrap(), before);

        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn derived_owner_never_adopts_a_database_owned_by_another_build() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-foreign-db-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let cache = agrep_core::ingest_cache::IngestCache::cold();
        cache
            .stage_save(&data.join(".ingest_cache.bin"))
            .unwrap()
            .commit()
            .unwrap();
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        let foreign = if current == "aaaaaaaaaaaaaaaaaaaa" {
            "bbbbbbbbbbbbbbbbbbbb"
        } else {
            "aaaaaaaaaaaaaaaaaaaa"
        };
        let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
        connection
            .execute_batch(&format!(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                 INSERT INTO meta(key, value) VALUES('build_id', '{foreign}');"
            ))
            .unwrap();
        drop(connection);

        let DerivedWriteOwnership::Foreign(reason) = derived_write_ownership(&data) else {
            panic!("an ownerless family must not adopt a foreign-owned database");
        };
        assert!(reason.contains(&format!("corpus.db owned-by {foreign}")));
        assert!(!data.join(DERIVED_OWNER_FILE).exists());

        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn corpus_takeover_retry_window_has_no_post_final_sleep() {
        let mut attempts = 0_usize;
        let mut verifications = 0_usize;
        let mut sleeps = Vec::new();
        let error = super::retry_corpus_takeover_replace(
            || {
                verifications += 1;
                Ok(())
            },
            || {
                attempts += 1;
                Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    format!("replace attempt {attempts}"),
                ))
            },
            |delay| sleeps.push(delay),
        )
        .unwrap_err();
        let expected = super::CORPUS_TAKEOVER_RETRY_DELAYS_MS.map(std::time::Duration::from_millis);
        assert_eq!(attempts, expected.len() + 1);
        assert_eq!(verifications, attempts);
        assert_eq!(sleeps, expected);
        assert_eq!(error.to_string(), format!("replace attempt {attempts}"));
        assert!(expected.windows(2).all(|pair| pair[0] <= pair[1]));
        assert_eq!(
            expected.iter().sum::<std::time::Duration>().as_millis(),
            1630
        );

        let mut released_attempts = 0_usize;
        let mut released_verifications = 0_usize;
        let mut released_sleeps = Vec::new();
        super::retry_corpus_takeover_replace(
            || {
                released_verifications += 1;
                Ok(())
            },
            || {
                released_attempts += 1;
                if released_attempts == 4 {
                    Ok(())
                } else {
                    Err(std::io::Error::new(
                        std::io::ErrorKind::PermissionDenied,
                        "reader still holds destination",
                    ))
                }
            },
            |delay| released_sleeps.push(delay),
        )
        .unwrap();
        assert_eq!(released_attempts, 4);
        assert_eq!(released_verifications, released_attempts);
        assert_eq!(released_sleeps, expected[..3]);

        let mut fenced_attempts = 0_usize;
        let mut fenced_verifications = 0_usize;
        let fenced = super::retry_corpus_takeover_replace(
            || {
                fenced_verifications += 1;
                if fenced_verifications == 1 {
                    Ok(())
                } else {
                    Err(std::io::Error::other("destination changed"))
                }
            },
            || {
                fenced_attempts += 1;
                Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "reader still holds destination",
                ))
            },
            |_| {},
        )
        .unwrap_err();
        assert_eq!(fenced_attempts, 1);
        assert_eq!(fenced_verifications, 2);
        assert_eq!(fenced.to_string(), "destination changed");
    }

    #[test]
    fn private_snapshot_sync_uses_a_flush_capable_handle() {
        let data = std::env::temp_dir().join(format!(
            "agrep-private-snapshot-sync-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let snapshot = data.join("corpus.db.tmp");
        std::fs::write(&snapshot, b"durable snapshot").unwrap();

        super::sync_private_corpus_stage(&snapshot).unwrap();
        assert_eq!(std::fs::read(&snapshot).unwrap(), b"durable snapshot");

        let _ = std::fs::remove_dir_all(data);
    }

    #[cfg(windows)]
    #[test]
    fn windows_corpus_takeover_waits_for_a_held_reader() {
        use std::os::windows::fs::OpenOptionsExt as _;
        use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ;

        let data = std::env::temp_dir().join(format!(
            "agrep-corpus-windows-reader-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let target = data.join("corpus.db");
        let staged = data.join("corpus.db.tmp");
        std::fs::write(&target, b"old").unwrap();
        std::fs::write(&staged, b"new").unwrap();
        let expected = agrep_core::ingest::registry::regular_file_reader_identity(&target)
            .unwrap()
            .unwrap();
        let held = std::fs::OpenOptions::new()
            .read(true)
            .share_mode(FILE_SHARE_READ)
            .open(&target)
            .unwrap();
        let release = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(120));
            drop(held);
        });
        super::promote_corpus_takeover(&staged, &target, &expected).unwrap();
        release.join().unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"new");
        assert!(!staged.exists());
        let _ = std::fs::remove_dir_all(data);
    }
    fn retained_schema_connection(family_sql: &str) -> rusqlite::Connection {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection
            .execute_batch(&format!(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                 INSERT INTO meta VALUES('schema', '14');
                 INSERT INTO meta VALUES('stamp', 'retained-stamp');
                 CREATE TABLE msgs(
                     id INTEGER PRIMARY KEY,
                     session TEXT NOT NULL, turn INTEGER, ts INTEGER,
                     agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
                     who TEXT, text TEXT,
                     fts_text TEXT CHECK(
                         fts_text IS NULL OR instr(fts_text, char(0)) = 0),
                     content_digest TEXT CHECK(
                         content_digest IS NULL OR
                         (length(content_digest) = 4 AND
                          content_digest NOT GLOB '*[^0-9a-f]*')));
                 {family_sql};
                 CREATE TABLE boundary_stats(
                     token TEXT PRIMARY KEY, n INTEGER NOT NULL, s INTEGER NOT NULL,
                     q INTEGER NOT NULL) WITHOUT ROWID;
                 CREATE VIEW msgs_fts_content AS
                     SELECT id, coalesce(fts_text, text) AS text FROM msgs;
                 CREATE VIEW msgs_prose_fts_content AS
                     SELECT id, coalesce(fts_text, text) AS text
                     FROM msgs WHERE who <> 'tool';
                 CREATE VIRTUAL TABLE msgs_fts USING fts5(
                     text, content='msgs_fts_content',
                     content_rowid='id', tokenize='trigram');
                 CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
                     text, content='msgs_prose_fts_content',
                     content_rowid='id', tokenize='trigram');"
            ))
            .unwrap();
        connection
    }

    #[test]
    fn retained_reader_accepts_both_valid_family_table_generations() {
        for family_sql in [
            "CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL) WITHOUT ROWID",
            "CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL,
                side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID",
        ] {
            let connection = retained_schema_connection(family_sql);
            super::validate_corpus_reader_schema(&connection).unwrap();
        }

        let connection = retained_schema_connection(
            "CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL,
                stale INTEGER NOT NULL) WITHOUT ROWID",
        );
        let error = super::validate_corpus_reader_schema(&connection).unwrap_err();
        assert!(format!("{error:?}").contains("session_family"), "{error:?}");
    }

    #[test]
    fn retained_reader_rejects_the_schema_13_fts_layout() {
        let connection = retained_schema_connection(
            "CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL) WITHOUT ROWID",
        );
        connection
            .execute_batch(
                "DROP TABLE msgs_fts;
             DROP TABLE msgs_prose_fts;
             DROP VIEW msgs_fts_content;
             DROP VIEW msgs_prose_fts_content;
             CREATE VIRTUAL TABLE msgs_fts USING fts5(
                 text, content='msgs', content_rowid='id', tokenize='trigram');
             CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
                 text, content='msgs', content_rowid='id', tokenize='trigram');",
            )
            .unwrap();
        let error = super::validate_corpus_reader_schema(&connection).unwrap_err();
        assert!(
            format!("{error:?}").contains("search schema inventory failed"),
            "{error:?}"
        );
    }

    #[test]
    fn corpus_adoption_publish_failure_preserves_the_original_database() {
        let data = std::env::temp_dir().join(format!(
            "agrep-corpus-publish-failure-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let corpus = data.join("corpus.db");
        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        let foreign = if current == "aaaaaaaaaaaaaaaaaaaa" {
            "bbbbbbbbbbbbbbbbbbbb"
        } else {
            "aaaaaaaaaaaaaaaaaaaa"
        };
        let connection = rusqlite::Connection::open(&corpus).unwrap();
        connection
            .execute_batch(&format!(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                 INSERT INTO meta(key, value) VALUES('build_id', '{foreign}');
                 INSERT INTO meta(key, value) VALUES('schema', '15');
                 INSERT INTO meta(key, value) VALUES('stamp', 'publish-failure-stamp');
                 INSERT INTO meta(key, value) VALUES('fts_triggers', '4');
                 CREATE TABLE msgs(
                     id INTEGER PRIMARY KEY, session TEXT NOT NULL, turn INTEGER, ts INTEGER,
                     agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
                     who TEXT, text TEXT,
                     fts_text TEXT CHECK(
                         fts_text IS NULL OR instr(fts_text, char(0)) = 0),
                     content_digest TEXT CHECK(
                         content_digest IS NULL OR
                         (length(content_digest) = 4 AND
                          content_digest NOT GLOB '*[^0-9a-f]*')));
                 CREATE INDEX msgs_session ON msgs(session, turn);
                 CREATE INDEX msgs_transcript_session_turn ON msgs(session, turn)
                     WHERE who <> 'tool';
                 CREATE INDEX msgs_who_ts ON msgs(who, coalesce(ts, 0) DESC);
                 CREATE INDEX msgs_re_i_exceptions ON msgs(id) WHERE
                     instr(text, 'İ') > 0 OR instr(text, 'ı') > 0
                     OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0;
                 CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT);
                 CREATE TABLE session_family(
                     session TEXT PRIMARY KEY, root TEXT NOT NULL,
                     side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID;
                 CREATE INDEX session_family_root ON session_family(root);
                 CREATE TABLE boundary_stats(
                     token TEXT PRIMARY KEY, n INTEGER NOT NULL, s INTEGER NOT NULL,
                     q INTEGER NOT NULL) WITHOUT ROWID;
                 INSERT INTO msgs(session, who, text)
                     VALUES('session', 'user', 'ownership probe text');
                 CREATE VIEW msgs_fts_content AS
                     SELECT id, coalesce(fts_text, text) AS text FROM msgs;
                 CREATE VIEW msgs_prose_fts_content AS
                     SELECT id, coalesce(fts_text, text) AS text
                     FROM msgs WHERE who <> 'tool';
                 CREATE VIRTUAL TABLE msgs_fts USING fts5(
                     text, content='msgs_fts_content', content_rowid='id', tokenize='trigram');
                 CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
                     text, content='msgs_prose_fts_content', content_rowid='id', tokenize='trigram');
                 INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild');
                 INSERT INTO msgs_prose_fts(msgs_prose_fts) VALUES('rebuild');
                 CREATE TRIGGER msgs_ai AFTER INSERT ON msgs BEGIN
                     INSERT INTO msgs_fts(rowid, text)
                         VALUES(new.id, coalesce(new.fts_text, new.text)); END;
                 CREATE TRIGGER msgs_ad AFTER DELETE ON msgs BEGIN
                     INSERT INTO msgs_fts(msgs_fts, rowid, text)
                         VALUES('delete', old.id, coalesce(old.fts_text, old.text)); END;
                 CREATE TRIGGER msgs_au AFTER UPDATE OF text, fts_text ON msgs
                 WHEN coalesce(old.fts_text, old.text) IS NOT
                      coalesce(new.fts_text, new.text) BEGIN
                     INSERT INTO msgs_fts(msgs_fts, rowid, text)
                         VALUES('delete', old.id, coalesce(old.fts_text, old.text));
                     INSERT INTO msgs_fts(rowid, text)
                         VALUES(new.id, coalesce(new.fts_text, new.text)); END;
                 CREATE TRIGGER msgs_prose_ai AFTER INSERT ON msgs
                 WHEN new.who <> 'tool' BEGIN
                     INSERT INTO msgs_prose_fts(rowid, text)
                         VALUES(new.id, coalesce(new.fts_text, new.text)); END;
                 CREATE TRIGGER msgs_prose_ad AFTER DELETE ON msgs
                 WHEN old.who <> 'tool' BEGIN
                     INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                         VALUES('delete', old.id, coalesce(old.fts_text, old.text)); END;
                 CREATE TRIGGER msgs_prose_au_old AFTER UPDATE OF text, fts_text, who ON msgs
                 WHEN old.who <> 'tool'
                      AND (coalesce(old.fts_text, old.text) IS NOT
                           coalesce(new.fts_text, new.text) OR new.who = 'tool') BEGIN
                     INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                         VALUES('delete', old.id, coalesce(old.fts_text, old.text));
                     INSERT INTO msgs_prose_fts(rowid, text)
                         SELECT new.id, coalesce(new.fts_text, new.text)
                         WHERE new.who <> 'tool'; END;
                 CREATE TRIGGER msgs_prose_au_new AFTER UPDATE OF text, fts_text, who ON msgs
                 WHEN old.who = 'tool' AND new.who <> 'tool' BEGIN
                     INSERT INTO msgs_prose_fts(rowid, text)
                         VALUES(new.id, coalesce(new.fts_text, new.text)); END;"
            ))
            .unwrap();
        drop(connection);
        let before = std::fs::read(&corpus).unwrap();

        let refusal = adopt_foreign_corpus_db_with(
            &corpus,
            &current,
            foreign,
            |staged, target, _expected| {
                assert_eq!(staged.parent(), target.parent());
                assert!(staged
                    .file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("corpus.db.tmp."));
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt as _;
                    assert_eq!(
                        std::fs::metadata(staged).unwrap().permissions().mode() & 0o777,
                        0o600
                    );
                }
                Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "injected publish refusal",
                ))
            },
        )
        .unwrap_err();
        assert!(
            matches!(refusal, CorpusDbAdoptionError::Fatal(_)),
            "{refusal:?}"
        );
        assert_eq!(std::fs::read(&corpus).unwrap(), before);
        assert_eq!(
            rusqlite::Connection::open(&corpus)
                .unwrap()
                .query_row("SELECT value FROM meta WHERE key='build_id'", [], |row| {
                    row.get::<_, String>(0)
                })
                .unwrap(),
            foreign
        );
        assert!(std::fs::read_dir(&data).unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with("corpus.db.tmp.")));
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn wal_owner_probe_never_creates_live_shm_or_mutates_the_store() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-wal-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        agrep_core::ingest_cache::IngestCache::cold()
            .stage_save(&data.join(".ingest_cache.bin"))
            .unwrap()
            .commit()
            .unwrap();

        let current = agrep_core::ingest_cache::current_cache_writer_build_id();
        let foreign = if current == "aaaaaaaaaaaaaaaaaaaa" {
            "bbbbbbbbbbbbbbbbbbbb"
        } else {
            "aaaaaaaaaaaaaaaaaaaa"
        };
        let seed = data.join("seed.db");
        let writer = rusqlite::Connection::open(&seed).unwrap();
        assert_eq!(
            writer
                .query_row("PRAGMA journal_mode=WAL", [], |row| row.get::<_, String>(0))
                .unwrap(),
            "wal"
        );
        writer
            .execute_batch("PRAGMA wal_autocheckpoint=0;")
            .unwrap();
        writer
            .execute_batch(&format!(
                "BEGIN;
                 CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                 INSERT INTO meta(key, value) VALUES('build_id', '{foreign}');
                 COMMIT;"
            ))
            .unwrap();
        let seed_wal = PathBuf::from(format!("{}-wal", seed.display()));
        assert!(seed_wal.exists());

        let corpus = data.join("corpus.db");
        let corpus_wal = PathBuf::from(format!("{}-wal", corpus.display()));
        let corpus_shm = PathBuf::from(format!("{}-shm", corpus.display()));
        std::fs::copy(&seed, &corpus).unwrap();
        std::fs::copy(&seed_wal, &corpus_wal).unwrap();
        assert!(!corpus_shm.exists());

        let names = || {
            let mut names = std::fs::read_dir(&data)
                .unwrap()
                .map(|entry| entry.unwrap().file_name())
                .collect::<Vec<_>>();
            names.sort();
            names
        };
        let names_before = names();
        let main_before =
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus, 512).unwrap();
        let wal_before =
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus_wal, 512).unwrap();

        assert_eq!(
            probe_corpus_db_owner(&corpus, &current),
            CorpusDbOwnerProbe::Foreign(foreign.to_string()),
            "the private family probe must observe an owner committed only in WAL"
        );
        assert!(
            !corpus_shm.exists(),
            "ownership probe created corpus.db-shm"
        );
        assert_eq!(names(), names_before);
        assert_eq!(
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus, 512).unwrap(),
            main_before
        );
        assert_eq!(
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus_wal, 512).unwrap(),
            wal_before
        );

        let DerivedWriteOwnership::Foreign(reason) = derived_write_ownership(&data) else {
            panic!("a WAL-only foreign database owner must refuse adoption");
        };
        assert!(
            reason.contains(&format!("corpus.db owned-by {foreign}")),
            "{reason}"
        );
        assert!(!corpus_shm.exists());
        assert_eq!(names(), names_before);
        assert_eq!(
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus, 512).unwrap(),
            main_before
        );
        assert_eq!(
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus_wal, 512).unwrap(),
            wal_before
        );

        std::fs::write(
            data.join(DERIVED_OWNER_FILE),
            serde_json::to_vec(&DerivedOwnerRecord {
                version: DERIVED_OWNER_VERSION,
                build_id: current,
                legacy_corpus_db: None,
                retained_corpus_db: None,
            })
            .unwrap(),
        )
        .unwrap();
        let names_after_anchor = names();
        let DerivedWriteOwnership::Foreign(reason) = derived_write_ownership(&data) else {
            panic!("a current anchor must not hide a WAL-only foreign database owner");
        };
        assert!(
            reason.contains(&format!("corpus.db owned-by {foreign}")),
            "{reason}"
        );
        assert!(!corpus_shm.exists());
        assert_eq!(names(), names_after_anchor);
        assert_eq!(
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus, 512).unwrap(),
            main_before
        );
        assert_eq!(
            agrep_core::ingest::registry::regular_file_edge_snapshot(&corpus_wal, 512).unwrap(),
            wal_before
        );

        drop(writer);
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn current_cache_identity_keeps_owner_crash_window_repairable() {
        let data = std::env::temp_dir().join(format!(
            "agrep-derived-owner-current-cache-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        agrep_core::ingest_cache::IngestCache::cold()
            .stage_save(&data.join(".ingest_cache.bin"))
            .unwrap()
            .commit()
            .unwrap();
        std::fs::write(data.join("corpus.db"), b"damaged sqlite bytes").unwrap();

        assert_eq!(
            derived_write_ownership(&data),
            DerivedWriteOwnership::Adoption,
            "the current cache is an independent same-build crash-window proof"
        );

        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn missing_derived_directory_has_no_daemon_owner() {
        let data = std::env::temp_dir().join(format!(
            "agrep-missing-derived-owner-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        assert!(!data.exists());
        assert_eq!(super::adoption_daemon_fence(&data), None);
    }

    #[test]
    fn dead_adoption_claim_is_reclaimed_before_fencing() {
        let data = std::env::temp_dir().join(format!(
            "agrep-dead-adoption-claim-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();

        let dead_claim = data.join(".indexd.v2.lock");
        let raw = format!(
            "state=derived-adoption pid={} start=fixture writer={} token={}\n",
            super::index_lock::MAX_PID,
            "e".repeat(20),
            "a".repeat(32),
        );
        std::fs::write(&dead_claim, raw).unwrap();
        assert_eq!(
            super::adoption_daemon_fence(&data),
            None,
            "dead adoption claim must be reclaimed; fence must pass"
        );
        assert!(
            !dead_claim.exists(),
            "dead adoption claim must be removed by the reclaim"
        );

        let _ = std::fs::remove_dir_all(&data);
    }

    #[test]
    fn live_adoption_claim_from_different_build_blocks_fencing() {
        let data = std::env::temp_dir().join(format!(
            "agrep-live-adoption-block-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();

        let live_claim = data.join(".indexd.v2.lock");
        let start =
            super::index_lock::current_process_start_identity().unwrap_or_else(|| "unknown".into());
        let raw = format!(
            "state=derived-adoption pid={} start={start} writer={} token={}\n",
            std::process::id(),
            "f".repeat(20),
            "b".repeat(32),
        );
        std::fs::write(&live_claim, raw.as_bytes()).unwrap();
        let reason = super::adoption_daemon_fence(&data);
        assert!(
            reason.is_some(),
            "live adoption claim from a different build must block the fence"
        );
        assert!(
            live_claim.exists(),
            "live adoption claim must not be removed"
        );

        let _ = std::fs::remove_dir_all(&data);
    }

    #[test]
    fn missing_staging_directory_is_an_empty_inventory() {
        let root = std::env::temp_dir().join(format!(
            "agrep-missing-staging-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        assert_eq!(
            sweep_staging_temps_with(&root, |_| Some(false)).unwrap(),
            (0, 0)
        );
    }

    #[test]
    fn pasted_recap_words_remain_user_authored_without_ingest_provenance() {
        let message = agrep_core::model::RawMessage {
            agent: "claude",
            project: "repo".into(),
            session: "session".into(),
            ts: 1,
            turn: 0,
            text: "This session is being continued from a previous conversation pasted by user"
                .into(),
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: false,
            parent: String::new(),
        }
        .freeze();
        let normalized = normalize_messages(vec![message], &[]);
        assert_eq!(&*normalized[0].who, "user");
    }

    #[test]
    fn explicit_data_dir_wins_and_missing_defaults_are_actionable() {
        let explicit = std::path::PathBuf::from("chosen-data");
        assert_eq!(
            resolve_data_dir(Some(explicit.clone()), Some("ignored".into())).unwrap(),
            explicit
        );
        let error = resolve_data_dir(None, None).unwrap_err().to_string();
        assert!(error.contains("set AGREP_DATA_DIR explicitly"));
    }

    #[test]
    fn relative_data_dir_is_bound_once_to_the_initial_cwd() {
        let cwd = std::path::Path::new(if cfg!(windows) {
            r"C:\initial\cwd"
        } else {
            "/initial/cwd"
        });
        assert_eq!(
            absolute_data_dir("relative-data".into(), cwd),
            cwd.join("relative-data")
        );
        let absolute = cwd.join("explicit-data");
        assert_eq!(absolute_data_dir(absolute.clone(), cwd), absolute);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_default_data_dir_is_user_scoped() {
        assert_eq!(
            platform_data_dir_from(Some("/Users/tester".into())).unwrap(),
            std::path::PathBuf::from("/Users/tester/Library/Application Support/agrep")
        );
    }

    #[cfg(unix)]
    #[test]
    fn default_data_dir_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;

        let path = std::env::temp_dir().join(format!(
            "agrep-test-data-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&path).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        protect_default_data_dir(&path).unwrap();
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let _ = std::fs::remove_dir_all(path);
    }

    #[cfg(windows)]
    #[test]
    fn windows_default_data_dir_prefers_local_app_data() {
        assert_eq!(
            platform_data_dir_from(Some(r"C:\Users\tester\AppData\Local".into()), None).unwrap(),
            std::path::PathBuf::from(r"C:\Users\tester\AppData\Local\agrep")
        );
    }

    #[cfg(not(any(windows, target_os = "macos")))]
    #[test]
    fn unix_default_data_dir_honors_xdg() {
        assert_eq!(
            platform_data_dir_from(Some("/tmp/xdg".into()), Some("/home/tester".into())).unwrap(),
            std::path::PathBuf::from("/tmp/xdg/agrep")
        );
    }

    #[test]
    fn changed_session_marker_read_error_is_not_overwritten() {
        let data = std::env::temp_dir().join(format!(
            "agrep_changed_marker_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let marker = data.join(".changed_sessions");
        std::fs::write(&marker, [0xff, 0xfe]).unwrap();
        let cache = agrep_core::ingest_cache::IngestCache::cold();
        assert!(write_changed_sessions(&data, false, &cache, &[], &HashSet::new()).is_err());
        assert_eq!(std::fs::read(&marker).unwrap(), [0xff, 0xfe]);
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn optional_read_errors_name_the_path() {
        let path = std::env::temp_dir().join(format!(
            "agrep_pathful_read_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&path).unwrap();
        let error = read_optional_bytes(&path, 1024).unwrap_err().to_string();
        assert!(error.contains(&path.display().to_string()));
        let _ = std::fs::remove_dir_all(path);
    }

    #[cfg(unix)]
    #[test]
    fn ingest_markers_reject_fifos_without_waiting() {
        let data = std::env::temp_dir().join(format!(
            "agrep_marker_fifo_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();

        let optional = data.join("optional");
        fifo(&optional);
        assert!(read_optional_bytes(&optional, 1024).is_err());

        let signature = data.join("signature");
        fifo(&signature);
        assert!(!regular_file_has_bytes(&signature, b"expected").unwrap());

        let proof = data.join(super::DERIVED_PROOF_FILE);
        fifo(&proof);
        assert!(!derived_generation_valid(&data, "signature"));

        let changed = data.join(".changed_sessions");
        fifo(&changed);
        let cache = agrep_core::ingest_cache::IngestCache::cold();
        assert!(write_changed_sessions(&data, false, &cache, &[], &HashSet::new()).is_err());
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn derived_validation_rechecks_files_after_the_first_proof() {
        let data = std::env::temp_dir().join(format!(
            "agrep_derived_recheck_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        for name in derived_names() {
            std::fs::write(data.join(name), format!("stable {name}")).unwrap();
        }
        publish_derived_proof(&data, "signature").unwrap();
        let mut passes = 0;
        let valid = derived_generation_valid_with(&data, "signature", || {
            let proof = derived_proof(&data, "signature")?;
            passes += 1;
            if passes == 1 {
                std::fs::write(data.join("messages.jsonl"), b"moved after first proof")?;
            }
            Ok(proof)
        });
        assert!(!valid);
        assert_eq!(passes, 2);
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn legacy_generation_validation_accepts_only_the_exact_stable_v4_shape() {
        let data = std::env::temp_dir().join(format!(
            "agrep_legacy_generation_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        for name in super::legacy_derived_names() {
            std::fs::write(data.join(name), format!("stable {name}")).unwrap();
        }
        let signature = "6:legacy-proof";
        let proof = super::legacy_derived_proof(&data, signature).unwrap();
        std::fs::write(
            data.join(super::DERIVED_PROOF_FILE),
            serde_json::to_vec(&proof).unwrap(),
        )
        .unwrap();
        assert!(super::legacy_derived_generation_valid(&data, signature));

        std::fs::write(
            data.join(agrep_core::cache::SESSION_FAMILY_META_FILE),
            b"current-family-proof",
        )
        .unwrap();
        assert!(!super::legacy_derived_generation_valid(&data, signature));
        std::fs::remove_file(data.join(agrep_core::cache::SESSION_FAMILY_META_FILE)).unwrap();

        std::fs::write(data.join("messages.jsonl"), b"legacy output moved").unwrap();
        assert!(!super::legacy_derived_generation_valid(&data, signature));
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn store_audit_schema_is_exact_and_durable_health_moves_boundary() {
        let view = agrep_core::ingest::registry::SourceSnapshotAuditView {
            paths: vec![agrep_core::ingest::registry::SourceSnapshotAuditPath {
                agent: "codex".into(),
                path: PathBuf::from("/history/session.jsonl"),
                stat_key: "s:1:2".into(),
                identity_sha256: [7; 32],
            }],
            tokens: vec![("cursor".into(), "session-id".into(), "token".into())],
            issues: Vec::new(),
            complete: true,
        };
        let clean =
            store_audit_payload(b"opaque-snapshot", "all", view.clone(), Vec::new()).unwrap();
        let issue = serde_json::json!({
            "agent": "codex",
            "path": "/history/session.jsonl",
            "kind": "permission-denied",
            "reason": "denied",
        });
        let degraded =
            store_audit_payload(b"opaque-snapshot", "all", view, vec![issue.clone(), issue])
                .unwrap();
        let scoped = store_audit_payload(
            b"opaque-snapshot",
            "codex",
            agrep_core::ingest::registry::SourceSnapshotAuditView {
                paths: Vec::new(),
                tokens: Vec::new(),
                issues: Vec::new(),
                complete: true,
            },
            vec![serde_json::json!({
                "agent": "cursor",
                "path": "/cursor/state.vscdb",
                "kind": "permission-denied",
                "reason": "denied",
            })],
        )
        .unwrap();

        let keys: std::collections::BTreeSet<_> = clean
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            std::collections::BTreeSet::from([
                "boundary_sha256",
                "complete",
                "issues",
                "paths",
                "schema",
                "selection",
                "snapshot_sha256",
                "tokens",
                "version",
            ])
        );
        assert_eq!(clean["schema"], super::STORE_AUDIT_SCHEMA);
        assert_eq!(clean["version"], super::STORE_AUDIT_VERSION);
        assert_eq!(clean["selection"], "all");
        assert_eq!(
            clean["paths"][0]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<std::collections::BTreeSet<_>>(),
            std::collections::BTreeSet::from(["identity_sha256", "name", "path", "stat_key",])
        );
        for key in ["snapshot_sha256", "boundary_sha256"] {
            let digest = clean[key].as_str().unwrap();
            assert_eq!(digest.len(), 64);
            assert!(digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)));
        }
        assert_eq!(clean["complete"], true);
        assert_eq!(degraded["complete"], false);
        assert_eq!(degraded["issues"].as_array().unwrap().len(), 1);
        assert_eq!(clean["snapshot_sha256"], degraded["snapshot_sha256"]);
        assert_ne!(clean["boundary_sha256"], degraded["boundary_sha256"]);
        assert_eq!(scoped["selection"], "codex");
        assert_eq!(scoped["complete"], true);
        assert!(scoped["issues"].as_array().unwrap().is_empty());
        assert!(serialize_store_audit_payload(&clean, 1)
            .unwrap_err()
            .to_string()
            .contains("output exceeds"));
    }

    #[test]
    fn store_audit_rejects_malformed_snapshot_and_conflicting_modes() {
        assert!(decoded_store_audit_payload(b"not-bincode", "all", Vec::new()).is_err());
        assert!(Cli::try_parse_from(["agrep-rs", "stores", "--audit", "--paths"]).is_err());
        assert!(Cli::try_parse_from(["agrep-rs", "stores", "--audit", "--tokens"]).is_err());
        assert!(Cli::try_parse_from(["agrep-rs", "stores", "--agent", "codex"]).is_err());
        assert!(agrep_core::ingest::registry::source_audit_snapshot("unknown-adapter").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn store_audit_marks_non_unicode_content_path_incomplete() {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt;

        let view = agrep_core::ingest::registry::SourceSnapshotAuditView {
            paths: vec![agrep_core::ingest::registry::SourceSnapshotAuditPath {
                agent: "codex".into(),
                path: PathBuf::from(OsString::from_vec(b"rollout-\xff.jsonl".to_vec())),
                stat_key: "s:1:2".into(),
                identity_sha256: [7; 32],
            }],
            tokens: Vec::new(),
            issues: Vec::new(),
            complete: true,
        };
        let payload = store_audit_payload(b"exact-native-path", "all", view, Vec::new()).unwrap();
        assert_eq!(payload["complete"], false);
        assert!(payload["paths"].as_array().unwrap().is_empty());
        assert_eq!(payload["issues"][0]["kind"], "path-unrepresentable");
    }

    #[cfg(windows)]
    #[test]
    fn store_audit_marks_unpaired_utf16_content_path_incomplete() {
        use std::ffi::OsString;
        use std::os::windows::ffi::OsStringExt;

        let view = agrep_core::ingest::registry::SourceSnapshotAuditView {
            paths: vec![agrep_core::ingest::registry::SourceSnapshotAuditPath {
                agent: "codex".into(),
                path: PathBuf::from(OsString::from_wide(&[
                    0x43, 0x3a, 0x5c, 0xd800, 0x2e, 0x6a, 0x73, 0x6f, 0x6e, 0x6c,
                ])),
                stat_key: "s:1:2".into(),
                identity_sha256: [7; 32],
            }],
            tokens: Vec::new(),
            issues: Vec::new(),
            complete: true,
        };
        let payload = store_audit_payload(b"exact-native-path", "all", view, Vec::new()).unwrap();
        assert_eq!(payload["complete"], false);
        assert!(payload["paths"].as_array().unwrap().is_empty());
        assert_eq!(payload["issues"][0]["kind"], "path-unrepresentable");
    }

    #[test]
    fn scoped_source_recovery_preserves_other_agent_failures() {
        let data = std::env::temp_dir().join(format!(
            "agrep_source_health_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        write_source_health(
            &data,
            vec![
                serde_json::json!({"agent": "claude", "path": "claude", "kind": "io", "reason": "denied"}),
                serde_json::json!({"agent": "cline", "path": "cline", "kind": "io", "reason": "denied"}),
            ],
        )
        .unwrap();

        clear_source_health(&data, "cline").unwrap();
        let marker: serde_json::Value =
            serde_json::from_slice(&std::fs::read(data.join(super::SOURCE_HEALTH_FILE)).unwrap())
                .unwrap();
        assert_eq!(marker["issues"].as_array().unwrap().len(), 1);
        assert_eq!(marker["issues"][0]["agent"], "claude");
        clear_source_health(&data, "claude").unwrap();
        assert!(!data.join(super::SOURCE_HEALTH_FILE).exists());
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn fallback_source_health_names_an_actionable_store_root() {
        let data = std::env::temp_dir().join(format!(
            "agrep_source_fallback_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        publish_source_unreadable(&data, "cursor", &[], &[], "snapshot failed").unwrap();
        let records = source_health_records(&data).unwrap();
        assert_eq!(records.len(), 1);
        let path = records[0]["path"].as_str().unwrap();
        assert!(!path.is_empty());
        assert_ne!(path, ".");
        let _ = std::fs::remove_dir_all(data);
    }

    #[cfg(unix)]
    #[test]
    fn runtime_source_health_serializes_non_utf8_path_without_panicking() {
        use std::os::unix::ffi::OsStringExt;

        let path = std::path::PathBuf::from(std::ffi::OsString::from_vec(
            b"/tmp/agrep-source-\xff.jsonl".to_vec(),
        ));
        let issue = agrep_core::ingest_cache::SourceReadIssue {
            agent: "claude",
            path,
            kind: "source-read-failed",
            reason: "permission denied".to_string(),
        };
        let first = runtime_source_issue_value(&issue);
        let second = runtime_source_issue_value(&issue);
        assert_eq!(first, second);
        assert_eq!(first["agent"], "claude");
        assert!(first["path"].as_str().unwrap().contains('\u{fffd}'));
    }

    #[test]
    fn global_source_health_marks_every_visible_store_unreadable() {
        let mut stores = vec![
            serde_json::json!({
                "name": "claude", "files": 1, "state": "available", "issues": []
            }),
            serde_json::json!({
                "name": "cursor", "files": 1, "state": "available", "issues": []
            }),
        ];
        let issue = serde_json::json!({
            "agent": "all",
            "path": "/fixture/history",
            "kind": "source-read-incomplete",
            "reason": "read failed",
        });
        apply_durable_store_issues(&mut stores, vec![issue.clone()]);
        assert!(stores
            .iter()
            .all(|store| store["state"] == "source-unreadable"));
        assert!(stores.iter().all(|store| {
            store["issues"]
                .as_array()
                .is_some_and(|issues| issues == std::slice::from_ref(&issue))
        }));
    }

    #[cfg(windows)]
    #[test]
    fn runtime_source_health_serializes_unpaired_utf16_without_panicking() {
        use std::os::windows::ffi::OsStringExt;

        let path = std::path::PathBuf::from(std::ffi::OsString::from_wide(&[
            b'C' as u16,
            b':' as u16,
            b'\\' as u16,
            0xd800,
        ]));
        let issue = agrep_core::ingest_cache::SourceReadIssue {
            agent: "codex",
            path,
            kind: "source-read-failed",
            reason: "sharing violation".to_string(),
        };
        let first = runtime_source_issue_value(&issue);
        assert_eq!(first, runtime_source_issue_value(&issue));
        assert_eq!(first["agent"], "codex");
        assert!(first["path"].as_str().unwrap().contains('\u{fffd}'));
    }

    #[test]
    fn source_publication_preserves_last_good_until_pending_retry_is_complete() {
        let data = std::env::temp_dir().join(format!(
            "agrep_source_pending_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let source = data.join(".source_snapshot.bin");
        let policy = data.join(".harness_prefixes.snapshot");
        let pending = data.join(".ingest_pending.bin");
        std::fs::write(&source, b"last-good").unwrap();
        std::fs::write(&pending, b"attempted-preflight").unwrap();

        assert_eq!(
            publish_source_snapshot(None, &source, &policy, &pending).unwrap(),
            SourcePublishOutcome::default()
        );
        assert_eq!(std::fs::read(&source).unwrap(), b"last-good");
        assert_eq!(std::fs::read(&pending).unwrap(), b"attempted-preflight");

        let outcome = publish_source_snapshot(
            Some((b"validated".to_vec(), b"policy".to_vec())),
            &source,
            &policy,
            &pending,
        )
        .unwrap();
        assert_eq!(
            outcome,
            SourcePublishOutcome {
                policy_written: true,
                pending_promoted: false,
            }
        );
        assert_eq!(std::fs::read(&source).unwrap(), b"validated");
        assert_eq!(std::fs::read(&policy).unwrap(), b"policy");
        assert_eq!(read_optional_bytes(&pending, 1024).unwrap(), None);

        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn source_publication_promotes_matching_pending_without_rewriting_policy() {
        let data = std::env::temp_dir().join(format!(
            "agrep_source_promote_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let source = data.join(".source_snapshot.bin");
        let policy = data.join(".harness_prefixes.snapshot");
        let pending = data.join(".ingest_pending.bin");
        std::fs::write(&source, b"last-good").unwrap();
        std::fs::write(&policy, b"policy").unwrap();
        std::fs::write(&pending, b"validated").unwrap();

        let outcome = publish_source_snapshot(
            Some((b"validated".to_vec(), b"policy".to_vec())),
            &source,
            &policy,
            &pending,
        )
        .unwrap();

        assert_eq!(
            outcome,
            SourcePublishOutcome {
                policy_written: false,
                pending_promoted: true,
            }
        );
        assert_eq!(std::fs::read(&source).unwrap(), b"validated");
        assert_eq!(std::fs::read(&policy).unwrap(), b"policy");
        assert_eq!(read_optional_bytes(&pending, 1024).unwrap(), None);
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn source_promotion_failure_keeps_old_source_and_pending_after_policy_write() {
        let data = std::env::temp_dir().join(format!(
            "agrep_source_promote_fail_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&data).unwrap();
        let source = data.join(".source_snapshot.bin");
        let policy = data.join(".harness_prefixes.snapshot");
        let pending = data.join(".ingest_pending.bin");
        std::fs::write(&source, b"last-good").unwrap();
        std::fs::write(&policy, b"old-policy").unwrap();
        std::fs::write(&pending, b"validated").unwrap();
        let policy_for_check = policy.clone();

        let error = publish_source_snapshot_with(
            Some((b"validated".to_vec(), b"new-policy".to_vec())),
            &source,
            &policy,
            &pending,
            move |_, _| {
                assert_eq!(std::fs::read(&policy_for_check).unwrap(), b"new-policy");
                anyhow::bail!("injected promotion failure")
            },
        )
        .unwrap_err();

        assert!(error.to_string().contains("injected promotion failure"));
        assert_eq!(std::fs::read(&source).unwrap(), b"last-good");
        assert_eq!(std::fs::read(&pending).unwrap(), b"validated");
        assert_eq!(std::fs::read(&policy).unwrap(), b"new-policy");
        let _ = std::fs::remove_dir_all(data);
    }
}
