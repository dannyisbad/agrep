//! A full read: one session file, plain or gzip-compressed, parsed in one pass.

use std::io::Read;
use std::path::Path;

use super::parse::{parse_text, Origin};
use super::Parsed;
use crate::intake::Tally;

const GZIP_COMPRESSED_MAX_BYTES: u64 = 64 * 1024 * 1024;
const GZIP_DECOMPRESSED_MAX_BYTES: u64 = 256 * 1024 * 1024;

fn read_session(path: &Path) -> std::io::Result<String> {
    if path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".jsonl.gz"))
    {
        let bytes =
            crate::ingest::registry::read_growing_regular_file(path, GZIP_COMPRESSED_MAX_BYTES)?
                .ok_or_else(|| {
                    std::io::Error::new(std::io::ErrorKind::NotFound, "source does not exist")
                })?;
        let mut decoded = Vec::new();
        flate2::read::GzDecoder::new(bytes.as_slice())
            .take(GZIP_DECOMPRESSED_MAX_BYTES.saturating_add(1))
            .read_to_end(&mut decoded)?;
        if decoded.len() as u64 > GZIP_DECOMPRESSED_MAX_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("decompressed source exceeds {GZIP_DECOMPRESSED_MAX_BYTES} bytes"),
            ));
        }
        return Ok(String::from_utf8(decoded)
            .unwrap_or_else(|error| String::from_utf8_lossy(error.as_bytes()).into_owned()));
    }
    crate::ingest::read_lossy(path)
}

pub(super) fn parse_file(agent: &'static str, path: &Path, parent: Option<String>) -> Parsed {
    // seen = JSONL records, matching every other line-oriented adapter
    let tally = crate::intake::tally();
    match read_session(path) {
        Ok(data) => parse_text(
            Origin {
                agent,
                path,
                parent,
            },
            tally,
            &data,
        ),
        Err(error) => unreadable(agent, path, &tally, &error),
    }
}

/// An unreadable file counts as one seen, errored record.
fn unreadable(agent: &str, path: &Path, tally: &Tally, error: &std::io::Error) -> Parsed {
    crate::intake::diagnostic(format_args!(
        "  ! {agent}: cannot read {}: {}",
        crate::ingest::terminal_safe(path.display()),
        crate::ingest::terminal_safe(error)
    ));
    tally.seen();
    tally.error(&format!("cannot read: {error}"));
    crate::ingest::note_io_failure(path, error);
    // A compressed session's decode and size-limit failures are its content, not a race.
    let outcome = if path.to_string_lossy().ends_with(".jsonl.gz") {
        crate::intake_batch::ReadOutcome::Skipped
    } else {
        crate::ingest::read_failure_outcome(error)
    };
    (Vec::new(), Vec::new(), outcome)
}
