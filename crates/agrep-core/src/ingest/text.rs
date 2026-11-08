//! Text helpers shared by the adapters: lossy source reads, capped event text, tool-input
//! summaries, wrapper detection, and terminal-safe diagnostics.

/// Cap on event summaries. Original lengths remain in the event row to disclose truncation.
pub const EVENT_CAP: usize = 800;

/// Read a store file tolerating invalid UTF-8: corrupt bytes become U+FFFD, so one bad
/// byte costs at most its own record instead of the whole transcript (a strict
/// read_to_string fails the entire file). Io errors still bubble; at a tallied call
/// site the error arm counts the file as one seen record + one error, keeping the
/// intake identity whole for files the parser never got to iterate.
///
/// Transcripts are live: the session being written right now is the one a user most wants
/// searchable, so this serves the extent the file had at open and leaves the tail to the
/// next pass. A trailing partial record simply fails its own line and is re-read then.
pub fn read_lossy(path: &std::path::Path) -> std::io::Result<String> {
    let bytes = crate::ingest::registry::read_growing_regular_file(path, u64::MAX)
        .and_then(|body| {
            body.ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::NotFound, "source does not exist")
            })
        })
        .map_err(|error| {
            std::io::Error::new(error.kind(), format!("{}: {error}", path.display()))
        })?;
    Ok(match String::from_utf8(bytes) {
        // Valid UTF-8 is the overwhelming case: take ownership of the read buffer instead of copying it.
        Ok(text) => text,
        Err(error) => String::from_utf8_lossy(error.as_bytes()).into_owned(),
    })
}

/// The canonical human-output sanitizer is shared with search and evidence output.
pub fn terminal_safe(value: impl std::fmt::Display) -> String {
    crate::surface::safe(&value.to_string(), false)
}

/// Report a source that could not join this generation without letting store-derived paths or
/// errors write terminal controls.
pub fn warn_source_skip(agent: &str, path: &std::path::Path, reason: impl std::fmt::Display) {
    crate::intake::diagnostic(format_args!(
        "  ! {agent}: skipping {}: {}",
        terminal_safe(path.display()),
        terminal_safe(reason)
    ));
}

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

pub fn cap_str_with_chars(s: &str, cap: usize) -> (String, usize) {
    (cap_str(s, cap), s.chars().count())
}

/// Cap an event output while retaining both source units used by display surfaces.
///
/// The byte count must be sampled from the uncapped source. In particular, UTF-8
/// byte length cannot be recovered from the capped excerpt (which also gains an
/// ellipsis when truncated).
pub fn cap_event_output(s: &str) -> (String, usize, usize) {
    (cap_str(s, EVENT_CAP), s.chars().count(), s.len())
}

/// Compact, human-meaningful summary of a tool-call input object: prefer the fields a
/// person would scan for (command line, file path, pattern, prompt), else compact JSON.
pub fn summarize_tool_input(input: &serde_json::Value) -> String {
    summarize_tool_input_with_chars(input).0
}

pub fn summarize_tool_input_with_chars(input: &serde_json::Value) -> (String, usize) {
    if let Some(obj) = input.as_object() {
        let parts = scanned_fields(obj);
        if !parts.is_empty() {
            let text = parts.join(" · ");
            return cap_str_with_chars(&text, EVENT_CAP);
        }
    }
    if input.is_null() {
        return (String::new(), 0);
    }
    cap_str_with_chars(&input.to_string(), EVENT_CAP)
}

/// The non-empty fields of a tool input a person would scan for, in that order of preference.
fn scanned_fields(obj: &serde_json::Map<String, serde_json::Value>) -> Vec<String> {
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
    parts
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
        // multi-agent orchestration chatter that isn't the user typing:
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
