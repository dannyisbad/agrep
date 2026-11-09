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

/// Notes a source's failed read by its cause alone.
pub(crate) fn note_io_failure(path: &Path, error: &std::io::Error) {
    note_read_failure(path, io_reason(error));
}

/// A failed read's cause, the I/O error under it when there is one.
pub(crate) fn error_reason(error: &anyhow::Error) -> String {
    error
        .chain()
        .find_map(|cause| cause.downcast_ref::<std::io::Error>())
        .map_or_else(|| format!("{error:#}"), io_reason)
}

/// An I/O error as `permission denied` rather than `Permission denied (os error 13)`: it is
/// shown beside the path, which names the file.
fn io_reason(error: &std::io::Error) -> String {
    let text = error.to_string();
    let text = match (error.raw_os_error(), text.rfind(" (os error ")) {
        (Some(_), Some(at)) => &text[..at],
        _ => text.as_str(),
    };
    let text = text.trim_end_matches('.');
    let mut chars = text.chars();
    match (chars.next(), chars.clone().next()) {
        (Some(first), Some(second)) if first.is_uppercase() && second.is_lowercase() => {
            first.to_lowercase().chain(chars).collect()
        }
        _ => text.to_owned(),
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_read_error_is_named_by_its_cause_alone() {
        let denied = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
        assert_eq!(io_reason(&denied), "permission denied");
        let os = std::io::Error::from_raw_os_error(2);
        assert!(!io_reason(&os).contains("os error"), "{}", io_reason(&os));
        assert!(io_reason(&os).starts_with(|c: char| c.is_lowercase()));
        let custom = std::io::Error::new(std::io::ErrorKind::NotFound, "source does not exist");
        assert_eq!(io_reason(&custom), "source does not exist");
        let wrapped = anyhow::Error::from(denied).context("opening the source");
        assert_eq!(error_reason(&wrapped), "permission denied");
        assert_eq!(
            error_reason(&anyhow::anyhow!("source is a symlink")),
            "source is a symlink"
        );
    }
}
