//! Where pi sessions live, and what a session file's path says about it: its
//! cwd slug, its sidechat parent and the id its filename carries.

use std::collections::HashMap;
use std::fs;
use std::path::{Component, Path, PathBuf};

use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};

/// pi's own home and the oh-my-pi fork's, walked as one store.
const PI_HOMES: [&str; 2] = [".pi", ".omp"];

fn sessions_root(home_dir: &str) -> PathBuf {
    crate::ingest::home()
        .join(home_dir)
        .join("agent")
        .join("sessions")
}
fn archived_sessions_root(home_dir: &str) -> PathBuf {
    crate::ingest::home()
        .join(home_dir)
        .join("agent")
        .join("archive")
        .join("sessions")
}

/// Every session root in store order: each home's live sessions, then its archive.
pub(super) fn session_roots() -> impl Iterator<Item = PathBuf> {
    PI_HOMES
        .iter()
        .flat_map(|home| [sessions_root(home), archived_sessions_root(home)])
}

pub(crate) fn source_order() -> HashMap<PathBuf, usize> {
    session_roots()
        .flat_map(|root| session_files("pi", &root))
        .enumerate()
        .map(|(rank, path)| (path, rank))
        .collect()
}

fn plain_dir(agent: &str, path: &Path) -> bool {
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => true,
        Ok(meta) if metadata_is_link(&meta) => {
            crate::ingest::warn_source_skip(agent, path, "symlink sources are not followed");
            false
        }
        Ok(_) => false,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
        Err(error) => {
            crate::ingest::warn_source_skip(agent, path, &error);
            false
        }
    }
}

pub(super) fn is_session_file(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    name.ends_with(".jsonl") || name.ends_with(".jsonl.gz")
}

/// Session files under one cwd-slug directory. A slug also holds subdirectories
/// of sidecar sessions (the fork's advisor runs), so the walk goes one level
/// deeper - and session filenames are not always <timestamp>_<uuid>.
fn slug_files(agent: &str, slug: &Path, files: &mut Vec<PathBuf>) {
    let entries = match fs::read_dir(slug) {
        Ok(entries) => entries,
        Err(error) => {
            crate::ingest::warn_source_skip(agent, slug, &error);
            return;
        }
    };
    let mut nested: Vec<PathBuf> = Vec::new();
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip(agent, slug, &error);
                continue;
            }
        };
        let path = entry.path();
        match plain_entry_metadata(&entry) {
            Ok(Some(meta)) if meta.is_file() => {
                if is_session_file(&path) {
                    files.push(path);
                }
            }
            Ok(Some(meta)) if meta.is_dir() => nested.push(path),
            Ok(Some(_)) => {}
            Ok(None) => {
                crate::ingest::warn_source_skip(agent, &path, "symlink sources are not followed")
            }
            Err(error) => crate::ingest::warn_source_skip(agent, &path, &error),
        }
    }
    for child in nested {
        if plain_dir(agent, &child) {
            slug_files(agent, &child, files);
        }
    }
}

pub(crate) fn session_files(agent: &str, root: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = Vec::new();
    if !plain_dir(agent, root) {
        return files;
    }
    let slugs = match fs::read_dir(root) {
        Ok(slugs) => slugs,
        Err(error) => {
            crate::ingest::warn_source_skip(agent, root, &error);
            return files;
        }
    };
    for slug in slugs {
        let slug = match slug {
            Ok(slug) => slug,
            Err(error) => {
                crate::ingest::warn_source_skip(agent, root, &error);
                continue;
            }
        };
        let path = slug.path();
        match plain_entry_metadata(&slug) {
            Ok(Some(meta)) if meta.is_dir() => {
                if plain_dir(agent, &path) {
                    slug_files(agent, &path, &mut files);
                }
            }
            Ok(None) => {
                crate::ingest::warn_source_skip(agent, &path, "symlink sources are not followed")
            }
            Ok(Some(_)) => {}
            Err(error) => crate::ingest::warn_source_skip(agent, &path, &error),
        }
    }
    files
}

/// The cwd slug directory name, used only when the session header has no cwd.
pub(super) fn slug_project(path: &Path) -> String {
    path.parent()
        .and_then(|parent| parent.file_name())
        .map(|name| name.to_string_lossy().trim_matches('-').replace('-', "/"))
        .filter(|project| !project.is_empty())
        .unwrap_or_else(|| "pi".to_string())
}

fn side_parent_in(root: &Path, path: &Path) -> Option<String> {
    let relative = path.strip_prefix(root).ok()?;
    let mut parts = relative.components();
    let Component::Normal(_slug) = parts.next()? else {
        return None;
    };
    let Component::Normal(container) = parts.next()? else {
        return None;
    };
    let container = container.to_str()?;
    parts.next()?;
    let parent = container
        .rsplit_once('_')
        .map_or(container, |(_, session)| session);
    (!parent.is_empty()).then(|| parent.to_string())
}

/// The sidechat root a nested sidecar session hangs off, from its container directory.
pub(super) fn side_parent(path: &Path) -> Option<String> {
    session_roots().find_map(|root| side_parent_in(&root, path))
}

/// `YYYY-MM-DDTHH-MM-SS-mmmZ`, the prefix pi gives every session file it creates.
fn is_session_stamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 24
        && bytes.iter().enumerate().all(|(index, byte)| match index {
            4 | 7 | 13 | 16 | 19 => *byte == b'-',
            10 => *byte == b'T',
            23 => *byte == b'Z',
            _ => byte.is_ascii_digit(),
        })
}

/// The id a slug-level `<timestamp>_<id>.jsonl[.gz]` filename carries; the sidechat container
/// is keyed off this name, so it outlives a header edit. Nested sidecars are named freely.
fn session_file_id<'p>(root: &Path, path: &'p Path) -> Option<&'p str> {
    let relative = path.strip_prefix(root).ok()?;
    let mut parts = relative.components();
    let Component::Normal(_slug) = parts.next()? else {
        return None;
    };
    let Component::Normal(file) = parts.next()? else {
        return None;
    };
    if parts.next().is_some() {
        return None;
    }
    let file = file.to_str()?;
    let stem = file
        .strip_suffix(".jsonl.gz")
        .or_else(|| file.strip_suffix(".jsonl"))?;
    let (stamp, id) = stem.rsplit_once('_')?;
    (is_session_stamp(stamp) && !id.is_empty()).then_some(id)
}

pub(super) fn session_alias(path: &Path, session: &str) -> Option<String> {
    session_roots()
        .find_map(|root| session_file_id(&root, path))
        .filter(|id| *id != session)
        .map(str::to_owned)
}
