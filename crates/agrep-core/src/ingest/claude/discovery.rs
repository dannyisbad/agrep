//! Discovery: the transcript files under ~/.claude/projects, and which paths count as
//! discovered transcripts for freshness.

use std::fs;
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use crate::ingest::registry::{metadata_is_link, plain_entry_metadata, StoreLink};
use crate::ingest::warn_source_skip;

pub(super) fn projects_root() -> PathBuf {
    crate::ingest::home().join(".claude").join("projects")
}

/// Every transcript under the projects root, sorted; links are warned about unless they are
/// agrep's own aliases.
pub(super) fn transcript_files(root: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut links = Vec::new();
    if let Some(dirs) = read_root(root) {
        for entry in dirs {
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) => {
                    warn_source_skip("claude", root, &error);
                    continue;
                }
            };
            let dir = entry.path();
            match plain_entry_metadata(&entry) {
                Ok(Some(meta)) if meta.is_dir() => gather_jsonl(&dir, &mut files, &mut links, 0),
                Ok(None) => links.push(dir),
                Ok(Some(_)) => {}
                Err(error) => warn_source_skip("claude", &dir, &error),
            }
        }
        files.sort_unstable();
    }
    warn_foreign_links(root, &files, links);
    files
}

/// The root's listing when it is a plain directory; a missing root is silent.
fn read_root(root: &Path) -> Option<fs::ReadDir> {
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => match fs::read_dir(root) {
            Ok(dirs) => Some(dirs),
            Err(error) => {
                warn_source_skip("claude", root, &error);
                None
            }
        },
        Ok(_) => {
            warn_source_skip("claude", root, "source root is not a plain directory");
            None
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => {
            warn_source_skip("claude", root, &error);
            None
        }
    }
}

enum EntryKind {
    Directory,
    Link,
    Jsonl,
    Other,
    Error(String),
}

/// Recursively gather every transcript jsonl under a project dir (sessions,
/// subagents/ side transcripts, nested project-slug dirs). Deliberately skipped
/// worker transcripts remain in this set so freshness and intake accounting see them.
/// Links are collected for `warn_foreign_links`, which needs the finished file set.
fn gather_jsonl(dir: &Path, files: &mut Vec<PathBuf>, links: &mut Vec<PathBuf>, depth: u8) {
    if depth > 5 {
        return;
    }
    let Some(entries) = plain_dir_entries(dir) else {
        return;
    };
    // Directory enumeration is sequential, but classifying the many transcript entries is
    // independent work and should not delay the newest-file streamed first hit.
    let classified: Vec<_> = entries
        .into_par_iter()
        .map(|entry| {
            let path = entry.path();
            let kind = classify(&entry, &path);
            (path, kind)
        })
        .collect();
    for (path, kind) in classified {
        match kind {
            EntryKind::Directory => gather_jsonl(&path, files, links, depth + 1),
            EntryKind::Link => links.push(path),
            EntryKind::Jsonl => files.push(path),
            EntryKind::Other => {}
            EntryKind::Error(error) => warn_source_skip("claude", &path, error),
        }
    }
}

/// The readable entries of `dir` when it is a plain directory, warning about each failure.
fn plain_dir_entries(dir: &Path) -> Option<Vec<fs::DirEntry>> {
    match fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            warn_source_skip("claude", dir, "source is not a plain directory");
            return None;
        }
        Err(error) => {
            warn_source_skip("claude", dir, &error);
            return None;
        }
    }
    let rd = match fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(error) => {
            warn_source_skip("claude", dir, &error);
            return None;
        }
    };
    Some(
        rd.filter_map(|entry| match entry {
            Ok(entry) => Some(entry),
            Err(error) => {
                warn_source_skip("claude", dir, &error);
                None
            }
        })
        .collect(),
    )
}

fn classify(entry: &fs::DirEntry, path: &Path) -> EntryKind {
    match plain_entry_metadata(entry) {
        Ok(Some(metadata)) if metadata.is_dir() => EntryKind::Directory,
        Ok(Some(metadata))
            if metadata.is_file() && path.extension().and_then(|e| e.to_str()) == Some("jsonl") =>
        {
            EntryKind::Jsonl
        }
        Ok(None) => EntryKind::Link,
        Ok(Some(_)) => EntryKind::Other,
        Err(error) => EntryKind::Error(error.to_string()),
    }
}

/// agrep's own resume links (aliases of gathered transcripts, or dangling inside the
/// store) are skipped silently, like the registry walk; any other link is warned about.
fn warn_foreign_links(root: &Path, files: &[PathBuf], links: Vec<PathBuf>) {
    let accepts = |path: &Path| path.extension().and_then(|e| e.to_str()) == Some("jsonl");
    for link in links {
        match crate::ingest::registry::classify_store_link(&link, root, &accepts) {
            StoreLink::Alias(walked) if files.binary_search(&walked).is_ok() => {}
            StoreLink::Dangling => {}
            _ => warn_source_skip("claude", &link, "symlink sources are not followed"),
        }
    }
}

pub(super) fn is_discovered_transcript(path: &Path) -> bool {
    if path.extension().and_then(|x| x.to_str()) != Some("jsonl") {
        return false;
    }
    let root = projects_root();
    let relative =
        crate::ingest::registry::relative_path(path, &root).unwrap_or_else(|| path.to_path_buf());
    let depth = relative.components().count();
    (2..=7).contains(&depth)
}
