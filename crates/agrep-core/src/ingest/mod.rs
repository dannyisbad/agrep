//! Per-agent transcript adapters. Each yields normalized `Message`s (the user's words only).
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

/// Lowercased last segment of the home dir (the username). Paths under home start with
/// container segments (`Users/<name>/Desktop/...`); the username must never read as a
/// project name, whoever runs tilt.
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
/// `finance2/web` and `parkonce/.../src` don't all collapse into "web"/"src".
pub fn project_name(cwd: &str) -> String {
    // Cwds recovered from logged tool args can carry shell quoting (`...\opencode-dev"`).
    let parts: Vec<&str> = cwd
        .split(|c| c == '/' || c == '\\')
        .map(|s| s.trim_matches(|c| c == '"' || c == '\'' || c == ' '))
        .filter(|s| !s.is_empty())
        .collect();
    match parts.last() {
        None => "unknown".to_string(),
        Some(&leaf) => {
            let lower = leaf.to_ascii_lowercase();
            let generic = lower == home_leaf()
                || matches!(
                    lower.as_str(),
                    "src" | "web" | "mobile" | "app" | "lib" | "client" | "server"
                        | "desktop" | "documents" | "downloads"
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

/// Hard cap on event input/output summaries. The uncapped payload stays in the source
/// store and is re-fetched on demand (`/event_raw`), so this only bounds the index size
/// (at 2000 the corpus weighed 729MB, codex-dominated; 800 keeps expansion previews
/// useful at roughly half that).
pub const EVENT_CAP: usize = 800;

/// Truncate to `cap` characters (UTF-8 safe), appending an ellipsis when cut.
pub fn cap_str(s: &str, cap: usize) -> String {
    let mut it = s.char_indices();
    match it.nth(cap) {
        None => s.to_string(),
        Some((i, _)) => {
            let mut t = s[..i].to_string();
            t.push('…');
            t
        }
    }
}

/// Compact, human-meaningful summary of a tool-call input object: prefer the fields a
/// person would scan for (command line, file path, pattern, prompt), else compact JSON.
pub fn summarize_tool_input(input: &serde_json::Value) -> String {
    const KEYS: &[&str] = &[
        "command",
        "file_path",
        "notebook_path",
        "path",
        "pattern",
        "query",
        "url",
        "prompt",
        "description",
        "cmd",
    ];
    if let Some(obj) = input.as_object() {
        let mut parts: Vec<String> = Vec::new();
        for k in KEYS {
            match obj.get(*k) {
                Some(serde_json::Value::String(s)) if !s.trim().is_empty() => {
                    parts.push(s.trim().to_string());
                }
                // command arrays like ["bash","-lc","cargo build"]
                Some(serde_json::Value::Array(a)) => {
                    let joined: Vec<&str> = a.iter().filter_map(|x| x.as_str()).collect();
                    if !joined.is_empty() {
                        parts.push(joined.join(" "));
                    }
                }
                _ => {}
            }
        }
        if !parts.is_empty() {
            return cap_str(&parts.join(" · "), EVENT_CAP);
        }
    }
    if input.is_null() {
        return String::new();
    }
    cap_str(&input.to_string(), EVENT_CAP)
}

/// Is this content a command/system wrapper rather than something the user typed?
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
        // multi-agent orchestration chatter that isn't the user typing (leaked into search):
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
