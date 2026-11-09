//! Store discovery: data roots, their projects.json registries and the databases they name.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use super::Unenumerable;
use crate::ingest::project_name;
use crate::ingest::registry::{
    metadata_is_link, read_bounded_regular_file, relative_path, root_file, root_metadata,
    stat_proves_absent,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct Database {
    pub(super) path: PathBuf,
    pub(super) project: String,
}

#[derive(Clone, Debug)]
pub(super) struct Discovery {
    pub(super) databases: Vec<Database>,
    /// registered database paths a stat error hid: unavailable, never absent
    pub(super) unavailable: Vec<(PathBuf, Unenumerable)>,
    /// registered database paths a stat proves gone (a volume unmounted, a project renamed):
    /// store roots whose absence two observations must confirm before their chats go
    pub(super) absent: Vec<PathBuf>,
    pub(super) incomplete: bool,
    pub(super) issues: Vec<(PathBuf, String)>,
}

const PROJECTS_JSON_MAX_BYTES: u64 = 4 * 1024 * 1024;

fn data_roots_at(
    home: &Path,
    test_home: bool,
    global_data: Option<OsString>,
    xdg_data: Option<OsString>,
    local_app_data: Option<OsString>,
    windows: bool,
) -> Vec<PathBuf> {
    let legacy = home.join(".local").join("share").join("crush");
    let primary = if test_home {
        legacy.clone()
    } else if let Some(path) = global_data.filter(|value| !value.is_empty()) {
        PathBuf::from(path)
    } else if let Some(path) = xdg_data.filter(|value| !value.is_empty()) {
        PathBuf::from(path).join("crush")
    } else if windows {
        local_app_data
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Local"))
            .join("crush")
    } else {
        legacy.clone()
    };
    if primary == legacy {
        vec![primary]
    } else {
        vec![primary, legacy]
    }
}

pub(super) fn data_roots() -> Vec<PathBuf> {
    data_roots_at(
        &crate::ingest::home(),
        std::env::var_os("AGREP_HOME").is_some(),
        std::env::var_os("CRUSH_GLOBAL_DATA"),
        std::env::var_os("XDG_DATA_HOME"),
        std::env::var_os("LOCALAPPDATA"),
        cfg!(target_os = "windows"),
    )
}

/// The primary data root, where store-wide read issues are filed.
pub(super) fn runtime_root() -> PathBuf {
    data_roots()
        .into_iter()
        .next()
        .unwrap_or_else(crate::ingest::home)
}

/// Whether `path` is a plain file (or directory): a symlink, the wrong type or a stat error that
/// proves absence is absent; any other failure may hide a store that still holds conversations.
fn presence(path: &Path, directory: bool) -> Result<bool, Unenumerable> {
    observed(path, std::fs::symlink_metadata(path), directory)
}

/// [`presence`] of a store root - a data root or a database - which may be a link, followed once;
/// a link that may not be followed is a failure, never an absence.
fn root_presence(path: &Path, directory: bool) -> Result<bool, Unenumerable> {
    let found = if directory {
        root_metadata(path)
    } else {
        root_file(path).map(|(_, meta)| meta)
    };
    observed(path, found, directory)
}

fn observed(
    path: &Path,
    found: std::io::Result<std::fs::Metadata>,
    directory: bool,
) -> Result<bool, Unenumerable> {
    match found {
        Ok(meta) if metadata_is_link(&meta) => {
            crate::ingest::warn_source_skip("crush", path, "symlink sources are not followed");
            Ok(false)
        }
        Ok(meta) => Ok(if directory {
            meta.is_dir()
        } else {
            meta.is_file()
        }),
        Err(error) if stat_proves_absent(path, &error) => Ok(false),
        Err(error) => {
            crate::ingest::warn_source_skip("crush", path, &error);
            Err(Unenumerable::uninspectable(path, &error))
        }
    }
}

fn discovered_plain(path: &Path, directory: bool) -> bool {
    presence(path, directory).unwrap_or(false)
}

/// Whether `path` is a plain directory chain below `anchor`, itself checked by `anchor_present`.
fn dir_below(
    path: &Path,
    anchor: &Path,
    anchor_present: fn(&Path, bool) -> Result<bool, Unenumerable>,
) -> Option<Result<bool, Unenumerable>> {
    let relative = relative_path(path, anchor)?;
    let walk = || {
        if !anchor_present(anchor, true)? {
            return Ok(false);
        }
        let mut current = anchor.to_path_buf();
        for component in relative.components() {
            match component {
                std::path::Component::CurDir => continue,
                std::path::Component::Normal(component) => current.push(component),
                _ => return Ok(false),
            }
            if !presence(&current, true)? {
                return Ok(false);
            }
        }
        Ok(true)
    };
    Some(walk())
}

fn data_dir_present(
    path: &Path,
    registry_root: &Path,
    project_path: &Path,
) -> Result<bool, Unenumerable> {
    dir_below(path, registry_root, root_presence)
        .or_else(|| dir_below(path, project_path, presence))
        .unwrap_or_else(|| presence(path, true))
}

/// What discovery learned of the databases it looked for.
#[derive(Default)]
struct Found {
    databases: Vec<Database>,
    unavailable: Vec<(PathBuf, Unenumerable)>,
    absent: Vec<PathBuf>,
}

impl Found {
    /// The database at `path`, a store root, when its data dir is plain and it is a regular file
    /// or a link to one; a stat failure on either names the database unavailable, so its
    /// conversations are retained rather than pruned, and one that proves it gone names a
    /// `registered` database absent, for two observations to confirm.
    fn observe(
        &mut self,
        path: PathBuf,
        data_dir_present: Result<bool, Unenumerable>,
        project: String,
        registered: bool,
    ) {
        match data_dir_present.and_then(|present| {
            if present {
                root_presence(&path, false)
            } else {
                Ok(false)
            }
        }) {
            Ok(true) => self.databases.push(Database { path, project }),
            Ok(false) if registered => self.absent.push(path),
            Ok(false) => {}
            Err(failure) => self.unavailable.push((path, failure)),
        }
    }

    /// Sorts and dedups each list; a path observed more than once stays only in the first of
    /// databases, unavailable, absent.
    fn settle(&mut self) {
        let Found {
            databases,
            unavailable,
            absent,
        } = self;
        databases.sort_by(|left, right| {
            left.path
                .cmp(&right.path)
                .then_with(|| left.project.cmp(&right.project))
        });
        databases.dedup_by(|left, right| left.path == right.path);
        unavailable.sort_by(|left, right| left.0.cmp(&right.0));
        unavailable.dedup_by(|left, right| left.0 == right.0);
        unavailable.retain(|(path, _)| !databases.iter().any(|database| database.path == *path));
        absent.sort();
        absent.dedup();
        absent.retain(|path| {
            !databases.iter().any(|database| database.path == *path)
                && !unavailable
                    .iter()
                    .any(|(unavailable, _)| unavailable == path)
        });
    }
}

fn add_project_record(
    value: &serde_json::Value,
    fallback_path: Option<&str>,
    registry_root: &Path,
    found: &mut Found,
) {
    let Some(record) = value.as_object() else {
        return;
    };
    let Some(project_raw) = record
        .get("path")
        .and_then(serde_json::Value::as_str)
        .or(fallback_path)
        .filter(|value| !value.is_empty())
    else {
        return;
    };
    let Some(data_raw) = record
        .get("data_dir")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return;
    };
    let project_path = PathBuf::from(project_raw);
    let raw_data = PathBuf::from(data_raw);
    let data_dir = if raw_data.is_absolute() {
        raw_data
    } else if project_path.is_absolute() {
        project_path.join(raw_data)
    } else {
        return;
    };
    found.observe(
        data_dir.join("crush.db"),
        data_dir_present(&data_dir, registry_root, &project_path),
        project_name(project_raw),
        true,
    );
}

/// The registry at `path`, a store root that may be a link to a file, followed once.
fn read_projects(path: &Path, registry_root: &Path, found: &mut Found) -> std::io::Result<()> {
    let source = match root_file(path) {
        Ok((source, _)) => source,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let Some(bytes) = read_bounded_regular_file(&source, PROJECTS_JSON_MAX_BYTES)? else {
        return Ok(());
    };
    let value: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    if let Some(records) = value.as_array() {
        for record in records {
            add_project_record(record, None, registry_root, found);
        }
        return Ok(());
    }
    let Some(object) = value.as_object() else {
        return Ok(());
    };
    if let Some(projects) = object.get("projects") {
        if let Some(records) = projects.as_array() {
            for record in records {
                add_project_record(record, None, registry_root, found);
            }
        } else if let Some(records) = projects.as_object() {
            for (project, record) in records {
                add_project_record(record, Some(project), registry_root, found);
            }
        }
    } else {
        for (project, record) in object {
            add_project_record(record, Some(project), registry_root, found);
        }
    }
    Ok(())
}

/// Crush databases sitting under a root that projects.json never registers.
///
/// Crush writes every store it opens into the registry, so an unregistered database
/// is at best a stale leftover - it is named, never ingested. The scan is shallow
/// (one level of subdirectories, bounded entries) because this is a courtesy, not a
/// second discovery path, and symlinked directories are not descended.
fn unregistered_databases(roots: &[PathBuf], databases: &[Database]) -> Vec<PathBuf> {
    const SCAN_ENTRY_CAP: usize = 64;
    let mut found = Vec::new();
    for root in roots {
        let Ok(entries) = std::fs::read_dir(root) else {
            continue;
        };
        for entry in entries.take(SCAN_ENTRY_CAP).flatten() {
            if !entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                continue;
            }
            let candidate = entry.path().join("crush.db");
            if discovered_plain(&candidate, false)
                && !databases.iter().any(|database| database.path == candidate)
                && !found.contains(&candidate)
            {
                found.push(candidate);
            }
        }
    }
    found.sort();
    found
}

/// One line per unregistered store per process: a single index pass rediscovers through
/// the token and census lanes several times. Never recorded as a source-read issue - the
/// database is readable, it is simply not registered, so a read remedy could not resolve it.
/// A store-root listing drops what it reports, so it leaves the line to a later discovery.
fn warn_unregistered(paths: &[PathBuf]) {
    static WARNED: std::sync::Mutex<std::collections::BTreeSet<PathBuf>> =
        std::sync::Mutex::new(std::collections::BTreeSet::new());
    if crate::ingest::registry::listing_roots() {
        return;
    }
    let Ok(mut warned) = WARNED.lock() else {
        return;
    };
    for path in paths {
        if warned.insert(path.clone()) {
            crate::intake::diagnostic(format_args!(
                "  ! crush: {} exists but is not referenced by projects.json; not ingested",
                crate::ingest::terminal_safe(path.display())
            ));
        }
    }
}

/// Observes one data root's registered databases and its global crush.db. A returned issue
/// leaves discovery incomplete.
fn read_root(root: &Path, found: &mut Found) -> Option<(PathBuf, String)> {
    match root_presence(root, true) {
        Ok(true) => {}
        Ok(false) => return None,
        // The registry below it is unread: every database it names stays unlisted.
        Err(failure) => return Some((root.to_path_buf(), failure.reason)),
    }
    let registry = root.join("projects.json");
    let issue = read_projects(&registry, root, found).err().map(|error| {
        crate::intake::diagnostic(format_args!(
            "  ! crush: cannot read {}: {}",
            crate::ingest::terminal_safe(registry.display()),
            crate::ingest::terminal_safe(&error)
        ));
        (registry, error.to_string())
    });
    found.observe(root.join("crush.db"), Ok(true), "crush".to_string(), false);
    issue
}

fn discover_at(roots: Vec<PathBuf>, home: &Path) -> std::io::Result<Discovery> {
    let mut found = Found::default();
    let mut issues = Vec::new();
    for root in &roots {
        issues.extend(read_root(root, &mut found));
    }
    let old = home.join(".crush").join("crush.db");
    let old_dir_present = old
        .parent()
        .map(|parent| root_presence(parent, true))
        .unwrap_or(Ok(false));
    found.observe(old, old_dir_present, "crush".to_string(), false);
    let unregistered = unregistered_databases(&roots, &found.databases);
    warn_unregistered(&unregistered);
    found.settle();
    Ok(Discovery {
        databases: found.databases,
        unavailable: found.unavailable,
        absent: found.absent,
        incomplete: !issues.is_empty(),
        issues,
    })
}

pub(super) fn discover() -> std::io::Result<Discovery> {
    let home = crate::ingest::home();
    discover_at(data_roots(), &home)
}
