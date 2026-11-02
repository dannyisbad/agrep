//! Explicit environment for benchmark child processes on every platform.
use std::{ffi::OsString, path::Path, process::Command};
pub fn command(
    bin: &Path,
    home: &Path,
    data: &Path,
    profile: &str,
    path: OsString,
    system_root: Option<OsString>,
) -> Command {
    let mut c = Command::new(bin);
    c.env_clear()
        .env("PATH", path)
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_PROFILE", profile)
        .env("LOCALAPPDATA", home.join("AppData/Local"))
        .env("APPDATA", home.join("AppData/Roaming"))
        .env("XDG_DATA_HOME", home.join(".local/share"))
        .env("XDG_CONFIG_HOME", home.join(".config"))
        .env("XDG_CACHE_HOME", home.join(".cache"));
    for key in ["TMPDIR", "TEMP", "TMP"] {
        c.env(key, std::env::temp_dir());
    }
    if let Some(root) = system_root {
        c.env("SystemRoot", root);
    }
    c
}
