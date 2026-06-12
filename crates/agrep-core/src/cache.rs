//! The downstream contract between tier-0 ingest and the semantic sidecar + search index.
//!
//! `write_messages` serializes normalized [`Message`]s to JSON Lines (one compact object per
//! line) at `data/messages.jsonl`. The Python sidecar reads this file, computes the authoritative
//! affect read, and writes back; the search index is built from the same rows. Each record carries
//! a stable `id` (`agent:session:turn`) so the sidecar can join its results back onto the source.

use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::Path;

use serde::Serialize;

use crate::model::{Event, Message};

/// Write a file atomically: stream into `<path>.tmp`, flush, then rename over `path`.
/// Rename is atomic within a filesystem on both Windows and Unix, so a concurrent reader
/// (the server's auto-indexer reindexes while requests are served) always sees either the
/// complete old file or the complete new one — never a half-written one.
fn write_atomic<F>(path: &Path, f: F) -> anyhow::Result<()>
where
    F: FnOnce(&mut BufWriter<fs::File>) -> anyhow::Result<()>,
{
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let tmp = path.with_extension(format!(
        "{}tmp",
        path.extension().map(|e| format!("{}.", e.to_string_lossy()))
            .unwrap_or_default()
    ));
    {
        let mut w = BufWriter::new(fs::File::create(&tmp)?);
        f(&mut w)?;
        w.flush()?;
    }
    // On Windows, rename fails if the destination exists; remove it first. The tiny
    // window between remove and rename is acceptable for a single-writer indexer.
    #[cfg(windows)]
    let _ = fs::remove_file(path);
    fs::rename(&tmp, path)?;
    Ok(())
}

/// One cached row. `id` is the stable join key the sidecar/index reference.
#[derive(Serialize)]
struct Record<'a> {
    id: String,
    agent: &'a str,
    project: &'a str,
    session: &'a str,
    ts: i64,
    turn: u32,
    text: &'a str,
    /// Model on the agent's side of this turn ("" when unknown). Tiny, so it rides along
    /// in the hot file; the bulky reply text goes to the replies sidecar instead.
    #[serde(skip_serializing_if = "str::is_empty")]
    model: &'a str,
}

impl<'a> Record<'a> {
    fn from_message(m: &'a Message) -> Self {
        Record {
            id: format!("{}:{}:{}", m.agent, m.session, m.turn),
            agent: m.agent,
            project: &m.project,
            session: &m.session,
            ts: m.ts,
            turn: m.turn,
            text: &m.text,
            model: &m.model,
        }
    }
}

/// One reply-sidecar row: `{id, reply}`. Kept out of `messages.jsonl` so the embed/affect
/// streaming reads stay lean; only the chat-detail view joins these back in.
#[derive(Serialize)]
struct ReplyRecord<'a> {
    id: String,
    reply: &'a str,
}

/// Write `msgs` as JSON Lines to `path` (creating parent dirs as needed). One compact JSON object
/// per line: `{id, agent, project, session, ts, turn, text}`. Overwrites any existing file.
pub fn write_messages(msgs: &[Message], path: &Path) -> anyhow::Result<()> {
    write_atomic(path, |w| {
        for m in msgs {
            let rec = Record::from_message(m);
            // Compact (no pretty-print) so each record is exactly one line.
            serde_json::to_writer(&mut *w, &rec)?;
            w.write_all(b"\n")?;
        }
        Ok(())
    })
}

/// Write the agent replies as JSON Lines (`{id, reply}`) to `path`, skipping turns with no
/// captured reply. Same stable `id` as `messages.jsonl`, so the detail view joins on it.
pub fn write_replies(msgs: &[Message], path: &Path) -> anyhow::Result<()> {
    write_atomic(path, |w| {
        for m in msgs {
            if m.reply.trim().is_empty() {
                continue;
            }
            let rec = ReplyRecord {
                id: format!("{}:{}:{}", m.agent, m.session, m.turn),
                reply: &m.reply,
            };
            serde_json::to_writer(&mut *w, &rec)?;
            w.write_all(b"\n")?;
        }
        Ok(())
    })
}

/// One row per session in `data/sessions.jsonl`: everything the explorer's rail needs
/// (counts, span, label fallback) WITHOUT touching the ~50 MB messages.jsonl. This is
/// what makes the directory near-instant on a cold server and right after a reindex.
#[derive(Serialize)]
struct SessionRecord<'a> {
    session: &'a str,
    agent: &'a str,
    project: &'a str,
    n: u32,
    first_ts: i64,
    last_ts: i64,
    /// First real typed message (compaction recaps skipped), one line, capped.
    first_text: String,
}

const RECAP_PREFIX: &str = "This session is being continued from a previous conversation";

/// Write the per-session aggregate index. One pass over the already-deduped messages.
pub fn write_session_index(msgs: &[Message], path: &Path) -> anyhow::Result<usize> {
    struct Agg<'a> {
        agent: &'a str,
        project: &'a str,
        n: u32,
        first_ts: i64,
        last_ts: i64,
        first_text: String,
    }
    let mut by: BTreeMap<&str, Agg> = BTreeMap::new();
    for m in msgs {
        let a = by.entry(&*m.session).or_insert(Agg {
            agent: m.agent,
            project: &m.project,
            n: 0,
            first_ts: i64::MAX,
            last_ts: 0,
            first_text: String::new(),
        });
        a.n += 1;
        if m.ts > 0 {
            a.first_ts = a.first_ts.min(m.ts);
            a.last_ts = a.last_ts.max(m.ts);
        }
        if a.first_text.is_empty() && !m.text.trim().is_empty() && !m.text.starts_with(RECAP_PREFIX)
        {
            let one_line: String = m.text.split_whitespace().collect::<Vec<_>>().join(" ");
            a.first_text = one_line.chars().take(120).collect();
        }
    }
    let n = by.len();
    write_atomic(path, |w| {
        for (session, a) in by {
            let rec = SessionRecord {
                session,
                agent: a.agent,
                project: a.project,
                n: a.n,
                first_ts: if a.first_ts == i64::MAX { 0 } else { a.first_ts },
                last_ts: a.last_ts,
                first_text: a.first_text,
            };
            serde_json::to_writer(&mut *w, &rec)?;
            w.write_all(b"\n")?;
        }
        Ok(())
    })?;
    Ok(n)
}

/// One event row inside a per-session file. The file name already carries agent+session,
/// so rows hold only what varies per event; empty fields are omitted to keep lines lean.
#[derive(Serialize)]
struct EventRecord<'a> {
    ts: i64,
    kind: &'a str,
    name: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    input: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    output: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    ok: Option<bool>,
    #[serde(skip_serializing_if = "str::is_empty")]
    call_id: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    child: &'a str,
}

/// Session ids are uuids/`ses_*` in practice, but never trust them as raw file names.
fn safe_name(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// FNV-1a 64-bit of a byte slice. Used to detect whether a per-session event file's
/// content changed since the last index, so unchanged files are skipped (see below).
fn content_hash(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

/// Write events as per-session JSON Lines files `dir/{agent}-{session}.jsonl`, each
/// sorted by ts (stable, so same-ts events keep ingest order).
///
/// INCREMENTAL on two axes:
///  - content: each session's events are built in memory and hashed; a manifest
///    (`.manifest`, fname -> hash) records the last-written hash and a file is rewritten
///    only when its content changed or it's missing. (Rewriting all ~12k files every run
///    was 40s+ of Windows tmp+rename churn over 440MB.)
///  - coverage: `events` may cover only the sessions touched this run (the parse cache hands
///    back events for changed sessions only). `keep` is the full set of live session fnames;
///    unchanged sessions not in `events` keep their files AND their manifest hash (carried
///    forward), and only files whose fname is absent from `keep` are deleted. Agent logs are
///    append-only, so a re-parsed session always carries its complete event set.
///
/// `agents` scopes the DELETE pass: `keep` is built from this run's messages, so on a
/// single-agent run it contains only that agent's sessions — an unscoped sweep would
/// silently delete every other agent's event files (it did). Only files whose
/// `{agent}-` prefix belongs to an adapter actually ingested this run are eligible.
///
/// Returns (n_files, n_events_written, n_rewritten).
pub fn write_events(
    events: &[Event],
    dir: &Path,
    keep: &HashSet<String>,
    agents: &[&str],
) -> anyhow::Result<(usize, usize, usize)> {
    fs::create_dir_all(dir)?;
    let manifest_path = dir.join(".manifest");
    let manifest: std::collections::HashMap<String, u64> = fs::read_to_string(&manifest_path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();

    let mut by: BTreeMap<(&str, &str), Vec<&Event>> = BTreeMap::new();
    for e in events {
        by.entry((e.agent, e.session.as_str())).or_default().push(e);
    }

    // carry forward the hashes of sessions still live but not touched this run
    let mut next_manifest: std::collections::HashMap<String, u64> =
        manifest.iter().filter(|(k, _)| keep.contains(*k)).map(|(k, v)| (k.clone(), *v)).collect();
    let mut n_events = 0usize;
    let mut n_written = 0usize;
    for ((agent, session), mut evs) in by {
        if session.is_empty() {
            continue;
        }
        evs.sort_by_key(|e| e.ts);
        let fname = format!("{}-{}.jsonl", agent, safe_name(session));
        let mut buf: Vec<u8> = Vec::new();
        for e in &evs {
            let rec = EventRecord {
                ts: e.ts,
                kind: e.kind,
                name: &e.name,
                input: &e.input,
                output: &e.output,
                ok: e.ok,
                call_id: &e.call_id,
                child: &e.child_session,
            };
            serde_json::to_writer(&mut buf, &rec)?;
            buf.push(b'\n');
        }
        let h = content_hash(&buf);
        let path = dir.join(&fname);
        if manifest.get(&fname) != Some(&h) || !path.exists() {
            write_atomic(&path, |w| {
                w.write_all(&buf)?;
                Ok(())
            })?;
            n_written += 1;
        }
        next_manifest.insert(fname.clone(), h);
        n_events += evs.len();
    }

    // Drop event files (and manifest entries) whose session is no longer live — but only
    // for the agents this run actually ingested (see the doc comment: an unscoped sweep
    // on a single-agent run deletes every other agent's files).
    if let Ok(rd) = fs::read_dir(dir) {
        for entry in rd.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            let ours = agents.iter().any(|a| name.starts_with(&format!("{a}-")));
            if name.ends_with(".jsonl") && ours && !keep.contains(&name) {
                fs::remove_file(entry.path()).ok();
                next_manifest.remove(&name);
            }
        }
    }

    let n_files = next_manifest.len();
    let body = serde_json::to_string(&next_manifest)?;
    write_atomic(&manifest_path, |w| {
        w.write_all(body.as_bytes())?;
        Ok(())
    })?;
    Ok((n_files, n_events, n_written))
}

/// The per-session event filename for a session (agent + sanitized id). Lets the caller build
/// the `keep` set passed to [`write_events`].
pub fn event_fname(agent: &str, session: &str) -> String {
    format!("{}-{}.jsonl", agent, safe_name(session))
}

/// Aggregate rollup for the pulse dashboard: per-agent call/fail counts, the tool mix,
/// and subagent totals. Computed here (the events are already in memory at index time)
/// so the dashboard never has to scan the ~0.5GB per-session event corpus per request.
pub fn write_event_stats(events: &[Event], path: &Path) -> anyhow::Result<()> {
    use std::collections::HashMap;

    #[derive(Default, Serialize)]
    struct AgentStat {
        calls: u64,
        fails: u64,
        /// Calls whose store actually RECORDED an outcome (ok true or false). Codex logs
        /// raw output text with no exit status, so its fail rate is "not recorded", not
        /// zero -- the dashboard derives rate from fails/known and hides unknowable rows.
        known: u64,
        subagents: u64,
    }
    let mut by_agent: HashMap<&str, AgentStat> = HashMap::new();
    // names borrow from `events`: this loop covers the full corpus on complete runs
    let mut by_tool: HashMap<&str, (u64, u64)> = HashMap::new(); // name -> (n, fails)
    let mut total = 0u64;
    let mut fails = 0u64;
    let mut subagents = 0u64;
    for e in events {
        let a = by_agent.entry(e.agent).or_default();
        if e.kind == "tool" {
            total += 1;
            a.calls += 1;
            let t = by_tool.entry(&e.name).or_default();
            t.0 += 1;
            if e.ok.is_some() {
                a.known += 1;
            }
            if e.ok == Some(false) {
                fails += 1;
                a.fails += 1;
                t.1 += 1;
            }
        } else {
            subagents += 1;
            a.subagents += 1;
        }
    }
    let mut tools: Vec<(&str, (u64, u64))> = by_tool.into_iter().collect();
    tools.sort_by(|a, b| b.1 .0.cmp(&a.1 .0));
    tools.truncate(14);

    #[derive(Serialize)]
    struct Stats<'a> {
        total: u64,
        fails: u64,
        subagents: u64,
        by_agent: HashMap<&'a str, AgentStat>,
        by_tool: Vec<ToolStat<'a>>,
    }
    #[derive(Serialize)]
    struct ToolStat<'a> {
        name: &'a str,
        n: u64,
        fails: u64,
    }
    let stats = Stats {
        total,
        fails,
        subagents,
        by_agent,
        by_tool: tools
            .into_iter()
            .map(|(name, (n, f))| ToolStat { name, n, fails: f })
            .collect(),
    };
    let body = serde_json::to_string_pretty(&stats)?;
    write_atomic(path, |w| {
        w.write_all(body.as_bytes())?;
        Ok(())
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_one_compact_line_per_message() {
        let msgs = vec![
            Message {
                agent: "claude",
                project: "myproj".into(),
                session: "sess-1".into(),
                ts: 1_700_000_000_000,
                turn: 0,
                text: "first".into(),
                model: "claude-opus-4-8".into(),
                reply: "an answer".into(),
            },
            Message {
                agent: "opencode",
                project: "myproj".into(),
                session: "sess-2".into(),
                ts: 0,
                turn: 7,
                text: "with \"quotes\" and \n newline".into(),
                model: "".into(),
                reply: "".into(),
            },
        ];
        let mut path = std::env::temp_dir();
        path.push(format!("agrep-cache-test-{}.jsonl", std::process::id()));
        write_messages(&msgs, &path).unwrap();

        let data = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = data.lines().collect();
        assert_eq!(lines.len(), 2);

        let v0: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(v0["id"], "claude:sess-1:0");
        assert_eq!(v0["agent"], "claude");
        assert_eq!(v0["project"], "myproj");
        assert_eq!(v0["session"], "sess-1");
        assert_eq!(v0["ts"], 1_700_000_000_000i64);
        assert_eq!(v0["turn"], 0);
        assert_eq!(v0["text"], "first");

        let v1: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(v1["id"], "opencode:sess-2:7");
        assert_eq!(v1["text"], "with \"quotes\" and \n newline");

        std::fs::remove_file(&path).ok();
    }
}
