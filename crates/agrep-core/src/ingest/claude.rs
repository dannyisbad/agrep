//! Claude Code adapter: ~/.claude/projects/<cwd-slug>/*.jsonl
//! Session files plus `subagents/` side transcripts; structurally identified
//! throwaway-worker records are accounted as named skips.
//! Keeps real human turns: type=user, not meta/sidechain, userType external|absent,
//! string or text-block content, not a command/system wrapper.

use std::borrow::Cow;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

use memchr::memmem;
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::ingest::{cap_event_output, is_wrapper, project_name, summarize_tool_input_with_chars};
use crate::model::{Event, Message};

// Borrowed deserialization: scalar fields are Cow (borrow from the line when escape-free,
// allocate only when JSON-escaped) and `content` stays a RawValue slice, parsed into a
// DOM only by the lines that actually need one - most fail a filter first.
#[derive(Deserialize)]
struct Line<'a> {
    #[serde(rename = "type", borrow)]
    ty: Option<Cow<'a, str>>,
    #[serde(rename = "isMeta")]
    is_meta: Option<bool>,
    #[serde(rename = "isSidechain")]
    is_sidechain: Option<bool>,
    #[serde(rename = "isCompactSummary")]
    is_compact_summary: Option<bool>,
    #[serde(rename = "userType", borrow)]
    user_type: Option<Cow<'a, str>>,
    #[serde(borrow)]
    message: Option<Msg<'a>>,
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    #[serde(rename = "sessionId", borrow)]
    session_id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    cwd: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct ProjectLine<'a> {
    #[serde(borrow)]
    cwd: Option<&'a RawValue>,
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

fn has_json_key(finder: &memmem::Finder, bytes: &[u8]) -> bool {
    let mut offset = 0;
    while let Some(found) = finder.find(&bytes[offset..]) {
        let mut cursor = offset + found + finder.needle().len();
        while matches!(bytes.get(cursor), Some(b' ' | b'\t' | b'\r' | b'\n')) {
            cursor += 1;
        }
        if bytes.get(cursor) == Some(&b':') {
            return true;
        }
        offset = cursor.min(bytes.len());
        if offset == bytes.len() {
            break;
        }
    }
    false
}

fn optional_cwd(value: Option<&RawValue>) -> Option<String> {
    value
        .and_then(|raw| serde_json::from_str::<String>(raw.get()).ok())
        .filter(|cwd| !cwd.is_empty())
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
    let mut segs: Vec<&str> = d.split('/').filter(|s| !s.is_empty()).collect();
    // UNC/WSL paths (//wsl.localhost/Ubuntu/..., //server/share/...) lead with host
    // and share segments that would otherwise win the bucket
    if d.starts_with("//") {
        segs.drain(..2.min(segs.len()));
    }
    let home = crate::ingest::home().to_string_lossy().replace('\\', "/");
    let home_segs: Vec<&str> = home.split('/').filter(|s| !s.is_empty()).collect();
    // strip home per component: a byte-prefix compare on a lowercased copy can slice
    // mid-char, and would false-match a shorter home prefix inside a longer username
    if segs.len() >= home_segs.len()
        && segs
            .iter()
            .zip(&home_segs)
            .all(|(a, b)| a.eq_ignore_ascii_case(b))
    {
        segs.drain(..home_segs.len());
    }
    let user = crate::ingest::home_leaf();
    // the segment right after a Users/home container is a username on ANY machine
    // (a migrated corpus's cwds carry another box's user) - home_leaf() only knows
    // this one's, so any post-Users/home segment is skipped unconditionally.
    let mut after_user_container = false;
    segs.into_iter()
        .find(|s| {
            let sl = s.to_ascii_lowercase();
            if sl.ends_with(':') || std::mem::take(&mut after_user_container) || sl == user {
                return false;
            }
            if matches!(sl.as_str(), "users" | "home") {
                after_user_container = true;
                return false;
            }
            !matches!(
                sl.as_str(),
                "desktop"
                    | "documents"
                    | "downloads"
                    | "onedrive"
                    | "tmp"
                    | "temp"
                    | "appdata"
                    | "local"
                    | "locallow"
                    | "roaming"
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
    fallback: &str,
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
    if first_cwd.is_empty() {
        fallback.to_string()
    } else {
        project_name(first_cwd)
    }
}

fn directory_project(path: &Path) -> String {
    let parts: Vec<&str> = path.iter().filter_map(|part| part.to_str()).collect();
    if let Some(index) = parts.iter().rposition(|part| *part == "projects") {
        if let Some(slug) = parts.get(index + 1) {
            return (*slug).to_string();
        }
    }
    if let Some(index) = parts.iter().rposition(|part| *part == "subagents") {
        if let Some(slug) = index.checked_sub(2).and_then(|value| parts.get(value)) {
            return (*slug).to_string();
        }
    }
    path.parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .unwrap_or("claude")
        .to_string()
}

/// Text of a tool_result's `content`: a plain string, or an array of text blocks.
fn tool_result_text(content: &serde_json::Value) -> (String, usize, usize) {
    match content {
        serde_json::Value::String(s) => cap_event_output(s),
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
            cap_event_output(&out)
        }
        _ => (String::new(), 0, 0),
    }
}

/// Side-session context, derived from the path so the cached per-file parse
/// signature stays untouched: `<project>/<parent-session>/subagents/**/agent-*.jsonl`
/// -> Some(parent session id). Top-level session files -> None.
fn side_context(path: &Path) -> Option<String> {
    let mut comps: Vec<&str> = path.iter().filter_map(|c| c.to_str()).collect();
    comps.pop(); // the file itself
    let sub = comps.iter().rposition(|c| *c == "subagents")?;
    comps.get(sub.checked_sub(1)?).map(|s| s.to_string())
}

#[cfg(test)]
fn parse_file(path: &Path) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    parse_file_with_tally(path, crate::intake::file("claude", path))
}

fn parse_file_stamped(
    path: &Path,
    mtime_ns: i64,
    size: u64,
) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    parse_file_with_tally(
        path,
        crate::intake::file_stamped("claude", path, mtime_ns, size),
    )
}

fn parse_file_with_tally(
    path: &Path,
    tally: std::sync::Arc<crate::intake::Tally>,
) -> (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome) {
    // Child transcript lines carry the PARENT's sessionId; the file stem is the
    // child identity and must not be overridden by it.
    let mut side_parent = side_context(path);
    let is_side = side_parent.is_some();
    let data = match crate::ingest::read_lossy(path) {
        Ok(d) => d,
        Err(e) => {
            // a present-but-unreadable file is a real problem; silence would make a
            // permissions break look identical to "no history"
            eprintln!(
                "  ! claude: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&e)
            );
            tally.seen();
            tally.error(&format!("cannot read: {e}"));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Skipped,
            );
        }
    };
    if is_throwaway_path(path) {
        // Keep byte-for-byte parity with audit's binary `line.strip()` census:
        // ASCII-whitespace-only lines are blank; non-ASCII bytes remain records.
        let records = data
            .lines()
            .filter(|line| line.bytes().any(|byte| !byte.is_ascii_whitespace()))
            .count() as u64;
        tally.seen_n(records);
        tally.skip_n(crate::intake::Skip::Throwaway, records);
        return (
            Vec::new(),
            Vec::new(),
            crate::ingest_cache::ReadOutcome::Complete,
        );
    }
    let file_session = path
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut recap_turns = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    // tool_use id -> index into `events`, so the later tool_result line can pair up.
    let mut pending: HashMap<String, usize> = HashMap::new();
    let mut turn = 0u32;
    let mut first_cwd = String::new();
    let mut cwd_counts: HashMap<String, usize> = HashMap::new();
    let mut tool_paths: Vec<String> = Vec::new();
    // Cheap key probes choose which borrowed schema to parse; attribution still comes from JSON.
    let f_msg = memmem::Finder::new(b"\"message\"");
    let f_cwd = memmem::Finder::new(b"\"cwd\"");
    let f_toolres = memmem::Finder::new(b"\"tool_result\"");
    // A side file's parent is decided ONCE, before any row is emitted: the first inner
    // sessionId (the parent's indexed identity, preferred since a dir stem can drift),
    // else the dir stem. A parent that flips mid-file splits one child across two ids.
    if is_side {
        let f_sess = memmem::Finder::new(b"\"sessionId\":\"");
        if let Some(pid) = data
            .lines()
            .find_map(|line| raw_str_value(&f_sess, line.as_bytes()).filter(|s| !s.is_empty()))
        {
            side_parent = Some(pid.to_string());
        }
    }
    for (record_ordinal, line) in data.lines().enumerate() {
        if line.is_empty() {
            continue;
        }
        tally.seen();
        let bytes = line.as_bytes();
        if !has_json_key(&f_msg, bytes) {
            if has_json_key(&f_cwd, bytes) {
                if let Ok(fields) = serde_json::from_str::<ProjectLine>(line) {
                    if let Some(cwd) = optional_cwd(fields.cwd) {
                        if first_cwd.is_empty() {
                            first_cwd = cwd.clone();
                        }
                        *cwd_counts.entry(cwd).or_insert(0) += 1;
                    }
                }
            }
            // progress / summary / file-history lines: structural, no message payload
            tally.skip(crate::intake::Skip::NonMessage);
            continue;
        }
        let l: Line = match serde_json::from_str(line) {
            Ok(l) => l,
            Err(e) => {
                tally.error(&format!("{e}: {}", crate::intake::clip(line, 80)));
                continue;
            }
        };
        if let Some(cwd) = optional_cwd(l.cwd) {
            if first_cwd.is_empty() {
                first_cwd = cwd.clone();
            }
            *cwd_counts.entry(cwd).or_insert(0) += 1;
        }
        let session = if is_side {
            file_session.clone()
        } else {
            l.session_id
                .as_deref()
                .unwrap_or(file_session.as_str())
                .to_string()
        };
        if l.is_sidechain == Some(true) && side_parent.is_none() {
            // inline sidechain rows duplicate the subagents/ child transcripts (their own
            // side sessions); dropping the whole line covers events and reply text too
            tally.skip(crate::intake::Skip::Sidechain);
            continue;
        }
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
                        // tool_use blocks -> events (inline sidechain lines never reach
                        // here; the subagents/ child transcript is their one source)
                        if let serde_json::Value::Array(blocks) = content {
                            for (block_ordinal, b) in blocks.iter().enumerate() {
                                if b.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                                    continue;
                                }
                                let name = b
                                    .get("name")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("?")
                                    .to_string();
                                let native_call_id = b
                                    .get("id")
                                    .and_then(|v| v.as_str())
                                    .filter(|id| !id.trim().is_empty());
                                let call_id =
                                    native_call_id.map(str::to_string).unwrap_or_else(|| {
                                        format!(
                                        "claude:{file_session}:{record_ordinal}:{block_ordinal}"
                                    )
                                    });
                                let (input, input_chars) = b
                                    .get("input")
                                    .map(summarize_tool_input_with_chars)
                                    .unwrap_or_default();
                                let kind = if name == "Task" || name == "Agent" {
                                    "subagent_start"
                                } else {
                                    "tool"
                                };
                                if native_call_id.is_some() {
                                    pending.insert(call_id.clone(), events.len());
                                }
                                tally.event();
                                events.push(Event {
                                    agent: "claude",
                                    session: session.clone(),
                                    ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
                                    kind,
                                    name,
                                    input,
                                    output: String::new(),
                                    input_chars,
                                    output_chars: 0,
                                    output_bytes: 0,
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
                                // "<synthetic>" is a marker, not a model: a hook-injected
                                // reply must not stamp the row it follows (it once
                                // reclassified compact summaries and poisoned the pool).
                                if !md.is_empty() && md != "<synthetic>" {
                                    last.model = md.to_string();
                                }
                            }
                        }
                        if let Some(txt) = content_val.as_ref().and_then(extract_text) {
                            let chars = crate::ingest::append_capped(
                                &mut last.reply,
                                &txt,
                                crate::ingest::REPLY_CAP,
                            );
                            last.reply_chars += chars;
                        }
                    }
                }
            }
            tally.agent_row();
            continue;
        }
        if l.ty.as_deref() != Some("user") {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }
        // User content parses lazily: the tool_result pairing only runs when the raw line
        // contains the marker, and lines that fail the human filters never build a DOM.
        let raw_content = l.message.as_ref().and_then(|m| m.content);
        let mut content_val: Option<serde_json::Value> = None;
        // tool_result blocks arrive in user-typed lines (userType external, isMeta null),
        // so pair them BEFORE the human-turn filters would drop the line.
        if f_toolres.find(bytes).is_some() {
            content_val = raw_content.and_then(|r| serde_json::from_str(r.get()).ok());
            if let Some(serde_json::Value::Array(blocks)) = &content_val {
                for b in blocks {
                    if b.get("type").and_then(|t| t.as_str()) != Some("tool_result") {
                        continue;
                    }
                    let id = b.get("tool_use_id").and_then(|v| v.as_str()).unwrap_or("");
                    if let Some(&i) = pending.get(id) {
                        let ev = &mut events[i];
                        if let Some(c) = b.get("content") {
                            (ev.output, ev.output_chars, ev.output_bytes) = tool_result_text(c);
                        }
                        ev.ok = Some(b.get("is_error").and_then(|v| v.as_bool()) != Some(true));
                        pending.remove(id);
                    }
                }
            }
        }
        if l.is_meta == Some(true) {
            tally.skip(crate::intake::Skip::Meta);
            continue;
        }
        if let Some(ut) = l.user_type.as_deref() {
            if ut != "external" {
                tally.skip(crate::intake::Skip::NonHuman);
                continue;
            }
        }
        let msg = match &l.message {
            Some(m) => m,
            None => {
                tally.skip(crate::intake::Skip::NonMessage);
                continue;
            }
        };
        if msg.role.as_deref() != Some("user") {
            tally.skip(crate::intake::Skip::NonHuman);
            continue;
        }
        if content_val.is_none() {
            content_val = raw_content.and_then(|r| serde_json::from_str(r.get()).ok());
        }
        let text = match content_val.as_ref().and_then(extract_text) {
            Some(t) if !t.trim().is_empty() => t,
            _ => {
                tally.skip(crate::intake::Skip::EmptyText);
                continue;
            }
        };
        if is_wrapper(&text) {
            tally.skip(crate::intake::Skip::Wrapper);
            continue;
        }
        tally.row();
        if l.is_compact_summary == Some(true) {
            recap_turns.push(turn);
        }
        out.push(crate::model::RawMessage {
            agent: "claude",
            project: String::new(), // filled once per session below
            session,
            ts: parse_timestamp::rfc3339(l.timestamp.as_deref()),
            turn,
            text,
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            side: is_side,
            parent: side_parent.clone().unwrap_or_default(),
        });
        turn += 1;
    }
    // One project for the whole session: where the work actually happened.
    let project = primary_project(
        &cwd_counts,
        &tool_paths,
        &first_cwd,
        &directory_project(path),
    );
    for m in &mut out {
        m.project = project.clone();
    }
    (
        out.into_iter()
            .map(|raw| {
                let turn = raw.turn;
                let mut message = raw.freeze();
                if recap_turns.binary_search(&turn).is_ok() {
                    message.who = "recap".into();
                    message.model_source = "recap".into();
                }
                message
            })
            .collect(),
        events,
        crate::ingest_cache::ReadOutcome::Complete,
    )
}

/// Walk all real project dirs and collect the user's Claude messages + tool events.
pub fn collect(cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
    let root = crate::ingest::home().join(".claude").join("projects");
    let files = cache.preflight_source_paths(&root).unwrap_or_else(|| {
        let mut files = Vec::new();
        let dirs = match fs::symlink_metadata(&root) {
            Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => match fs::read_dir(&root) {
                Ok(dirs) => Some(dirs),
                Err(error) => {
                    crate::ingest::warn_source_skip("claude", &root, &error);
                    None
                }
            },
            Ok(_) => {
                crate::ingest::warn_source_skip(
                    "claude",
                    &root,
                    "source root is not a plain directory",
                );
                None
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                crate::ingest::warn_source_skip("claude", &root, &error);
                None
            }
        };
        if let Some(dirs) = dirs {
            for entry in dirs {
                let entry = match entry {
                    Ok(entry) => entry,
                    Err(error) => {
                        crate::ingest::warn_source_skip("claude", &root, &error);
                        continue;
                    }
                };
                let dir = entry.path();
                match plain_entry_metadata(&entry) {
                    Ok(Some(meta)) if meta.is_dir() => gather_jsonl(&dir, &mut files, 0),
                    Ok(None) => crate::ingest::warn_source_skip(
                        "claude",
                        &dir,
                        "symlink sources are not followed",
                    ),
                    Ok(Some(_)) => {}
                    Err(error) => crate::ingest::warn_source_skip("claude", &dir, &error),
                }
            }
        }
        files.sort_unstable();
        files
    });

    let pass = crate::ingest_cache::collect_cached_stamped_for(
        cache,
        "claude",
        &root,
        &files,
        parse_file_stamped,
    );
    (pass.messages, pass.events)
}

/// Recursively gather every transcript jsonl under a project dir (sessions,
/// subagents/ side transcripts, nested project-slug dirs). Deliberately skipped
/// worker transcripts remain in this set so freshness and intake accounting see them.
fn gather_jsonl(dir: &Path, files: &mut Vec<std::path::PathBuf>, depth: u8) {
    if depth > 5 {
        return;
    }
    match fs::symlink_metadata(dir) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => {}
        Ok(_) => {
            crate::ingest::warn_source_skip("claude", dir, "source is not a plain directory");
            return;
        }
        Err(error) => {
            crate::ingest::warn_source_skip("claude", dir, &error);
            return;
        }
    }
    let rd = match fs::read_dir(dir) {
        Ok(rd) => rd,
        Err(error) => {
            crate::ingest::warn_source_skip("claude", dir, &error);
            return;
        }
    };
    for entry in rd {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip("claude", dir, &error);
                continue;
            }
        };
        let p = entry.path();
        let metadata = match plain_entry_metadata(&entry) {
            Ok(Some(metadata)) => metadata,
            Ok(None) => {
                crate::ingest::warn_source_skip("claude", &p, "symlink sources are not followed");
                continue;
            }
            Err(error) => {
                crate::ingest::warn_source_skip("claude", &p, &error);
                continue;
            }
        };
        if metadata.is_dir() {
            gather_jsonl(&p, files, depth + 1);
        } else if metadata.is_file() && p.extension().and_then(|e| e.to_str()) == Some("jsonl") {
            files.push(p);
        }
    }
}

fn is_throwaway_name(name: &std::ffi::OsStr) -> bool {
    use std::sync::OnceLock;

    static TEMP_WORKER_PREFIXES: OnceLock<Vec<String>> = OnceLock::new();
    let prefixes = TEMP_WORKER_PREFIXES.get_or_init(|| {
        let temp = std::env::temp_dir();
        let mut roots = vec![temp.clone()];
        if let Ok(canonical) = temp.canonicalize() {
            if canonical != temp {
                roots.push(canonical);
            }
        }
        roots
            .iter()
            .map(|root| claude_worker_temp_prefix(root))
            .collect()
    });
    is_throwaway_name_for_prefixes(name, prefixes)
}

fn claude_worker_temp_prefix(root: &Path) -> String {
    // Claude's project-folder encoding replaces every non-ASCII-alphanumeric
    // character with '-'. Re-encode the actual OS temp root instead of trying
    // to decode a lossy slug or accepting any nested directory named "tmp".
    let mut slug: String = root
        .to_string_lossy()
        .chars()
        .map(|value| {
            if value.is_ascii_alphanumeric() {
                value.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    slug.push_str("-claude-worker-");
    slug
}

fn is_throwaway_name_for_prefixes(name: &std::ffi::OsStr, prefixes: &[String]) -> bool {
    // tempfile adds exactly eight characters. An underscore in that suffix is
    // encoded as '-' (for example `_zgq26av` becomes `-zgq26av`).
    let name = name.to_string_lossy().to_ascii_lowercase();
    prefixes.iter().any(|prefix| {
        name.strip_prefix(prefix).is_some_and(|token| {
            token.len() == 8
                && token
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        })
    })
}

fn is_throwaway_path(path: &Path) -> bool {
    path.parent().is_some_and(|parent| {
        parent
            .components()
            .any(|component| is_throwaway_name(component.as_os_str()))
    })
}

fn is_discovered_transcript(path: &Path) -> bool {
    if path.extension().and_then(|x| x.to_str()) != Some("jsonl") {
        return false;
    }
    let root = crate::ingest::home().join(".claude").join("projects");
    let relative =
        crate::ingest::registry::relative_path(path, &root).unwrap_or_else(|| path.to_path_buf());
    let depth = relative.components().count();
    (2..=7).contains(&depth)
}

/// Registry entry (see ingest::registry). Byte-identical wrapper over `collect`.
pub struct Claude;
impl crate::ingest::registry::Adapter for Claude {
    fn name(&self) -> &'static str {
        "claude"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        collect(cache)
    }
    fn store_roots(&self) -> Vec<std::path::PathBuf> {
        vec![crate::ingest::home().join(".claude").join("projects")]
    }
    fn store_content(&self, path: &std::path::Path) -> bool {
        path.extension().and_then(|x| x.to_str()) == Some("jsonl")
    }
    fn freshness_content(&self, path: &std::path::Path) -> bool {
        is_discovered_transcript(path)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        claude_worker_temp_prefix, has_json_key, is_discovered_transcript, is_throwaway_name,
        is_throwaway_name_for_prefixes, parse_file, parse_file_with_tally,
    };

    #[test]
    fn throwaway_detector_matches_only_the_anchored_worker_temp_shape() {
        use std::ffi::OsStr;

        let roots = [
            std::path::Path::new(r"C:\Users\Example\AppData\Local\Temp"),
            std::path::Path::new("/private/tmp"),
            std::path::Path::new("/var/folders/xy/fixture/T"),
        ];
        let prefixes: Vec<_> = roots
            .iter()
            .map(|root| claude_worker_temp_prefix(root))
            .collect();
        let matches = |name: &str| is_throwaway_name_for_prefixes(OsStr::new(name), &prefixes);

        assert!(matches(
            "C--Users-Example-AppData-Local-Temp-claude-worker--zgq26av"
        ));
        assert!(matches("-private-tmp-claude-worker-a1b2c3d4"));
        assert!(matches("-var-folders-xy-fixture-T-claude-worker-0ggx5o3i"));
        assert!(!matches("claude-worker-poc"));
        assert!(!matches(
            "C--Users-Example-AppData-Local-Temp-claude-phx-nyc-site"
        ));
        assert!(!matches("C--Users-Example-Desktop-claude-worker-a1b2c3d4"));
        assert!(!matches("-Users-Example-work-tmp-claude-worker-a1b2c3d4"));
        assert!(!matches(
            "-Users-Example-work-var-folders-xy-fixture-T-claude-worker-a1b2c3d4"
        ));
        assert!(!matches(
            "C--Users-Example-AppData-Local-Temp-claude-worker-short"
        ));

        let current = format!(
            "{}a1b2c3d4",
            claude_worker_temp_prefix(&std::env::temp_dir())
        );
        assert!(is_throwaway_name(OsStr::new(&current)));
    }

    #[test]
    fn throwaway_file_is_discovered_and_every_nonblank_line_is_a_named_skip() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-throwaway-accounting-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let temp = std::env::temp_dir();
        let mut slugs = vec![format!("{}a1b2c3d4", claude_worker_temp_prefix(&temp))];
        if let Ok(canonical) = temp.canonicalize() {
            let canonical_slug = format!("{}0ggx5o3i", claude_worker_temp_prefix(&canonical));
            if !slugs.contains(&canonical_slug) {
                slugs.push(canonical_slug);
            }
        }
        for slug in slugs {
            let project = root.join("projects").join(&slug);
            std::fs::create_dir_all(&project).unwrap();
            let path = project.join("session.jsonl");
            std::fs::write(
                &path,
                concat!(
                    "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"noise\"}}\n",
                    "  \r\n",
                    "not json, but still a source record\n",
                    "\n",
                ),
            )
            .unwrap();

            assert!(is_discovered_transcript(
                &std::path::Path::new(&slug).join("session.jsonl")
            ));
            let tally = std::sync::Arc::new(crate::intake::Tally::default());
            let (messages, events, outcome) =
                parse_file_with_tally(&path, std::sync::Arc::clone(&tally));
            assert_eq!(outcome, crate::ingest_cache::ReadOutcome::Complete);
            assert!(messages.is_empty());
            assert!(events.is_empty());
            assert_eq!(
                tally.test_record_counts(crate::intake::Skip::Throwaway),
                (2, 0, 0, 2, 0),
                "{slug}"
            );
        }

        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn genuine_temp_claude_and_claude_worker_projects_still_emit_rows() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-genuine-projects-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        for project in [
            "C--Users-Example-AppData-Local-Temp-claude-phx-nyc-site",
            "claude-worker-poc",
        ] {
            let dir = root.join("projects").join(project);
            std::fs::create_dir_all(&dir).unwrap();
            let path = dir.join("session.jsonl");
            std::fs::write(
                &path,
                "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"real work\"}}\n",
            )
            .unwrap();
            let tally = std::sync::Arc::new(crate::intake::Tally::default());
            let (messages, events, outcome) =
                parse_file_with_tally(&path, std::sync::Arc::clone(&tally));
            assert_eq!(outcome, crate::ingest_cache::ReadOutcome::Complete);
            assert_eq!(messages.len(), 1, "{project}");
            assert!(events.is_empty(), "{project}");
            assert_eq!(
                tally.test_record_counts(crate::intake::Skip::Throwaway),
                (1, 1, 0, 0, 0),
                "{project}"
            );
        }

        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn byte_key_probe_accepts_json_whitespace_without_value_false_positives() {
        let message = memchr::memmem::Finder::new(b"\"message\"");
        let cwd = memchr::memmem::Finder::new(b"\"cwd\"");
        assert!(has_json_key(&message, br#"{"message" : {}}"#));
        assert!(has_json_key(&cwd, b"{\"cwd\"\t:\"/work\"}"));
        assert!(!has_json_key(&message, br#"{"content":"message"}"#));
        assert!(!has_json_key(
            &cwd,
            br#"{"content":"fake \"cwd\" : value"}"#
        ));
    }

    #[test]
    fn compact_summary_provenance_marks_a_recap_without_text_matching() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-structural-recap-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"type\":\"user\",\"userType\":\"external\",",
                "\"isCompactSummary\":true,\"uuid\":\"summary-uuid\",",
                "\"parentUuid\":\"previous-uuid\",\"message\":",
                "{\"role\":\"user\",\"content\":\"structural continuation\"}}\n",
            ),
        )
        .unwrap();
        let (messages, _, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].who, "recap");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn a_synthetic_assistant_reply_never_reclassifies_the_summary() {
        // /compact writes the summary, then a hook-style assistant row with
        // the "<synthetic>" model marker follows; adopting that marker made
        // row_kind call the boundary synthetic and postcompact stopped resolving.
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-recap-vs-synthetic-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"type\":\"user\",\"userType\":\"external\",",
                "\"isCompactSummary\":true,\"uuid\":\"summary-uuid\",",
                "\"message\":",
                "{\"role\":\"user\",\"content\":\"structural continuation\"}}\n",
                "{\"type\":\"assistant\",\"userType\":\"external\",\"message\":",
                "{\"role\":\"assistant\",\"model\":\"<synthetic>\",\"content\":",
                "[{\"type\":\"text\",\"text\":\"No response requested.\"}]}}\n",
            ),
        )
        .unwrap();
        let (messages, _, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].who, "recap");
        assert!(messages[0].model.is_empty(), "marker adopted as a model");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn project_attribution_reads_top_level_cwd_structurally() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-structural-cwd-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let dir = root.join("projects").join("fallback-slug");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"type\":\"progress\",\"cwd\" : \"/Users/alice/Desktop/alpha\"}\n",
                "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"one\"},\"cwd\":\"/Users/alice/Desktop/alpha\"}\n",
                "{\"cwd\" : \"/Users/alice/Desktop/alpha\", \"message\" : {\"content\": \"two\", \"role\": \"user\"}, \"type\": \"user\"}\n",
                "{\"message\":{\"role\":\"user\",\"content\":\"fake \\\"cwd\\\": \\\"attacker\\\"\"},\"cwd\":\"C:\\\\Users\\\\alice\\\\Desktop\\\\alpha\",\"type\":\"user\"}\n",
                "{\"type\":\"user\",\"cwd\":\"/Users/alice/Desktop/\\u0061lpha\",\"message\":{\"role\":\"user\",\"content\":\"four\"}}\n",
            ),
        )
        .unwrap();
        let (messages, _, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(messages.len(), 4);
        assert!(messages.iter().all(|message| &*message.project == "alpha"));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn spaced_message_with_wrong_cwd_uses_the_directory_slug() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-cwd-fallback-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let dir = root.join("projects").join("recorded-project-slug");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("session.jsonl");
        std::fs::write(
            &path,
            "{\"type\":\"user\",\"message\" : {\"role\":\"user\",\"content\":\"valid despite bad cwd\"},\"cwd\" : 42}\n",
        )
        .unwrap();
        let (messages, _, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].project, "recorded-project-slug");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn directory_fallback_uses_the_nearest_projects_slug() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-nested-projects-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let dir = root
            .join("projects")
            .join("outer")
            .join("home")
            .join(".claude")
            .join("projects")
            .join("right");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("session.jsonl");
        std::fs::write(
            &path,
            "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"turn\"}}\n",
        )
        .unwrap();
        let (messages, _, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].project, "right");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn skipped_line_cwd_allows_whitespace_before_the_colon() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-spaced-cwd-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let dir = root.join("projects").join("fallback-slug");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("session.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"type\":\"progress\",\"cwd\" : \"/Users/alice/Desktop/alpha\"}\n",
                "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"turn\"}}\n",
            ),
        )
        .unwrap();
        let (messages, _, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].project, "alpha");
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn side_file_parent_is_uniform_across_rows() {
        let root = std::env::temp_dir().join(format!(
            "agrep-claude-side-parent-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        // Dir stem drifted from the id the parent indexes under; the early line
        // carries no sessionId, a later one carries the inner (real) parent id.
        let dir = root
            .join("proj-slug")
            .join("stem-drifted-away")
            .join("subagents");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("agent-child1.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":\"early question\"},\"cwd\":\"/work/proj\"}\n",
                "{\"type\":\"user\",\"sessionId\":\"11111111-aaaa-4bbb-8ccc-222222222222\",\"message\":{\"role\":\"user\",\"content\":\"late question\"}}\n",
            ),
        )
        .unwrap();
        let (messages, _, healthy) = parse_file(&path);
        assert_eq!(healthy, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(messages.len(), 2);
        for m in &messages {
            assert!(m.side);
            assert_eq!(&*m.session, "agent-child1");
            assert_eq!(&*m.parent, "11111111-aaaa-4bbb-8ccc-222222222222");
        }
        let _ = std::fs::remove_dir_all(root);
    }
}
