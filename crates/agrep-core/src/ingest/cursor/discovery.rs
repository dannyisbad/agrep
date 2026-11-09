//! Which Cursor profile (`User` directory) holds the global state database.

use std::path::{Path, PathBuf};

use crate::ingest::registry::root_file;

fn user_dir_candidates_at(home: &Path, appdata: Option<&Path>) -> [PathBuf; 3] {
    let windows = appdata
        .map(Path::to_path_buf)
        .unwrap_or_else(|| home.join("AppData").join("Roaming"))
        .join("Cursor")
        .join("User");
    let macos = home
        .join("Library")
        .join("Application Support")
        .join("Cursor")
        .join("User");
    let linux = home.join(".config").join("Cursor").join("User");
    #[cfg(target_os = "windows")]
    return [windows, macos, linux];
    #[cfg(target_os = "macos")]
    return [macos, windows, linux];
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    return [linux, windows, macos];
}

pub(super) fn user_dir_candidates(home: &Path) -> [PathBuf; 3] {
    let appdata = if cfg!(target_os = "windows") {
        std::env::var_os("APPDATA")
            .filter(|value| !value.as_os_str().is_empty())
            .map(PathBuf::from)
    } else {
        None
    };
    user_dir_candidates_at(home, appdata.as_deref())
}

#[derive(Debug)]
pub(super) struct UserDirError {
    pub(super) path: PathBuf,
    source: std::io::Error,
}

impl std::fmt::Display for UserDirError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "cannot inspect {}: {}",
            self.path.display(),
            self.source
        )
    }
}

impl std::error::Error for UserDirError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// The first profile whose global database is a regular file, or a store-root link to one.
fn user_dir_checked_in(candidates: [PathBuf; 3]) -> Result<Option<PathBuf>, UserDirError> {
    for user in candidates {
        let path = user.join("globalStorage").join("state.vscdb");
        match root_file(&path) {
            Ok((_, meta)) if meta.is_file() => return Ok(Some(user)),
            Ok(_) => {}
            Err(error) if crate::ingest::registry::stat_proves_absent(&path, &error) => {}
            Err(error) => {
                return Err(UserDirError {
                    path,
                    source: error,
                });
            }
        }
    }
    Ok(None)
}

/// The native Cursor profile wins; copied cross-OS profiles are migration fallback only.
pub(super) fn user_dir_checked() -> Result<Option<PathBuf>, UserDirError> {
    user_dir_checked_in(user_dir_candidates(&crate::ingest::home()))
}

pub(super) fn user_dir() -> Option<PathBuf> {
    user_dir_checked().ok().flatten()
}
