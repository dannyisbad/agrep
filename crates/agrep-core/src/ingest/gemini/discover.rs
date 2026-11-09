//! Finding session files under `~/.gemini/tmp` and the project each belongs to.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};

/// Where a session file sits in the store: the project its chats belong to (see `Projects`)
/// and, for a subagent's file, the session it nests under.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(super) struct Place {
    pub(super) project: String,
    pub(super) parent: String,
}

/// A main-session file name: `session-<time>-<short id>.json(l)`. Activity logs live at
/// `logs/session-<id>.jsonl`, outside `chats`.
pub(super) fn is_session_name(name: &str) -> bool {
    name.starts_with("session-") && (name.ends_with(".json") || name.ends_with(".jsonl"))
}

/// The session a subagent's file nests under: `tmp/<project>/chats/<parent session id>/<agent
/// id>.jsonl` (chatRecordingService.ts 757-799 at fb972b2; the directory is the parent id,
/// sanitized to `[A-Za-z0-9_-]`, which a UUID already is). A main session's file sits in `chats`
/// itself, and the `tmp` anchor keeps a project slugged `chats` from reading its `logs/` as one.
pub(super) fn side_parent(path: &Path) -> Option<String> {
    if path.extension().and_then(|x| x.to_str()) != Some("jsonl") {
        return None;
    }
    let container = path.parent()?;
    let parent = container.file_name()?.to_str()?;
    let chats = container.parent()?;
    if parent == "chats" || chats.file_name()? != "chats" {
        return None;
    }
    let store = chats.parent()?.parent()?;
    (store.file_name()? == "tmp").then(|| parent.to_string())
}

/// The directory under the store root that holds a session file's project: `chats`' parent.
fn project_dir(path: &Path) -> Option<&Path> {
    let mut dir = path.parent()?;
    if side_parent(path).is_some() {
        dir = dir.parent()?;
    }
    (dir.file_name()? == "chats")
        .then(|| dir.parent())
        .flatten()
}

/// Upstream's ownership marker: each project directory under `tmp` holds the absolute project
/// path in `.project_root` (projectRegistry.ts 24, 372-412 at fb972b2), written before any chat.
/// A marker or a registry key longer than a path can be is foreign and names nothing: what it
/// named would be stamped on every row of its project.
const PROJECT_ROOT_MARKER: &str = ".project_root";
const PROJECT_PATH_MAX_BYTES: u64 = 4096;
const PROJECT_FILE_MAX_BYTES: u64 = 8 * 1024 * 1024;

/// The project path a marker file names, when it is a plain file no longer than a path.
fn marker_path(marker: &Path) -> Option<String> {
    let meta = fs::symlink_metadata(marker).ok()?;
    if !meta.is_file() || metadata_is_link(&meta) || meta.len() > PROJECT_PATH_MAX_BYTES {
        return None;
    }
    let text = crate::ingest::read_lossy(marker).ok()?;
    let line = text.lines().next()?.trim();
    (!line.is_empty()).then(|| line.to_string())
}

/// The whole text of a plain, small file.
fn marker_path_file(path: &Path) -> Option<String> {
    let meta = fs::symlink_metadata(path).ok()?;
    if !meta.is_file() || metadata_is_link(&meta) || meta.len() > PROJECT_FILE_MAX_BYTES {
        return None;
    }
    crate::ingest::read_lossy(path).ok()
}

/// Projects by store directory name, read once per pass: each directory's own marker, the slugs
/// `~/.gemini/projects.json` registers (projectRegistry.ts 16-22: `{projects: {path: slug}}`)
/// and, for the hash-named directories gemini-cli wrote before 2026-02 and copies on migration
/// (storage.ts 310-324), the SHA256 of every known project path.
#[derive(Default)]
pub(super) struct Projects(HashMap<String, String>);

impl Projects {
    /// From the markers under `root` (`~/.gemini/tmp`) and the registry beside it.
    pub(super) fn discover(root: &Path) -> Self {
        let mut projects = HashMap::new();
        let mut paths: Vec<String> = Vec::new();
        if let Ok(dirs) = fs::read_dir(root) {
            for entry in dirs.flatten() {
                let Some(path) = marker_path(&entry.path().join(PROJECT_ROOT_MARKER)) else {
                    continue;
                };
                if let Some(dir) = entry.file_name().to_str() {
                    projects.insert(dir.to_string(), crate::ingest::project_name(&path));
                }
                paths.push(path);
            }
        }
        if let Some(registry) = root.parent().map(|dir| dir.join("projects.json")) {
            Self::read_registry(&registry, &mut projects, &mut paths);
        }
        for path in paths {
            let hash = crate::store::digest(&path);
            projects
                .entry(hash)
                .or_insert_with(|| crate::ingest::project_name(&path));
        }
        Self(projects)
    }

    /// The slugs `projects.json` registers, after the markers; every listed path is known.
    fn read_registry(
        registry: &Path,
        projects: &mut HashMap<String, String>,
        paths: &mut Vec<String>,
    ) {
        let Some(text) = marker_path_file(registry) else {
            return;
        };
        let Ok(Value::Object(fields)) = serde_json::from_str::<Value>(&text) else {
            return;
        };
        let listed = fields.get("projects").and_then(Value::as_object);
        for (path, slug) in listed.into_iter().flatten() {
            if path.len() as u64 > PROJECT_PATH_MAX_BYTES {
                continue;
            }
            if let Some(slug) = slug.as_str().filter(|slug| !slug.is_empty()) {
                projects
                    .entry(slug.to_string())
                    .or_insert_with(|| crate::ingest::project_name(path));
            }
            paths.push(path.clone());
        }
    }
}

/// A session file's project: what `projects` knows of its project directory's name (its own
/// marker first), else the adapter's own name, as for the hash directories of a project never
/// opened by a gemini-cli with the registry. Its parent, for a subagent's file.
pub(super) fn place_of(path: &Path, projects: &Projects) -> Place {
    let parent = side_parent(path).unwrap_or_default();
    let project = project_dir(path)
        .and_then(|dir| projects.0.get(dir.file_name()?.to_str()?).cloned())
        .unwrap_or_else(|| "gemini".to_string());
    Place { project, parent }
}

/// A legacy `X.json` with a plain `X.jsonl` beside it: resuming migrated the whole record
/// into the `.jsonl`, which carries the conversation from then on.
pub(super) fn superseded(path: &Path) -> bool {
    if path.extension().and_then(|x| x.to_str()) != Some("json") {
        return false;
    }
    let mut sibling = path.as_os_str().to_os_string();
    sibling.push("l");
    fs::symlink_metadata(&sibling).is_ok_and(|meta| meta.is_file() && !metadata_is_link(&meta))
}

/// A plain directory entry of the wanted kind, or None, with the skip warned when it is a link
/// or unreadable: links are never followed.
fn plain_entry(entry: &fs::DirEntry, wanted: fn(&fs::Metadata) -> bool) -> Option<PathBuf> {
    let path = entry.path();
    match plain_entry_metadata(entry) {
        Ok(Some(meta)) if wanted(&meta) => Some(path),
        Ok(None) => {
            crate::ingest::warn_source_skip("gemini", &path, "symlink sources are not followed");
            None
        }
        Ok(Some(_)) => None,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", &path, &error);
            None
        }
    }
}

/// Walk every `~/.gemini/tmp/<project>/chats/session-*.json(l)` and each subagent chat nested
/// under it as `chats/<parent session id>/<agent id>.jsonl`, returning superseded legacy files
/// separately so the cache forgets them. `logs/` and `tool-outputs/` beside `chats` are
/// telemetry; symlinked projects, directories and files are never followed.
pub(super) fn session_files(root: &Path) -> (Vec<PathBuf>, HashSet<PathBuf>) {
    let mut files: Vec<PathBuf> = Vec::new();
    let mut retired: HashSet<PathBuf> = HashSet::new();
    let Some(dirs) = read_root(root) else {
        return (files, retired);
    };
    for entry in dirs {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", root, &error);
                continue;
            }
        };
        let Some(project) = plain_entry(&entry, fs::Metadata::is_dir) else {
            continue;
        };
        let Some(chats) = chats_dir(&project) else {
            continue;
        };
        chat_files(&chats, &mut files, &mut retired);
    }
    (files, retired)
}

/// The store root's listing, when it is a plain directory that can be read.
fn read_root(root: &Path) -> Option<fs::ReadDir> {
    match crate::ingest::registry::root_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("gemini", root, "source root is not a plain directory");
            return None;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return None,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            return None;
        }
    }
    match fs::read_dir(root) {
        Ok(dirs) => Some(dirs),
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", root, &error);
            None
        }
    }
}

/// A project's `chats` directory, when it is a plain one.
fn chats_dir(project: &Path) -> Option<PathBuf> {
    let chats = project.join("chats");
    match fs::symlink_metadata(&chats) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => Some(chats),
        Ok(meta) if metadata_is_link(&meta) => {
            crate::ingest::warn_source_skip("gemini", &chats, "symlink sources are not followed");
            None
        }
        Ok(_) => None,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", &chats, &error);
            None
        }
    }
}

/// The session files in one `chats` directory: main sessions, superseded ones retired, and the
/// subagent files of every directory in it.
fn chat_files(chats: &Path, files: &mut Vec<PathBuf>, retired: &mut HashSet<PathBuf>) {
    let rd = match fs::read_dir(chats) {
        Ok(rd) => rd,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", chats, &error);
            return;
        }
    };
    for file in rd {
        let file = match file {
            Ok(file) => file,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", chats, &error);
                continue;
            }
        };
        let path = file.path();
        let is_dir = match plain_entry_metadata(&file) {
            Ok(Some(meta)) => meta.is_dir(),
            _ => false,
        };
        if is_dir {
            subagent_files(&path, files);
            continue;
        }
        let Some(path) = plain_entry(&file, fs::Metadata::is_file) else {
            continue;
        };
        let name = path
            .file_name()
            .map(|name| name.to_string_lossy())
            .unwrap_or_default();
        if !is_session_name(&name) {
            continue;
        }
        if superseded(&path) {
            retired.insert(path);
        } else {
            files.push(path);
        }
    }
}

/// The subagent files under one `chats/<parent session id>` directory: every plain `.jsonl`.
fn subagent_files(container: &Path, files: &mut Vec<PathBuf>) {
    let rd = match fs::read_dir(container) {
        Ok(rd) => rd,
        Err(error) => {
            crate::ingest::warn_source_skip("gemini", container, &error);
            return;
        }
    };
    for file in rd {
        let file = match file {
            Ok(file) => file,
            Err(error) => {
                crate::ingest::warn_source_skip("gemini", container, &error);
                continue;
            }
        };
        if let Some(path) = plain_entry(&file, fs::Metadata::is_file) {
            if path.extension().and_then(|x| x.to_str()) == Some("jsonl") {
                files.push(path);
            }
        }
    }
}
