//! The change token that tells a source file's generations apart: a metadata stamp on Unix, a
//! USN or content hash of an open handle on Windows.

use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
pub enum ChangeToken {
    Metadata(u64),
    ContentSha256([u8; 32]),
}

impl ChangeToken {
    pub fn append_key(&self, output: &mut Vec<u8>) {
        match self {
            Self::Metadata(value) => {
                output.push(0);
                output.extend_from_slice(&value.to_le_bytes());
            }
            Self::ContentSha256(value) => {
                output.push(1);
                output.extend_from_slice(value);
            }
        }
    }
}

/// Linux stamps ctime from the coarse tick clock, so a change in the same tick as the
/// last one keeps its token. Tests that need a visible change wait for the next tick.
#[cfg(unix)]
pub fn metadata_change_token(
    _path: &Path,
    metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    use std::os::unix::fs::MetadataExt;
    let secs = metadata.ctime() as u64;
    Ok(ChangeToken::Metadata(
        secs.rotate_left(17) ^ metadata.ctime_nsec() as u64,
    ))
}

#[cfg(not(any(unix, windows)))]
pub fn metadata_change_token(
    _path: &Path,
    _metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    Ok(ChangeToken::Metadata(0))
}

#[cfg(windows)]
pub(super) fn open_file_change_token(
    _path: &Path,
    file: &fs::File,
    metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    super::windows_handle::windows_open_file_change_token(file, metadata).map(|(token, _)| token)
}

#[cfg(not(windows))]
pub(super) fn open_file_change_token(
    path: &Path,
    _file: &fs::File,
    metadata: &fs::Metadata,
) -> std::io::Result<ChangeToken> {
    metadata_change_token(path, metadata)
}
