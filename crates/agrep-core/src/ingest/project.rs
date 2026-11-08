//! The home directory every adapter discovers its store under, and the project name a
//! working directory buckets into.

use std::path::PathBuf;

/// Home directory: the discovery root every adapter resolves its store under. `AGREP_HOME`
/// is the shared hookless/ingest override; the golden harness uses it without touching the
/// real `USERPROFILE`/`HOME`. Otherwise `USERPROFILE` precedes `HOME`; reads stay fresh so
/// subprocess overrides are honored.
pub fn home() -> PathBuf {
    std::env::var_os("AGREP_HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Lowercased last segment of the home dir (the username). Paths under home start with
/// container segments (`Users/<name>/Desktop/...`); the username must never read as a
/// project name, whoever runs the ingest.
pub fn home_leaf() -> &'static str {
    use std::sync::OnceLock;
    static LEAF: OnceLock<String> = OnceLock::new();
    LEAF.get_or_init(|| {
        home()
            .file_name()
            .map(|s| s.to_string_lossy().to_ascii_lowercase())
            .unwrap_or_default()
    })
}

/// Bucket a working-dir path into a project name. Uses the basename, but disambiguates
/// generic leaf names (src/web/<you>/Desktop/...) by prepending the parent segment, so
/// `sampleapp/web` and `shopfront/.../src` don't all collapse into "web"/"src".
pub fn project_name(cwd: &str) -> String {
    // Cwds recovered from logged tool args can carry shell quoting (`...\sample-project"`).
    let parts: Vec<&str> = cwd
        .split(['/', '\\'])
        .map(|s| s.trim_matches(|c| c == '"' || c == '\'' || c == ' '))
        .filter(|s| !s.is_empty())
        .collect();
    let Some(&leaf) = parts.last() else {
        return "unknown".to_string();
    };
    // Any machine's home dir buckets as "~": home_leaf() knows only this machine's username,
    // so a foreign home cwd would leak a username as a project. Shape check: leaf whose
    // parent is Users/home at the path root.
    if parts.len() >= 2 && parts.len() - 2 <= 1 {
        let parent = parts[parts.len() - 2].to_ascii_lowercase();
        if parent == "users" || parent == "home" {
            return "~".to_string();
        }
    }
    if generic_leaf(&leaf.to_ascii_lowercase()) && parts.len() >= 2 {
        format!("{}/{}", parts[parts.len() - 2], leaf)
    } else {
        leaf.to_string()
    }
}

/// A lowercased leaf too common to name a project on its own.
fn generic_leaf(lower: &str) -> bool {
    lower == home_leaf()
        || matches!(
            lower,
            "src"
                | "web"
                | "mobile"
                | "app"
                | "lib"
                | "client"
                | "server"
                | "desktop"
                | "documents"
                | "downloads"
        )
}
