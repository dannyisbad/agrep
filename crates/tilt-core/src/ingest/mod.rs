//! Per-agent transcript adapters. Each yields normalized `Message`s (Danny's words only).
//! Implemented: claude. Next: codex, opencode, antigravity, gemini.

pub mod claude;
pub mod codex;
pub mod opencode;
pub mod antigravity;

use std::path::PathBuf;

/// Home directory (Windows `USERPROFILE`, else `HOME`).
pub fn home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Bucket a working-dir path into a project name. Uses the basename, but disambiguates
/// generic leaf names (src/web/Danny/Desktop/...) by prepending the parent segment, so
/// `finance2/web` and `parkonce/.../src` don't all collapse into "web"/"src".
pub fn project_name(cwd: &str) -> String {
    let parts: Vec<&str> = cwd
        .split(|c| c == '/' || c == '\\')
        .filter(|s| !s.is_empty())
        .collect();
    match parts.last() {
        None => "unknown".to_string(),
        Some(&leaf) => {
            let generic = matches!(
                leaf.to_ascii_lowercase().as_str(),
                "src" | "web" | "mobile" | "app" | "lib" | "client" | "server"
                    | "danny" | "desktop" | "documents" | "downloads"
            );
            if generic && parts.len() >= 2 {
                format!("{}/{}", parts[parts.len() - 2], leaf)
            } else {
                leaf.to_string()
            }
        }
    }
}

/// Parse an RFC3339 timestamp to epoch millis; 0 on failure/None.
pub fn ts_millis(s: Option<&str>) -> i64 {
    s.and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.timestamp_millis())
        .unwrap_or(0)
}

/// Append `t` to `buf` (space-joined), capping the result to `cap` characters (UTF-8 safe).
/// Used to accumulate a trimmed agent reply across multiple blocks without unbounded growth.
pub fn append_capped(buf: &mut String, t: &str, cap: usize) {
    let t = t.trim();
    if t.is_empty() || buf.chars().count() >= cap {
        return;
    }
    if !buf.is_empty() {
        buf.push(' ');
    }
    buf.push_str(t);
    if buf.chars().count() > cap {
        let kept: String = buf.chars().take(cap).collect();
        *buf = kept;
        buf.push('…');
    }
}

/// Is this content a command/system wrapper rather than something Danny typed?
pub fn is_wrapper(text: &str) -> bool {
    let t = text.trim_start();
    t.starts_with("<command-name>")
        || t.starts_with("<command-message>")
        || t.starts_with("<command-args>")
        || t.starts_with("<local-command")
        || t.starts_with("<bash-input>")
        || t.starts_with("<bash-stdout>")
        || t.starts_with("<user-prompt-submit-hook>")
        || t.starts_with("Caveat:")
        || (t.contains("<command-name>") && t.contains("</command-name>"))
        || t.starts_with("<system-reminder>")
        // multi-agent orchestration chatter that isn't Danny typing (leaked into search):
        || t.starts_with("<teammate-message")
        || t.starts_with("<task-notification")
        || t.starts_with("[SYSTEM NOTIFICATION")
        || t.starts_with("<system-notification")
        || (t.starts_with('{')
            && (t.contains("\"idle_notification\"")
                || t.contains("\"task_completed\"")
                || t.contains("\"shutdown_request\"")
                || t.contains("\"type\":\"idle\"")))
}
