//! Read-failure bookkeeping: why a source's read failed and what that failure says about the
//! source.

use std::path::{Path, PathBuf};

/// Why a source's read failed (a store's open, an empty file), keyed by its path, for the
/// read-failure record the cache writes after the parse returns only an outcome.
static READ_FAILURES: std::sync::Mutex<Vec<(PathBuf, String)>> = std::sync::Mutex::new(Vec::new());

pub(crate) fn note_read_failure(path: &Path, reason: String) {
    if let Ok(mut failures) = READ_FAILURES.lock() {
        failures.retain(|(noted, _)| noted != path);
        failures.push((path.to_path_buf(), reason));
    }
}

pub(crate) fn take_read_failure(path: &Path) -> Option<String> {
    let mut failures = READ_FAILURES.lock().ok()?;
    let at = failures.iter().position(|(noted, _)| noted == path)?;
    Some(failures.swap_remove(at).1)
}

/// What a per-file read error says about the source: a denial, an absence or a special file is
/// skipped as before; one that changed under the read moved; any other error may clear.
pub(crate) fn read_failure_outcome(error: &std::io::Error) -> crate::intake_batch::ReadOutcome {
    use crate::intake_batch::ReadOutcome;
    match error.kind() {
        std::io::ErrorKind::PermissionDenied
        | std::io::ErrorKind::NotFound
        | std::io::ErrorKind::InvalidInput => ReadOutcome::Skipped,
        std::io::ErrorKind::InvalidData => ReadOutcome::Moved,
        _ => ReadOutcome::Unavailable,
    }
}

/// A read whose source generation moved before or while it ran.
#[derive(Debug)]
pub(crate) struct SourceMoved(pub String);

impl std::fmt::Display for SourceMoved {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SourceMoved {}
