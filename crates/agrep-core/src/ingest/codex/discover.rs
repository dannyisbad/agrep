//! Rollout discovery under `~/.codex/sessions/` and the registry adapter.

use std::fs;
use std::path::Path;

use super::parse_file;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::model::{Event, Message};

/// Codex compresses a finished rollout to `<name>.jsonl.zst`.
const COMPRESSED: &str = ".jsonl.zst";

fn is_rollout_file(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            name.starts_with("rollout-") && (name.ends_with(".jsonl") || name.ends_with(COMPRESSED))
        })
}

pub(super) fn is_compressed(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(COMPRESSED))
}

/// The file stem a rollout's identities derive from, the same for its compressed copy so a
/// chat keeps its ids when Codex compresses it.
pub(super) fn rollout_stem(path: &Path) -> Option<&str> {
    let name = path.file_name()?.to_str()?;
    let plain = name
        .strip_suffix(".zst")
        .filter(|_| name.ends_with(COMPRESSED));
    Path::new(plain.unwrap_or(name)).file_stem()?.to_str()
}

/// Recursively collect rollout files under a Codex root (YYYY/MM/DD nesting). Only the root
/// (`absent_ok`) may be a link, which is followed.
fn gather(dir: &Path, files: &mut Vec<std::path::PathBuf>, absent_ok: bool) {
    let found = if absent_ok {
        crate::ingest::registry::root_metadata(dir)
    } else {
        fs::symlink_metadata(dir)
    };
    match found {
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

/// Codex moves a chat archived in its UI from `sessions/` to `archived_sessions/`; both hold
/// the user's chats.
fn roots() -> [std::path::PathBuf; 2] {
    let codex = crate::ingest::home().join(".codex");
    [codex.join("sessions"), codex.join("archived_sessions")]
}

/// Walk all session rollouts and collect the user's Codex messages + tool events.
pub fn collect(cache: &mut crate::intake_batch::IntakeBatch) -> (Vec<Message>, Vec<Event>) {
    let mut files = Vec::new();
    for root in roots() {
        files.extend(cache.preflight_source_paths(&root).unwrap_or_else(|| {
            let mut files = Vec::new();
            gather(&root, &mut files, true);
            files
        }));
    }
    files.sort_unstable();

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
        roots().into()
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        is_rollout_file(path)
    }
}
