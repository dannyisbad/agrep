//! Rollout discovery under `~/.codex/sessions/` and the registry adapter.

use std::fs;
use std::path::Path;

use super::parse_file;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::model::{Event, Message};

fn is_rollout_file(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("rollout-") && name.ends_with(".jsonl"))
}

/// Recursively collect rollout files under `~/.codex/sessions/` (YYYY/MM/DD nesting).
fn gather(dir: &Path, files: &mut Vec<std::path::PathBuf>, absent_ok: bool) {
    match fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("codex", dir, "source is not a plain directory");
            return;
        }
        Err(error) if absent_ok && error.kind() == std::io::ErrorKind::NotFound => return,
        Err(error) => {
            crate::ingest::warn_source_skip("codex", dir, &error);
            return;
        }
    }
    let rd = match fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(error) => {
            crate::ingest::warn_source_skip("codex", dir, &error);
            return;
        }
    };
    for entry in rd {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("codex", dir, &error);
                continue;
            }
        };
        let p = entry.path();
        let metadata = match plain_entry_metadata(&entry) {
            Ok(Some(metadata)) => metadata,
            Ok(None) => {
                crate::ingest::warn_source_skip("codex", &p, "symlink sources are not followed");
                continue;
            }
            Err(error) => {
                crate::ingest::warn_source_skip("codex", &p, &error);
                continue;
            }
        };
        if metadata.is_dir() {
            gather(&p, files, false);
        } else if metadata.is_file() && is_rollout_file(&p) {
            files.push(p);
        }
    }
}

/// Walk all session rollouts and collect the user's Codex messages + tool events.
pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".codex").join("sessions");
    let files = cache.preflight_source_paths(&root).unwrap_or_else(|| {
        let mut files = Vec::new();
        gather(&root, &mut files, true);
        files.sort_unstable();
        files
    });

    let pass = crate::intake_batch::collect_cached_for(cache, "codex", &files, parse_file);
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). Byte-identical wrapper over `collect`.
pub struct Codex;
impl crate::ingest::registry::Adapter for Codex {
    fn name(&self) -> &'static str {
        "codex"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".codex").join("sessions")]
    }
    fn archive_roots(&self) -> Vec<std::path::PathBuf> {
        vec![
            crate::ingest::home().join(".codex/sessions"),
            crate::ingest::home().join(".codex/archived_sessions"),
        ]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        is_rollout_file(path)
    }
}
