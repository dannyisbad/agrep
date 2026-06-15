//! Claude Code adapter: ~/.claude/projects/<cwd-slug>/*.jsonl
//! Top-level session files only (skip `subagents/` and throwaway worker slugs).
//! Keeps real human turns: type=user, not meta/sidechain, userType external|absent,
//! string or text-block content, not a command/system wrapper.

use std::borrow::Cow;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

use memchr::memmem;
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::ingest::{
    cap_str, is_wrapper, project_name, summarize_tool_input, ts_millis, EVENT_CAP,
};
use crate::model::{Event, Message};

// Borrowed deserialization: scalar fields are Cow (borrow from the line when escape-free,
// allocate only when JSON-escaped) and `content` stays a RawValue slice, parsed into a
// DOM only by the lines that actually need one. Most lines fail a filter long before
// that, so the old per-line cost (full Value DOM + owned Strings, then discarded)
// disappears.
#[derive(Deserialize)]
struct Line<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(rename = "isMeta")]
    is_meta: Option<bool>,
    #[serde(rename = "isSidechain")]
    is_sidechain: Option<bool>,
    #[serde(rename = "userType", borrow)]
    user_type: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Msg<'a>>,
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    #[serde(rename = "sessionId", borrow)]
    session_id: Option<Cow<'a, str>>,
}

#[derive(Deserialize)]
struct Msg<'a> {
    #[serde(borrow)]
    role: Option<Cow<'a, str>>,
    #[serde(borrow)]
    content: Option<&'a RawValue>,
    #[serde(borrow)]
    model: Option<Cow<'a, str>>,
}

/// Extract the raw (still-escaped) bytes of the JSON string value that follows `finder`'s
/// `"key":"` needle. The escaped form is fine for project-histogram keys: segmentation
/// splits on both slash kinds and drops empty segments, so `C:\\Users` (escaped) and
/// `C:\Users` bucket to the same project root.
///
/// SOUNDNESS of raw-byte key needles, here and in the prefilters: inside any JSON string
/// value a quote is escaped to `\"`, so the byte sequence `"key":` can only occur as a
/// real object key - string contents can never produce a false match on it.
fn raw_str_value<'a>(finder: &memmem::Finder, bytes: &'a [u8]) -> Option<&'a str> {
    let start = finder.find(bytes)? + finder.needle().len();
    let mut j = start;
    while j < bytes.len() {
        match bytes[j] {
            b'\\' => j += 2,
            b'"' => return std::str::from_utf8(&bytes[start..j]).ok(),
            _ => j += 1,
        }
    }
    None
}

/// Pull human text out of a `message.content` that may be a string or a block array.
fn extract_text(content: &serde_json::Value) -> Option<String> {
    match content {
        serde_json::Value::String(s) => Some(s.clone()),
        serde_json::Value::Array(blocks) => {
            let mut out = String::new();
            for b in blocks {
                if b.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                        if !out.is_empty() {
                            out.push('\n');
                        }
                        out.push_str(t);
                    }
                }
            }
            if out.is_empty() {
                None
            } else {
                Some(out)
            }
        }
        _ => None,
    }
}

/// Pull file paths out of a session's tool_use blocks (Read/Write/Edit/... carry file_path,
/// notebook_path, or path). These reveal where the work actually happened.
fn collect_tool_paths(content: &serde_json::Value, out: &mut Vec<String>) {
    if let serde_json::Value::Array(blocks) = content {
        for b in blocks {
            if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                continue;
            }
            if let Some(inp) = b.get("input") {
                for key in ["file_path", "notebook_path", "path"] {
                    if let Some(v) = inp.get(key).and_then(|v| v.as_str()) {
                        if v.contains('/') || v.contains('\\') {
                            out.push(v.to_string());
                        }
                    }
                }
            }
        }
    }
}

/// Reduce a directory to its project ROOT bucket: strip the home prefix and any
/// container segments (`Users/<name>/Desktop/...`), keep the first real segment.
/// `~/Desktop/myproj/src` -> Some("myproj"); a bare home dir -> None (no signal).
fn project_root(dir: &str) -> Option<String> {
    let d = dir.replace('\\', "/");
    let home = crate::ingest::home()
        .to_string_lossy()
        .replace('\\', "/")
        .to_lowercase();
    let rel = if d.to_lowercase().starts_with(&home) {
        &d[home.len().min(d.len())..]
    } else {
        &d[..]
    };
    let user = crate::ingest::home_leaf();
    let mut segs = rel.split('/').filter(|s| !s.is_empty());
    segs.find(|s| {
        let sl = s.to_ascii_lowercase();
        !sl.ends_with(':')
            && sl != user
            && !matches!(
                sl.as_str(),
                "users"
                    | "desktop"
                    | "documents"
                    | "downloads"
                    | "onedrive"
                    | "home"
                    | "tmp"
                    | "temp"
                    | "appdata"
                    | "src"
            )
    })
    .map(|s| s.to_string())
}

/// The project a session actually worked in: a histogram over EVERY line's cwd (Claude
/// updates it as the session cd's around) plus the parent dir of every file its tools
/// touched, reduced to project roots. The most-worked-in root wins, so sessions launched
/// from a home dir that evolve into a real project land on where the work went, not
/// where the terminal happened to open.
fn primary_project(
    cwd_counts: &HashMap<String, usize>,
    paths: &[String],
    first_cwd: &str,
) -> String {
    let mut counts: HashMap<String, usize> = HashMap::new();
    for (c, n) in cwd_counts {
        if let Some(k) = project_root(c) {
            *counts.entry(k).or_insert(0) += n;
        }
    }
    for p in paths {
        let pn = p.replace('\\', "/");
        if let Some(i) = pn.rfind('/') {
            if let Some(k) = project_root(&pn[..i]) {
                *counts.entry(k).or_insert(0) += 1;
            }
        }
    }
    // ties break on name so re-ingest is deterministic
    if let Some((k, _)) = counts
        .into_iter()
        .max_by(|a, b| a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0)))
    {
        return k;
    }
    project_name(first_cwd)
}

/// Text of a tool_result's `content`: a plain string, or an array of text blocks.
fn tool_result_text(content: &serde_json::Value) -> String {
    match content {
        serde_json::Value::String(s) => cap_str(s, EVENT_CAP),
        serde_json::Value::Array(blocks) => {
            let mut out = String::new();
            for b in blocks {
                if b.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(t) = b.get("text").and_then(|t| t.as_str()) {
                        if !out.is_empty() {
                            out.push('\n');
                        }
                        out.push_str(t);
                        if out.chars().count() > EVENT_CAP {
                            break;
                        }
                    }
                }
            }
            cap_str(&out, EVENT_CAP)
        }
        _ => String::new(),
    }
}

fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>) {
    let data = match fs::read_to_string(path) {
        Ok(d) => d,
        Err(e) => {
            // a present-but-unreadable file is a real problem; silence here made a
            // permissions break look identical to "no history"
            eprintln!("  ! claude: cannot read {}: {e}", path.display());
            return (Vec::new(), Vec::new());
        }
    };
    let file_session = path
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    // tool_use id -> index into `events`, so the later tool_result line can pair up.
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;
    let mut first_cwd = String::new();
    let mut cwd_counts: HashMap<String, usize> = HashMap::new();
    let mut tool_paths: Vec<String> = Vec::new();
    // Raw-byte key needles (sound per raw_str_value's doc). `"message":` admits exactly
    // the user/assistant lines; progress / summary / file-history-snapshot lines skip
    // serde entirely. cwd is histogrammed from EVERY line via raw extraction, preserving
    // the project-attribution semantics for the lines serde never sees.
    let f_msg = memmem::Finder::new(b"\"message\":");
    let f_cwd = memmem::Finder::new(b"\"cwd\":\"");
    let f_toolres = memmem::Finder::new(b"\"tool_result\"");
    for line in data.lines() {
        if line.is_empty() {
            continue;
        }
        let bytes = line.as_bytes();
        if let Some(c) = raw_str_value(&f_cwd, bytes) {
            if first_cwd.is_empty() {
                first_cwd = c.to_string();
            }
            *cwd_counts.entry(c.to_string()).or_insert(0) += 1;
        }
        if f_msg.find(bytes).is_none() {
            continue;
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(_) => continue,
        };
        let session = l
            .session_id
            .as_deref()
            .unwrap_or(file_session.as_str())
            .to_string();
        // Assistant turn -> attach its text + model to the user message it answers, and note
        // the files it touched (to infer the real working dir at the end).
        if l.ty.as_deref() == Some("assistant") {
            if let Some(m) = &l.message {
                if m.role.as_deref() == Some("assistant") {
                    // Assistant content is always needed (tool paths, events, reply text):
                    // parse the RawValue into a DOM once.
                    let content_val: Option<serde_json::Value> =
                        m.content.and_then(|r| serde_json::from_str(r.get()).ok());
                    if let Some(content) = &content_val {
                        collect_tool_paths(content, &mut tool_paths);
                        // tool_use blocks -> events (sidechain lines too: a subagent's
                        // internal calls carry the same sessionId and belong in the stream).
                        if let serde_json::Value::Array(blocks) = content {
                            for b in blocks {
                                if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                                    continue;
                                }
                                let name = b
                                    .get("name")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("?")
                                    .to_string();
                                let call_id = b
                                    .get("id")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or_default()
                                    .to_string();
                                let input =
                                    b.get("input").map(summarize_tool_input).unwrap_or_default();
                                let kind = if name == "Task" || name == "Agent" {
                                    "subagent_start"
                                } else {
                                    "tool"
                                };
                                if !call_id.is_empty() {
                                    pending.insert(call_id.clone(), events.len());
                                }
                                events.push(Event {
                                    agent: "claude",
                                    session: session.clone(),
                                    ts: ts_millis(l.timestamp.as_deref()),
                                    kind,
                                    name,
                                    input,
                                    output: String::new(),
                                    ok: None,
                                    call_id,
                                    child_session: String::new(),
                                });
                            }
                        }
                    }
                    if let Some(last) = out.last_mut() {
                        if last.model.is_empty() {
                            if let Some(md) = l.message.as_ref().and_then(|m| m.model.as_deref()) {
                                if !md.is_empty() {
                                    last.model = md.to_string();
                                }
                            }
                        }
                        if let Some(txt) = content_val.as_ref().and_then(extract_text) {
                            crate::ingest::append_capped(&mut last.reply, &txt, 1600);
                        }
                    }
                }
            }
            continue;
        }
        if l.ty.as_deref() != Some("user") {
            continue;
        }
        // User content parses lazily: the tool_result pairing only runs when the raw line
        // contains the marker, and lines that fail the human filters never build a DOM.
        let raw_content = l.message.as_ref().and_then(|m| m.content);
        let mut content_val: Option<serde_json::Value> = None;
        // tool_result blocks arrive in user-typed lines (userType external, isMeta null),
        // so pair them BEFORE the human-turn filters would drop the line.
        if f_toolres.find(bytes).is_some() {
            if content_val.is_none() {
                content_val = raw_content.and_then(|r| serde_json::from_str(r.get()).ok());
            }
            if let Some(serde_json::Value::Array(blocks)) = &content_val {
                for b in blocks {
                    if b.get("type").and_then(|t| t.as_str()) != Some("tool_result") {
                        continue;
                    }
                    let id = b.get("tool_use_id").and_then(|v| v.as_str()).unwrap_or("");
                    if let Some(&i) = pending.get(id) {
                        let ev = &mut events[i];
                        if let Some(c) = b.get("content") {
                            ev.output = tool_result_text(c);
                        }
                        ev.ok = Some(b.get("is_error").and_then(|v| v.as_bool()) != Some(true));
                        pending.remove(id);
                    }
                }
            }
        }
        if l.is_meta == Some(true) || l.is_sidechain == Some(true) {
            continue;
        }
        if let Some(ut) = l.user_type.as_deref() {
            if ut != "external" {
                continue;
            }
        }
        let msg = match &l.message {
            Some(m) => m,
            None => continue,
        };
        if msg.role.as_deref() != Some("user") {
            continue;
        }
        if content_val.is_none() {
            content_val = raw_content.and_then(|r| serde_json::from_str(r.get()).ok());
        }
        let text = match content_val.as_ref().and_then(extract_text) {
            Some(t) if !t.trim().is_empty() => t,
            _ => continue,
        };
        if is_wrapper(&text) {
            continue;
        }
        out.push(crate::model::RawMessage {
            agent: "claude",
            project: String::new(), // filled once per session below
            session,
            ts: ts_millis(l.timestamp.as_deref()),
            turn,
            text,
            model: String::new(),
            reply: String::new(),
        });
        turn += 1;
    }
    // One project for the whole session: where the work actually happened.
    let project = primary_project(&cwd_counts, &tool_paths, &first_cwd);
    for m in &mut out {
        m.project = project.clone();
    }
    (
        out.into_iter()
            .map(crate::model::RawMessage::freeze)
            .collect(),
        events,
    )
}

/// Walk all real project dirs and collect the user's Claude messages + tool events.
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".claude").join("projects");
    let dirs = match fs::read_dir(&root) {
        Ok(d) => d,
        Err(_) => return (Vec::new(), Vec::new()),
    };

    // Gather top-level *.jsonl from each non-throwaway project dir.
    let mut files: Vec<std::path::PathBuf> = Vec::new();
    for entry in dirs.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.contains("Temp-claude") || name.contains("claude-worker") {
            continue;
        }
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        if let Ok(rd) = fs::read_dir(&dir) {
            for f in rd.flatten() {
                let p = f.path();
                if p.extension().and_then(|e| e.to_str()) == Some("jsonl") {
                    files.push(p);
                }
            }
        }
    }

    let pass = crate::ingest_cache::collect_cached(cache, &root, &files, parse_file);
    (pass.messages, pass.events)
}
