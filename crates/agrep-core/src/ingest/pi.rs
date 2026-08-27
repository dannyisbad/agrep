//! pi / oh-my-pi adapter: <root>/agent/sessions/<cwd-slug>/<session>.jsonl
//!
//! Two stores share one format: `~/.pi` (pi) and `~/.omp` (the oh-my-pi fork).
//! Each session is JSONL of `type`-tagged records. The first line is a `session`
//! header carrying `version` (1|2|3), `id`, and `cwd`; every later record has an
//! `id`, a `parentId`, and an ISO `timestamp`.
//!
//! v2/v3 are TREES, not lists: branching appends children off an earlier entry
//! in the same file, so the tail of the file is the current leaf and the active
//! branch is the parent chain walked back from it (pi's own buildContextEntries).
//! Abandoned branches stay in the file and are deliberately not indexed - pi's
//! resume never shows them. v1 has no ids and is read in file order.
//!
//! Records that matter here: `message` (role user/assistant/toolResult, content
//! blocks text/thinking/toolCall) and `compaction`, whose summary becomes a
//! recap row so `agrep postcompact` works without a hook. `model_change` applies
//! forward. Reasoning (`thinking`) is excluded like every other adapter.
//! The fork adds fields (shortSummary, fromExtension, developer role, named
//! sessions like advisor sidecars); unknown records and roles are skipped, not
//! failed on. Sidecars receive `synthetic: true` user-role mirrors of the
//! watched session's transcript; those are sidechain copies, never user rows.
//! A sidecar assistant left anchorless by those skips is the sidecar's own
//! voice (advisor advisories): its text is indexed as its own row.

use std::collections::HashMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

use crate::ingest::parse_timestamp;
use crate::ingest::registry::{metadata_is_link, plain_entry_metadata};
use crate::ingest::{cap_event_output, is_wrapper, summarize_tool_input_with_chars};
use crate::model::{Event, Message};

/// What one parsed session yields: rows, tool events, and how completely the
/// file was read.
type Parsed = (Vec<Message>, Vec<Event>, crate::ingest_cache::ReadOutcome);

const GZIP_COMPRESSED_MAX_BYTES: u64 = 64 * 1024 * 1024;
const GZIP_DECOMPRESSED_MAX_BYTES: u64 = 256 * 1024 * 1024;

/// One decoded JSONL line plus its position, so the tree walk can order by file
/// arrival without re-parsing.
struct Record {
    value: serde_json::Value,
    ordinal: usize,
}

fn text_of(value: Option<&serde_json::Value>) -> String {
    match value {
        Some(serde_json::Value::String(text)) => text.clone(),
        Some(serde_json::Value::Array(blocks)) => {
            let mut out = String::new();
            for block in blocks {
                if block.get("type").and_then(|t| t.as_str()) != Some("text") {
                    continue;
                }
                if let Some(text) = block.get("text").and_then(|t| t.as_str()) {
                    if text.is_empty() {
                        continue;
                    }
                    if !out.is_empty() {
                        out.push('\n');
                    }
                    out.push_str(text);
                }
            }
            out
        }
        _ => String::new(),
    }
}

/// The active branch, oldest first. The last record in the file is the current
/// leaf; walking parentId back to the root drops branches the user abandoned.
/// A record whose parent is missing ends the walk rather than failing the file.
fn active_branch(records: &[Record]) -> Vec<usize> {
    let mut by_id: HashMap<&str, usize> = HashMap::new();
    for (index, record) in records.iter().enumerate() {
        if let Some(id) = record.value.get("id").and_then(|i| i.as_str()) {
            by_id.insert(id, index);
        }
    }
    let leaf = records.iter().rposition(|record| {
        record.value.get("type").and_then(|t| t.as_str()) != Some("session")
            && record.value.get("id").and_then(|i| i.as_str()).is_some()
    });
    let Some(leaf) = leaf else {
        // v1: no ids at all, so the file order IS the conversation
        return (0..records.len()).collect();
    };
    let mut chain = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut cursor = Some(leaf);
    while let Some(index) = cursor {
        if !seen.insert(index) {
            break; // a parentId cycle is corrupt input, not a reason to panic
        }
        chain.push(index);
        cursor = records[index]
            .value
            .get("parentId")
            .and_then(|p| p.as_str())
            .and_then(|parent| by_id.get(parent).copied());
    }
    chain.reverse();
    chain
}

/// Outputs of every toolResult on the branch, keyed by the call they answer, so
/// a toolCall can be emitted with its result even though they are separate rows.
fn tool_results(records: &[Record], branch: &[usize]) -> HashMap<String, (String, Option<bool>)> {
    let mut results = HashMap::new();
    for &index in branch {
        let message = match records[index].value.get("message") {
            Some(message) if is_role(message, "toolResult") => message,
            _ => continue,
        };
        let Some(call) = message.get("toolCallId").and_then(|c| c.as_str()) else {
            continue;
        };
        let ok = message
            .get("isError")
            .and_then(|e| e.as_bool())
            .map(|errored| !errored);
        results.insert(call.to_string(), (text_of(message.get("content")), ok));
    }
    results
}

fn is_role(message: &serde_json::Value, role: &str) -> bool {
    message.get("role").and_then(|r| r.as_str()) == Some(role)
}

/// The cwd slug directory name, used only when the session header has no cwd.
fn slug_project(path: &Path) -> String {
    path.parent()
        .and_then(|parent| parent.file_name())
        .map(|name| name.to_string_lossy().trim_matches('-').replace('-', "/"))
        .filter(|project| !project.is_empty())
        .unwrap_or_else(|| "pi".to_string())
}

fn read_session_with_limits(
    path: &Path,
    gzip_compressed_max_bytes: u64,
    gzip_decompressed_max_bytes: u64,
) -> std::io::Result<String> {
    if path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".jsonl.gz"))
    {
        let bytes =
            crate::ingest::registry::read_growing_regular_file(path, gzip_compressed_max_bytes)?
                .ok_or_else(|| {
                    std::io::Error::new(std::io::ErrorKind::NotFound, "source does not exist")
                })?;
        let mut decoded = Vec::new();
        flate2::read::GzDecoder::new(bytes.as_slice())
            .take(gzip_decompressed_max_bytes.saturating_add(1))
            .read_to_end(&mut decoded)?;
        if decoded.len() as u64 > gzip_decompressed_max_bytes {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("decompressed source exceeds {gzip_decompressed_max_bytes} bytes"),
            ));
        }
        return Ok(String::from_utf8(decoded)
            .unwrap_or_else(|error| String::from_utf8_lossy(error.as_bytes()).into_owned()));
    }
    crate::ingest::read_lossy(path)
}

fn parse_with(agent: &'static str, path: &Path, parent: Option<String>) -> Parsed {
    parse_with_limits(
        agent,
        path,
        parent,
        GZIP_COMPRESSED_MAX_BYTES,
        GZIP_DECOMPRESSED_MAX_BYTES,
    )
}

fn parse_with_limits(
    agent: &'static str,
    path: &Path,
    parent: Option<String>,
    gzip_compressed_max_bytes: u64,
    gzip_decompressed_max_bytes: u64,
) -> Parsed {
    // seen = JSONL records, matching every other line-oriented adapter
    let tally = crate::intake::file(agent, path);
    parse_with_tally(
        agent,
        path,
        parent,
        gzip_compressed_max_bytes,
        gzip_decompressed_max_bytes,
        tally,
    )
}

fn parse_with_tally(
    agent: &'static str,
    path: &Path,
    parent: Option<String>,
    gzip_compressed_max_bytes: u64,
    gzip_decompressed_max_bytes: u64,
    tally: std::sync::Arc<crate::intake::Tally>,
) -> Parsed {
    let side = parent.is_some();
    let parent = parent.unwrap_or_default();
    let data = match read_session_with_limits(
        path,
        gzip_compressed_max_bytes,
        gzip_decompressed_max_bytes,
    ) {
        Ok(data) => data,
        Err(error) => {
            eprintln!(
                "  ! {agent}: cannot read {}: {}",
                crate::ingest::terminal_safe(path.display()),
                crate::ingest::terminal_safe(&error)
            );
            tally.seen();
            tally.error(&format!("cannot read: {error}"));
            return (
                Vec::new(),
                Vec::new(),
                crate::ingest_cache::ReadOutcome::Skipped,
            );
        }
    };
    let mut records: Vec<Record> = Vec::new();
    for (ordinal, line) in data.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<serde_json::Value>(line) {
            Ok(value) => records.push(Record { value, ordinal }),
            Err(error) => {
                tally.seen();
                tally.error(&format!("{error}: {}", crate::intake::clip(line, 80)));
            }
        }
    }
    let header = records
        .iter()
        .find(|record| record.value.get("type").and_then(|t| t.as_str()) == Some("session"));
    let session = header
        .and_then(|record| record.value.get("id").and_then(|i| i.as_str()))
        .map(str::to_string)
        .unwrap_or_else(|| {
            path.file_stem()
                .map(|stem| stem.to_string_lossy().to_string())
                .unwrap_or_default()
        });
    let project = header
        .and_then(|record| record.value.get("cwd").and_then(|c| c.as_str()))
        .filter(|cwd| !cwd.trim().is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| slug_project(path));

    let branch = active_branch(&records);
    let results = tool_results(&records, &branch);
    // Abandoned branches are still records in the file, so they are counted and
    // skipped rather than ignored: audit's raw census is a non-blank line count,
    // and it only certifies this store while `seen` accounts for every line.
    let on_branch: std::collections::HashSet<usize> = branch.iter().copied().collect();
    for index in 0..records.len() {
        if !on_branch.contains(&index) {
            tally.seen();
            tally.skip(crate::intake::Skip::Unreferenced);
        }
    }
    let mut out: Vec<crate::model::RawMessage> = Vec::new();
    let mut events: Vec<Event> = Vec::new();
    let mut recap_turns: Vec<u32> = Vec::new();
    let mut turn = 0u32;
    let mut model = String::new();
    let mut mirrored_anchor = false;
    for &index in &branch {
        let record = &records[index];
        tally.seen();
        let ts = parse_timestamp::rfc3339(record.value.get("timestamp").and_then(|t| t.as_str()));
        match record.value.get("type").and_then(|t| t.as_str()) {
            Some("session") => tally.skip(crate::intake::Skip::Meta),
            Some("model_change") => {
                // applies forward until the next switch, like the store replays it.
                // pi writes modelId; the oh-my-pi fork writes model.
                if let Some(named) = record
                    .value
                    .get("modelId")
                    .or_else(|| record.value.get("model"))
                    .and_then(|m| m.as_str())
                {
                    model = named.to_string();
                }
                tally.skip(crate::intake::Skip::Meta);
            }
            Some("compaction") => {
                let summary = record
                    .value
                    .get("summary")
                    .and_then(|s| s.as_str())
                    .unwrap_or_default();
                if summary.trim().is_empty() {
                    tally.skip(crate::intake::Skip::EmptyText);
                    continue;
                }
                tally.row();
                recap_turns.push(turn);
                out.push(crate::model::RawMessage {
                    agent,
                    project: project.clone(),
                    session: session.clone(),
                    ts,
                    turn,
                    text: summary.to_string(),
                    model: model.clone(),
                    reply: String::new(),
                    reply_chars: 0,
                    side,
                    parent: parent.clone(),
                });
                turn += 1;
            }
            Some("message") => {
                let Some(message) = record.value.get("message") else {
                    tally.skip(crate::intake::Skip::NonHuman);
                    continue;
                };
                let ts = parse_timestamp::rfc3339(
                    message
                        .get("timestamp")
                        .and_then(|t| t.as_str())
                        .or_else(|| record.value.get("timestamp").and_then(|t| t.as_str())),
                );
                if is_role(message, "user") {
                    // Sidecar streams (advisor and friends) mirror the watched session
                    // as user-role records flagged `synthetic: true`: transcript copies
                    // already indexed at their source, so sidechain, never prompts.
                    if message
                        .get("synthetic")
                        .and_then(|s| s.as_bool())
                        .unwrap_or(false)
                    {
                        mirrored_anchor = true;
                        tally.skip(crate::intake::Skip::Sidechain);
                        continue;
                    }
                    mirrored_anchor = false;
                    let text = text_of(message.get("content"));
                    if text.trim().is_empty() {
                        tally.skip(crate::intake::Skip::EmptyText);
                        continue;
                    }
                    if is_wrapper(&text) {
                        tally.skip(crate::intake::Skip::Wrapper);
                        continue;
                    }
                    tally.row();
                    out.push(crate::model::RawMessage {
                        agent,
                        project: project.clone(),
                        session: session.clone(),
                        ts,
                        turn,
                        text,
                        model: model.clone(),
                        reply: String::new(),
                        reply_chars: 0,
                        side,
                        parent: parent.clone(),
                    });
                    turn += 1;
                } else if is_role(message, "assistant") {
                    if let Some(named) = message.get("model").and_then(|m| m.as_str()) {
                        if !named.is_empty() {
                            model = named.to_string();
                        }
                    }
                    let reply = text_of(message.get("content"));
                    // A sidecar assistant whose anchor was skipped as a synthetic
                    // mirror has no prompt row to reply to: that text is the
                    // sidecar's own voice (advisor advisories) and becomes a row.
                    if side && mirrored_anchor {
                        if reply.trim().is_empty() {
                            tally.skip(crate::intake::Skip::EmptyText);
                        } else {
                            tally.row();
                            out.push(crate::model::RawMessage {
                                agent,
                                project: project.clone(),
                                session: session.clone(),
                                ts,
                                turn,
                                text: reply,
                                model: model.clone(),
                                reply: String::new(),
                                reply_chars: 0,
                                side,
                                parent: parent.clone(),
                            });
                            turn += 1;
                        }
                    } else {
                        if let Some(last) = out.last_mut() {
                            if !reply.trim().is_empty() {
                                let chars = crate::ingest::append_capped(
                                    &mut last.reply,
                                    &reply,
                                    crate::ingest::REPLY_CAP,
                                );
                                last.reply_chars += chars;
                            }
                            if last.model.is_empty() {
                                last.model = model.clone();
                            }
                        }
                        tally.agent_row();
                    }
                    for (call_ordinal, block) in message
                        .get("content")
                        .and_then(|c| c.as_array())
                        .map(Vec::as_slice)
                        .unwrap_or_default()
                        .iter()
                        .enumerate()
                    {
                        if block.get("type").and_then(|t| t.as_str()) != Some("toolCall") {
                            continue;
                        }
                        let name = block.get("name").and_then(|n| n.as_str()).unwrap_or("?");
                        let (input, input_chars) = block
                            .get("arguments")
                            .map(summarize_tool_input_with_chars)
                            .unwrap_or_default();
                        let call_id = block
                            .get("id")
                            .and_then(|i| i.as_str())
                            .filter(|id| !id.trim().is_empty())
                            .map(str::to_string)
                            .unwrap_or_else(|| {
                                format!("{agent}:{}:{call_ordinal}", record.ordinal)
                            });
                        let (raw_output, ok) = results.get(&call_id).cloned().unwrap_or_default();
                        let (output, output_chars, output_bytes) = cap_event_output(&raw_output);
                        tally.event();
                        events.push(Event {
                            agent,
                            session: session.clone(),
                            ts,
                            kind: "tool",
                            name: name.to_string(),
                            input,
                            output,
                            input_chars,
                            output_chars,
                            output_bytes,
                            ok,
                            call_id,
                            child_session: String::new(),
                        });
                    }
                } else if is_role(message, "bashExecution") {
                    // pi runs a bash command as its own role, not a toolCall:
                    // no content, the command and output are message fields.
                    let command = message
                        .get("command")
                        .and_then(|c| c.as_str())
                        .unwrap_or_default();
                    let (output, output_chars, output_bytes) = cap_event_output(
                        message
                            .get("output")
                            .and_then(|o| o.as_str())
                            .unwrap_or_default(),
                    );
                    let cancelled = message
                        .get("cancelled")
                        .and_then(|c| c.as_bool())
                        .unwrap_or(false);
                    let ok = message
                        .get("exitCode")
                        .and_then(serde_json::Value::as_i64)
                        .map(|code| code == 0 && !cancelled);
                    tally.event();
                    events.push(Event {
                        agent,
                        session: session.clone(),
                        ts,
                        kind: "tool",
                        name: "bash".to_string(),
                        input: command.to_string(),
                        output,
                        input_chars: command.chars().count(),
                        output_chars,
                        output_bytes,
                        ok,
                        call_id: record
                            .value
                            .get("id")
                            .and_then(|i| i.as_str())
                            .map(str::to_string)
                            .unwrap_or_else(|| format!("{agent}:bash:{}", record.ordinal)),
                        child_session: String::new(),
                    });
                    tally.agent_row();
                } else {
                    // toolResult rows were consumed above; developer/custom
                    // roles are the harness talking to itself, never the user
                    tally.skip(crate::intake::Skip::NonHuman);
                }
            }
            _ => tally.skip(crate::intake::Skip::Meta),
        }
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

fn side_parent_in(root: &Path, path: &Path) -> Option<String> {
    let relative = path.strip_prefix(root).ok()?;
    let mut parts = relative.components();
    let _slug = match parts.next()? {
        std::path::Component::Normal(value) => value,
        _ => return None,
    };
    let container = match parts.next()? {
        std::path::Component::Normal(value) => value.to_str()?,
        _ => return None,
    };
    parts.next()?;
    let parent = container
        .rsplit_once('_')
        .map_or(container, |(_, session)| session);
    (!parent.is_empty()).then(|| parent.to_string())
}

fn side_parent(path: &Path) -> Option<String> {
    PI_HOMES.iter().find_map(|home_dir| {
        side_parent_in(&sessions_root(home_dir), path)
            .or_else(|| side_parent_in(&archived_sessions_root(home_dir), path))
    })
}

fn parse_pi(path: &Path) -> Parsed {
    parse_with("pi", path, side_parent(path))
}

fn plain_dir(agent: &str, path: &Path) -> bool {
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.is_dir() && !metadata_is_link(&meta) => true,
        Ok(meta) if metadata_is_link(&meta) => {
            crate::ingest::warn_source_skip(agent, path, "symlink sources are not followed");
            false
        }
        Ok(_) => false,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
        Err(error) => {
            crate::ingest::warn_source_skip(agent, path, &error);
            false
        }
    }
}

fn is_session_file(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    name.ends_with(".jsonl") || name.ends_with(".jsonl.gz")
}

/// Session files under one cwd-slug directory. A slug also holds subdirectories
/// of sidecar sessions (the fork's advisor runs), so the walk goes one level
/// deeper - and session filenames are not always <timestamp>_<uuid>.
fn slug_files(agent: &str, slug: &Path, files: &mut Vec<PathBuf>) {
    let entries = match fs::read_dir(slug) {
        Ok(entries) => entries,
        Err(error) => {
            crate::ingest::warn_source_skip(agent, slug, &error);
            return;
        }
    };
    let mut nested: Vec<PathBuf> = Vec::new();
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                crate::ingest::warn_source_skip(agent, slug, &error);
                continue;
            }
        };
        let path = entry.path();
        match plain_entry_metadata(&entry) {
            Ok(Some(meta)) if meta.is_file() => {
                if is_session_file(&path) {
                    files.push(path);
                }
            }
            Ok(Some(meta)) if meta.is_dir() => nested.push(path),
            Ok(Some(_)) => {}
            Ok(None) => {
                crate::ingest::warn_source_skip(agent, &path, "symlink sources are not followed")
            }
            Err(error) => crate::ingest::warn_source_skip(agent, &path, &error),
        }
    }
    for child in nested {
        if plain_dir(agent, &child) {
            slug_files(agent, &child, files);
        }
    }
}

fn session_files(agent: &str, root: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = Vec::new();
    if !plain_dir(agent, root) {
        return files;
    }
    let slugs = match fs::read_dir(root) {
        Ok(slugs) => slugs,
        Err(error) => {
            crate::ingest::warn_source_skip(agent, root, &error);
            return files;
        }
    };
    for slug in slugs {
        let slug = match slug {
            Ok(slug) => slug,
            Err(error) => {
                crate::ingest::warn_source_skip(agent, root, &error);
                continue;
            }
        };
        let path = slug.path();
        match plain_entry_metadata(&slug) {
            Ok(Some(meta)) if meta.is_dir() => {
                if plain_dir(agent, &path) {
                    slug_files(agent, &path, &mut files);
                }
            }
            Ok(None) => {
                crate::ingest::warn_source_skip(agent, &path, "symlink sources are not followed")
            }
            Ok(Some(_)) => {}
            Err(error) => crate::ingest::warn_source_skip(agent, &path, &error),
        }
    }
    files
}

/// pi's own home and the oh-my-pi fork's, walked as one store.
const PI_HOMES: [&str; 2] = [".pi", ".omp"];

fn sessions_root(home_dir: &str) -> PathBuf {
    crate::ingest::home()
        .join(home_dir)
        .join("agent")
        .join("sessions")
}
fn archived_sessions_root(home_dir: &str) -> PathBuf {
    crate::ingest::home()
        .join(home_dir)
        .join("agent")
        .join("archive")
        .join("sessions")
}

fn collect_store(
    cache: &mut crate::ingest_cache::IngestCache,
    agent: &'static str,
    root: &Path,
    parse: fn(&Path) -> Parsed,
) -> (Vec<Message>, Vec<Event>) {
    let files = session_files(agent, root);
    let pass = crate::ingest_cache::collect_cached_for(cache, agent, root, &files, parse);
    (pass.messages, pass.events)
}

/// Registry entry (see ingest::registry). File store -> Stat.
///
/// One agent, two homes and each home's cold archive: oh-my-pi is a fork of the
/// same format, and the owner named both "pi", so every session lands under the
/// same label rather than a second entry nobody would think to filter by.
pub struct Pi;
impl crate::ingest::registry::Adapter for Pi {
    fn name(&self) -> &'static str {
        "pi"
    }
    fn fingerprint(&self) -> crate::ingest::registry::Fingerprint {
        crate::ingest::registry::Fingerprint::Stat
    }
    fn collect(&self, cache: &mut crate::ingest_cache::IngestCache) -> (Vec<Message>, Vec<Event>) {
        let mut messages = Vec::new();
        let mut events = Vec::new();
        for home_dir in PI_HOMES {
            for root in [sessions_root(home_dir), archived_sessions_root(home_dir)] {
                let (rows, found) = collect_store(cache, "pi", &root, parse_pi);
                messages.extend(rows);
                events.extend(found);
            }
        }
        (messages, events)
    }
    fn store_roots(&self) -> Vec<PathBuf> {
        PI_HOMES
            .iter()
            .flat_map(|home| [sessions_root(home), archived_sessions_root(home)])
            .collect()
    }
    fn store_content(&self, path: &Path) -> bool {
        is_session_file(path)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        active_branch, parse_with, parse_with_limits, parse_with_tally, session_files,
        side_parent_in, Record, GZIP_COMPRESSED_MAX_BYTES, GZIP_DECOMPRESSED_MAX_BYTES,
    };
    use std::io::Write;
    use std::path::PathBuf;

    fn records(lines: &[&str]) -> Vec<Record> {
        lines
            .iter()
            .enumerate()
            .map(|(ordinal, line)| Record {
                value: serde_json::from_str(line).unwrap(),
                ordinal,
            })
            .collect()
    }

    #[test]
    fn the_active_branch_drops_the_abandoned_one() {
        // b2 branches off a, then b3 branches off a again and is appended last:
        // the leaf is b3, so b2's subtree is not part of the conversation.
        let parsed = records(&[
            r#"{"type":"session","id":"s","version":3,"cwd":"/w"}"#,
            r#"{"type":"message","id":"a","parentId":null}"#,
            r#"{"type":"message","id":"b2","parentId":"a"}"#,
            r#"{"type":"message","id":"b3","parentId":"a"}"#,
        ]);
        let branch: Vec<&str> = active_branch(&parsed)
            .into_iter()
            .map(|index| parsed[index].value["id"].as_str().unwrap())
            .collect();
        assert_eq!(branch, vec!["a", "b3"]);
    }

    #[test]
    fn a_v1_file_without_ids_keeps_file_order() {
        let parsed = records(&[
            r#"{"type":"session","version":1,"cwd":"/w"}"#,
            r#"{"type":"message","message":{"role":"user","content":"one"}}"#,
        ]);
        assert_eq!(active_branch(&parsed), vec![0, 1]);
    }

    #[test]
    fn a_parent_cycle_terminates_instead_of_hanging() {
        let parsed = records(&[
            r#"{"type":"message","id":"a","parentId":"b"}"#,
            r#"{"type":"message","id":"b","parentId":"a"}"#,
        ]);
        assert_eq!(active_branch(&parsed).len(), 2);
    }

    fn write(body: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "agrep-pi-{}-{}.jsonl",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::write(&path, body).unwrap();
        path
    }

    #[test]
    fn a_compaction_summary_becomes_a_recap_row() {
        let path = write(concat!(
            r#"{"type":"session","id":"s1","version":3,"cwd":"/work/app","timestamp":"2026-01-02T03:04:05.000Z"}"#,
            "\n",
            r#"{"type":"message","id":"m1","parentId":null,"timestamp":"2026-01-02T03:04:06.000Z","message":{"role":"user","content":[{"type":"text","text":"first question"}]}}"#,
            "\n",
            r#"{"type":"compaction","id":"c1","parentId":"m1","timestamp":"2026-01-02T03:04:07.000Z","summary":"earlier context recap","tokensBefore":50000}"#,
            "\n",
        ));
        let (messages, _events, _outcome) = parse_with("pi", &path, None);
        let _ = std::fs::remove_file(&path);
        assert_eq!(messages.len(), 2);
        assert_eq!(&*messages[0].who, "user");
        assert_eq!(&*messages[1].who, "recap");
        assert_eq!(&*messages[1].text, "earlier context recap");
        assert_eq!(&*messages[1].project, "/work/app");
        assert_eq!(&*messages[1].session, "s1");
    }

    #[test]
    fn a_tool_call_pairs_with_the_result_that_answers_it() {
        let path = write(concat!(
            r#"{"type":"session","id":"s2","version":3,"cwd":"/work/app"}"#,
            "\n",
            r#"{"type":"message","id":"m1","parentId":null,"timestamp":"2026-01-02T03:04:06.000Z","message":{"role":"user","content":[{"type":"text","text":"run it"}]}}"#,
            "\n",
            r#"{"type":"message","id":"m2","parentId":"m1","timestamp":"2026-01-02T03:04:07.000Z","message":{"role":"assistant","model":"pi-1","content":[{"type":"thinking","thinking":"hidden"},{"type":"text","text":"running"},{"type":"toolCall","id":"tc1","name":"bash","arguments":{"command":"ls"}}]}}"#,
            "\n",
            r#"{"type":"message","id":"m3","parentId":"m2","timestamp":"2026-01-02T03:04:08.000Z","message":{"role":"toolResult","toolCallId":"tc1","toolName":"bash","isError":false,"content":[{"type":"text","text":"a.txt"}]}}"#,
            "\n",
        ));
        let (messages, events, _outcome) = parse_with("pi", &path, None);
        let _ = std::fs::remove_file(&path);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].reply, "running");
        assert!(!messages[0].reply.contains("hidden"), "reasoning leaked");
        assert_eq!(&*messages[0].model, "pi-1");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "bash");
        assert_eq!(events[0].output, "a.txt");
        assert_eq!(events[0].ok, Some(true));
    }

    #[test]
    fn a_v3_custom_record_is_not_a_conversation_turn() {
        let path = write(concat!(
            r#"{"type":"session","id":"s3","version":3,"cwd":"/work/app"}"#,
            "\n",
            r#"{"type":"custom","id":"x1","parentId":null,"customType":"ext.state","data":{"k":1}}"#,
            "\n",
            r#"{"type":"message","id":"m1","parentId":"x1","timestamp":"2026-01-02T03:04:06.000Z","message":{"role":"user","content":[{"type":"text","text":"only turn"}]}}"#,
            "\n",
        ));
        let (messages, _events, _outcome) = parse_with("pi", &path, None);
        let _ = std::fs::remove_file(&path);
        assert_eq!(messages.len(), 1);
        assert_eq!(&*messages[0].text, "only turn");
    }

    #[test]
    fn a_synthetic_transcript_mirror_is_not_a_user_row() {
        // The mirror (m2) sits on the active branch between prompt and reply;
        // m1x is an abandoned sibling of m1, counted-and-skipped, never
        // re-walked. Six records may yield exactly one user row plus one recap.
        let path = write(concat!(
            r#"{"type":"session","id":"s4","version":3,"cwd":"/work/app"}"#,
            "\n",
            r#"{"type":"message","id":"m1","parentId":null,"timestamp":"2026-01-02T03:04:06.000Z","message":{"role":"user","attribution":"user","content":[{"type":"text","text":"real question"}]}}"#,
            "\n",
            r#"{"type":"message","id":"m1x","parentId":null,"timestamp":"2026-01-02T03:04:06.500Z","message":{"role":"user","attribution":"user","content":[{"type":"text","text":"abandoned branch"}]}}"#,
            "\n",
            r####"{"type":"message","id":"m2","parentId":"m1","timestamp":"2026-01-02T03:04:07.000Z","message":{"role":"user","synthetic":true,"attribution":"agent","content":[{"type":"text","text":"### Session update\n\n**user**:\nreal question\n"}]}}"####,
            "\n",
            r#"{"type":"message","id":"m3","parentId":"m2","timestamp":"2026-01-02T03:04:08.000Z","message":{"role":"assistant","model":"pi-1","content":[{"type":"text","text":"an answer"}]}}"#,
            "\n",
            r#"{"type":"compaction","id":"c1","parentId":"m3","timestamp":"2026-01-02T03:04:09.000Z","summary":"recap of earlier context"}"#,
            "\n",
        ));
        let (messages, _events, _outcome) = parse_with("pi", &path, None);
        let _ = std::fs::remove_file(&path);
        assert_eq!(messages.len(), 2, "one genuine prompt plus one recap");
        assert_eq!(&*messages[0].who, "user");
        assert_eq!(&*messages[0].text, "real question");
        // the reply lands on the genuine row, not on a mirror row
        assert_eq!(&*messages[0].reply, "an answer");
        assert_eq!(&*messages[1].who, "recap");
        assert!(
            !messages.iter().any(|m| m.text.contains("Session update")),
            "a synthetic mirror leaked into the user lane"
        );
    }

    #[test]
    fn an_advisor_sidecar_indexes_its_own_voice_but_never_the_mirrors() {
        // A sidecar's user side is all synthetic mirrors, so its assistant records
        // are anchorless: text-bearing ones become the sidecar's own rows, pure-
        // thinking ones emit nothing, and tool events survive as events.
        let root = std::env::temp_dir().join(format!(
            "agrep-omp-advisor-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let sessions = root.join("agent").join("sessions");
        let container = "2026-08-18T03-34-43-541Z_01a012ef-8a55-7000-b9ca-8ab44ce2a383";
        let child = sessions
            .join("project")
            .join(container)
            .join("__advisor.jsonl");
        std::fs::create_dir_all(child.parent().unwrap()).unwrap();
        std::fs::write(
            &child,
            concat!(
                r#"{"type":"session","id":"advisor","version":3,"cwd":"/work/app"}"#,
                "\n",
                r####"{"type":"message","id":"m1","parentId":null,"message":{"role":"user","synthetic":true,"attribution":"agent","content":[{"type":"text","text":"### Session update\n\n**user**:\nhi\n"}]}}"####,
                "\n",
                r####"{"type":"message","id":"m2","parentId":"m1","message":{"role":"user","synthetic":true,"attribution":"agent","content":[{"type":"text","text":"### Session update\n\n**agent**:\nhello\n"}]}}"####,
                "\n",
                r#"{"type":"message","id":"m3","parentId":"m2","message":{"role":"assistant","model":"pi-1","content":[{"type":"text","text":"advice"},{"type":"toolCall","id":"tc1","name":"read","arguments":{"path":"a.rs"}}]}}"#,
                "\n",
                r#"{"type":"message","id":"m4","parentId":"m3","message":{"role":"toolResult","toolCallId":"tc1","content":[{"type":"text","text":"fn main() {}"}]}}"#,
                "\n",
                r#"{"type":"message","id":"m5","parentId":"m4","message":{"role":"assistant","content":[{"type":"thinking","thinking":"weighing options"}]}}"#,
                "\n",
                r####"{"type":"message","id":"m6","parentId":"m5","message":{"role":"user","synthetic":true,"attribution":"agent","content":[{"type":"text","text":"### Session update\n\n**agent**:\nmore\n"}]}}"####,
                "\n",
                r#"{"type":"message","id":"m7","parentId":"m6","message":{"role":"assistant","content":[{"type":"text","text":"ship the advisory"}]}}"#,
                "\n",
            ),
        )
        .unwrap();
        let parent = side_parent_in(&sessions, &child);
        assert_eq!(parent.as_deref(), Some("01a012ef-8a55-7000-b9ca-8ab44ce2a383"));
        let tally = std::sync::Arc::new(crate::intake::Tally::default());
        let (messages, events, outcome) = parse_with_tally(
            "pi",
            &child,
            parent,
            GZIP_COMPRESSED_MAX_BYTES,
            GZIP_DECOMPRESSED_MAX_BYTES,
            std::sync::Arc::clone(&tally),
        );
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(outcome, crate::ingest_cache::ReadOutcome::Complete);
        let texts: Vec<&str> = messages.iter().map(|m| &*m.text).collect();
        assert_eq!(texts, vec!["advice", "ship the advisory"]);
        for (expected_turn, message) in messages.iter().enumerate() {
            assert!(message.side);
            assert_eq!(&*message.parent, "01a012ef-8a55-7000-b9ca-8ab44ce2a383");
            assert_eq!(&*message.who, "user");
            assert_eq!(message.turn, expected_turn as u32);
            assert!(message.reply.is_empty());
        }
        assert_eq!(&*messages[0].model, "pi-1");
        assert!(
            !texts.iter().any(|t| t.contains("Session update")),
            "a synthetic mirror leaked into the row lane"
        );
        assert!(
            !texts.iter().any(|t| t.contains("weighing options")),
            "reasoning leaked"
        );
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "read");
        assert_eq!(events[0].output, "fn main() {}");
        // seen == the 8 physical lines; 3 mirrors sidechain, 1 pure-thinking
        // assistant empty-text, header meta, toolResult non-human, 2 rows.
        assert_eq!(
            tally.test_record_counts(crate::intake::Skip::Sidechain),
            (8, 2, 0, 3, 0)
        );
        assert_eq!(
            tally.test_record_counts(crate::intake::Skip::EmptyText),
            (8, 2, 0, 1, 0)
        );
    }

    #[test]
    fn nested_omp_sessions_link_to_their_root_chat() {
        let root = std::env::temp_dir().join(format!(
            "agrep-omp-side-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let sessions = root.join("agent").join("sessions");
        let container = "2026-08-09T20-59-21-133Z_019fe852-b0ad-7000-b68f-f054af8dff14";
        let child = sessions
            .join("project")
            .join(container)
            .join("worker.jsonl");
        std::fs::create_dir_all(child.parent().unwrap()).unwrap();
        std::fs::write(
            &child,
            concat!(
                r#"{"type":"session","id":"child","version":3,"cwd":"/work/app"}"#,
                "\n",
                r#"{"type":"message","id":"m1","parentId":null,"message":{"role":"user","content":"task"}}"#,
                "\n",
            ),
        )
        .unwrap();
        let parent = side_parent_in(&sessions, &child);
        let (messages, _events, _outcome) = parse_with("pi", &child, parent);
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(messages.len(), 1);
        assert!(messages[0].side);
        assert_eq!(&*messages[0].parent, "019fe852-b0ad-7000-b68f-f054af8dff14");
    }
    #[test]
    fn cold_archive_sessions_and_nested_sidecars_remain_discoverable() {
        let root = std::env::temp_dir().join(format!(
            "agrep-omp-archive-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let archive = root.join("agent").join("archive").join("sessions");
        let container = "2026-07-01T10-00-00-000Z_archived-root";
        let main = archive
            .join("project")
            .join(format!("{container}.jsonl.gz"));
        let child = archive.join("project").join(container).join("worker.jsonl");
        std::fs::create_dir_all(child.parent().unwrap()).unwrap();
        let body = concat!(
            r#"{"type":"session","id":"archived-root","version":3,"cwd":"/work/archive"}"#,
            "\n",
            r#"{"type":"message","id":"m1","parentId":null,"message":{"role":"user","content":"remember this archive fact"}}"#,
            "\n",
        );
        let mut encoder = flate2::write::GzEncoder::new(
            std::fs::File::create(&main).unwrap(),
            flate2::Compression::default(),
        );
        encoder.write_all(body.as_bytes()).unwrap();
        encoder.finish().unwrap();
        std::fs::write(
            &child,
            concat!(
                r#"{"type":"session","id":"archived-child","version":3,"cwd":"/work/archive"}"#,
                "\n",
                r#"{"type":"message","id":"m1","parentId":null,"message":{"role":"user","content":"archived side task"}}"#,
                "\n",
            ),
        )
        .unwrap();

        let files = session_files("pi", &archive);
        assert!(files.contains(&main));
        assert!(files.contains(&child));
        let (messages, _events, outcome) = parse_with("pi", &main, None);
        let parent = side_parent_in(&archive, &child);
        let (side_messages, _events, side_outcome) = parse_with("pi", &child, parent);
        let _ = std::fs::remove_dir_all(&root);

        assert_eq!(outcome, crate::ingest_cache::ReadOutcome::Complete);
        assert_eq!(&*messages[0].text, "remember this archive fact");
        assert_eq!(&*messages[0].session, "archived-root");
        assert_eq!(side_outcome, crate::ingest_cache::ReadOutcome::Complete);
        assert!(side_messages[0].side);
        assert_eq!(&*side_messages[0].parent, "archived-root");
    }

    #[cfg(unix)]
    #[test]
    fn an_archive_slug_symlink_is_not_ingested() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!(
            "agrep-omp-archive-link-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let outside = root.with_extension("outside");
        let archive = root.join("agent").join("archive").join("sessions");
        let safe = archive.join("safe").join("safe.jsonl");
        let escaped = outside.join("escaped.jsonl");
        std::fs::create_dir_all(safe.parent().unwrap()).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(&safe, "{}\n").unwrap();
        std::fs::write(&escaped, "{}\n").unwrap();
        symlink(&outside, archive.join("linked-slug")).unwrap();

        let files = session_files("pi", &archive);
        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(&outside);

        assert_eq!(files, vec![safe]);
    }

    #[test]
    fn gzip_expansion_overflow_is_skipped_and_cannot_replace_cached_rows() {
        const COMPRESSED_LIMIT: u64 = 1024;
        const DECOMPRESSED_LIMIT: u64 = 512;

        let root = std::env::temp_dir().join(format!(
            "agrep-omp-gzip-limit-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path = root.join("project").join("session.jsonl.gz");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let write_gzip = |body: &str| {
            let mut encoder = flate2::write::GzEncoder::new(
                std::fs::File::create(&path).unwrap(),
                flate2::Compression::default(),
            );
            encoder.write_all(body.as_bytes()).unwrap();
            encoder.finish().unwrap();
        };
        write_gzip(concat!(
            r#"{"type":"session","id":"good","version":3,"cwd":"/work/archive"}"#,
            "\n",
            r#"{"type":"message","id":"m1","parentId":null,"message":{"role":"user","content":"last known good"}}"#,
            "\n",
        ));

        let mut cache = crate::ingest_cache::IngestCache::cold();
        let first = crate::ingest_cache::collect_cached_for(
            &mut cache,
            "pi",
            &root,
            std::slice::from_ref(&path),
            |path| parse_with_limits("pi", path, None, COMPRESSED_LIMIT, DECOMPRESSED_LIMIT),
        );
        assert_eq!(first.messages.len(), 1);
        assert_eq!(&*first.messages[0].text, "last known good");
        assert!(cache.source_snapshot_safe());

        let replacement = format!(
            concat!(
                r#"{{"type":"session","id":"replacement","version":3,"cwd":"/work/archive"}}"#,
                "\n",
                r#"{{"type":"message","id":"m2","parentId":null,"message":{{"role":"user","content":"unsafe replacement"}}}}"#,
                "\n{}"
            ),
            " ".repeat(DECOMPRESSED_LIMIT as usize)
        );
        assert!(replacement.len() as u64 > DECOMPRESSED_LIMIT);
        write_gzip(&replacement);
        assert!(std::fs::metadata(&path).unwrap().len() <= COMPRESSED_LIMIT);

        let (messages, events, outcome) =
            parse_with_limits("pi", &path, None, COMPRESSED_LIMIT, DECOMPRESSED_LIMIT);
        assert!(messages.is_empty());
        assert!(events.is_empty());
        assert_eq!(outcome, crate::ingest_cache::ReadOutcome::Skipped);

        let guarded = crate::ingest_cache::collect_cached_for(
            &mut cache,
            "pi",
            &root,
            std::slice::from_ref(&path),
            |path| parse_with_limits("pi", path, None, COMPRESSED_LIMIT, DECOMPRESSED_LIMIT),
        );
        let _ = std::fs::remove_dir_all(&root);

        assert_eq!(guarded.messages.len(), 1);
        assert_eq!(&*guarded.messages[0].text, "last known good");
        assert!(!cache.source_snapshot_safe());
    }
}
