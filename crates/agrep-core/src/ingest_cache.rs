//! Per-source-file parse cache: skip re-parsing files whose `(mtime, size)` are unchanged
//! since the last index. Parsing the ~9GB of source took ~11s every run even when almost
//! nothing changed; this caches each file's parsed MESSAGES so a typical run re-parses only
//! the handful of files that actually moved.
//!
//! Only messages are cached (small). EVENTS are not: their per-session files already persist
//! on disk and are skipped by content-hash in `cache::write_events`, so re-deriving them for
//! unchanged sessions would be pure waste (it was - caching events made a run SLOWER than a
//! full parse). We therefore return events only for the sessions touched this run.
//!
//! SAFETY: the downstream dedup is by `(agent, session, turn)` / `(agent, session, call_id)`,
//! where a repeated tuple is byte-identical - so the result is order-independent and the cache
//! is transparent. claude/codex are one-file-per-session in practice; for any session that DOES
//! span files, its unchanged sibling files are re-parsed too so its event file stays complete.
//! A schema bump (`CACHE_VERSION`), a missing/corrupt cache, or `--full` fall back to a clean
//! full parse.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use rayon::prelude::*;
use serde::{Deserialize, Serialize};

use crate::model::{Event, Message};

/// Bump when the cached struct layout changes; old caches are then ignored (full reparse).
const CACHE_VERSION: u32 = 3;

#[derive(Serialize, Deserialize, Clone)]
struct CMsg {
    agent: String,
    project: std::sync::Arc<str>,
    session: std::sync::Arc<str>,
    ts: i64,
    turn: u32,
    text: std::sync::Arc<str>,
    who: std::sync::Arc<str>,
    model: std::sync::Arc<str>,
    model_source: std::sync::Arc<str>,
    reply: std::sync::Arc<str>,
}
#[derive(Serialize, Deserialize, Clone)]
struct Entry {
    mtime: i64,
    size: u64,
    msgs: Vec<CMsg>,
}

#[derive(Deserialize, Default)]
struct CacheFile {
    version: u32,
    entries: HashMap<String, Entry>,
}
#[derive(Serialize)]
struct CacheFileRef<'a> {
    version: u32,
    entries: &'a HashMap<String, Entry>,
}

fn intern_agent(s: &str) -> &'static str {
    match s {
        "claude" => "claude",
        "codex" => "codex",
        "opencode" => "opencode",
        "antigravity" => "antigravity",
        "kimi" => "kimi",
        "cline" => "cline",
        "gemini" => "gemini",
        _ => "unknown",
    }
}

impl CMsg {
    // both directions are refcount bumps (Arc fields), not string copies - on a warm
    // run every cached message round-trips through here, so this is the hot edge
    fn from(m: &Message) -> Self {
        CMsg {
            agent: m.agent.to_string(),
            project: m.project.clone(),
            session: m.session.clone(),
            ts: m.ts,
            turn: m.turn,
            text: m.text.clone(),
            who: m.who.clone(),
            model: m.model.clone(),
            model_source: m.model_source.clone(),
            reply: m.reply.clone(),
        }
    }
    fn to_msg(&self) -> Message {
        Message {
            agent: intern_agent(&self.agent),
            project: self.project.clone(),
            session: self.session.clone(),
            ts: self.ts,
            turn: self.turn,
            text: self.text.clone(),
            who: self.who.clone(),
            model: self.model.clone(),
            model_source: self.model_source.clone(),
            reply: self.reply.clone(),
        }
    }
}

pub struct IngestCache {
    entries: HashMap<String, Entry>,
    /// true when a valid cache was loaded - i.e. this run is INCREMENTAL (only changed files
    /// re-parsed, so the returned events cover only touched sessions). false on a cold/forced
    /// load means every file is parsed and the event set is complete.
    pub warm: bool,
}

impl IngestCache {
    /// Load the cache, or an empty one on absence / corruption / version mismatch.
    pub fn load(path: &Path) -> Self {
        match fs::read(path)
            .ok()
            .and_then(|b| bincode::deserialize::<CacheFile>(&b).ok())
            .filter(|c| c.version == CACHE_VERSION)
        {
            Some(c) => IngestCache {
                entries: c.entries,
                warm: true,
            },
            None => IngestCache {
                entries: HashMap::new(),
                warm: false,
            },
        }
    }

    /// An empty cache that treats every file as changed (used for `--full`).
    pub fn cold() -> Self {
        IngestCache {
            entries: HashMap::new(),
            warm: false,
        }
    }

    pub fn save(&self, path: &Path) -> anyhow::Result<()> {
        let bytes = bincode::serialize(&CacheFileRef {
            version: CACHE_VERSION,
            entries: &self.entries,
        })?;
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let tmp = path.with_file_name(format!(
            "{}.tmp.{pid}.{nanos}",
            path.file_name()
                .map(|s| s.to_string_lossy())
                .unwrap_or_else(|| "ingest-cache".into())
        ));
        fs::write(&tmp, &bytes)?;
        crate::cache::replace_file(&tmp, path)?;
        Ok(())
    }
}

fn file_stat(p: &Path) -> Option<(i64, u64)> {
    let m = fs::metadata(p).ok()?;
    let mtime = m
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    Some((mtime, m.len()))
}

/// Result of an incremental adapter pass.
pub struct Pass {
    /// All current messages for this adapter (cached for unchanged files, fresh for the rest).
    pub messages: Vec<Message>,
    /// Events ONLY for sessions touched this run (changed files + their siblings). Unchanged
    /// sessions keep their existing event files.
    pub events: Vec<Event>,
    /// Number of source files actually parsed (for logging).
    pub parsed: usize,
}

/// Parse `files`, pulling unchanged ones from `cache`. Updates the cache in place.
///
/// `root` scopes the stale-entry cleanup: the cache is SHARED across adapters (claude,
/// then codex, against the same store), so this call may only forget entries under its
/// own root - wiping everything not in `files` would erase the other adapters' entries
/// and silently disable the cache for them (run N parses claude, run N+1 codex, forever).
pub fn collect_cached<F>(cache: &mut IngestCache, root: &Path, files: &[PathBuf], parse: F) -> Pass
where
    F: Fn(&Path) -> (Vec<Message>, Vec<Event>) + Sync,
{
    let root_key = root.to_string_lossy().to_string();
    let t_stat = std::time::Instant::now();
    let stats: Vec<(PathBuf, String, i64, u64)> = files
        .par_iter()
        .filter_map(|p| {
            file_stat(p).map(|(mt, sz)| (p.clone(), p.to_string_lossy().to_string(), mt, sz))
        })
        .collect();
    let stat_ms = t_stat.elapsed().as_millis();
    let t_parse = std::time::Instant::now();
    let present: HashSet<String> = stats.iter().map(|(_, k, _, _)| k.clone()).collect();

    // split into cache hits and misses (changed/new)
    let mut hits: Vec<(PathBuf, String)> = Vec::new();
    let mut misses: Vec<(PathBuf, String, i64, u64)> = Vec::new();
    for (p, key, mt, sz) in &stats {
        match cache.entries.get(key) {
            Some(e) if e.mtime == *mt && e.size == *sz => hits.push((p.clone(), key.clone())),
            _ => misses.push((p.clone(), key.clone(), *mt, *sz)),
        }
    }

    // parse the misses
    let miss_parsed: Vec<(String, i64, u64, Vec<Message>, Vec<Event>)> = misses
        .par_iter()
        .map(|(p, key, mt, sz)| {
            let (m, e) = parse(p);
            (key.clone(), *mt, *sz, m, e)
        })
        .collect();

    // sessions touched: from the fresh parses AND from the OLD cache entries of changed files
    // (so a session that moved between files is fully refreshed)
    let mut affected: HashSet<std::sync::Arc<str>> = HashSet::new();
    for (_, _, _, m, _) in &miss_parsed {
        for msg in m {
            affected.insert(msg.session.clone());
        }
    }
    for (_, key, _, _) in &misses {
        if let Some(e) = cache.entries.get(key) {
            for cm in &e.msgs {
                affected.insert(cm.session.clone());
            }
        }
    }

    // unchanged sibling files that share an affected session: re-parse them so the session's
    // event file is rebuilt from ALL its sources (no-op for one-file-per-session adapters)
    let siblings: Vec<(PathBuf, String)> = hits
        .iter()
        .filter(|(_, key)| {
            cache
                .entries
                .get(key)
                .map(|e| e.msgs.iter().any(|cm| affected.contains(&cm.session)))
                .unwrap_or(false)
        })
        .cloned()
        .collect();
    let sib_keys: HashSet<String> = siblings.iter().map(|(_, k)| k.clone()).collect();
    let sib_parsed: Vec<(String, Vec<Message>, Vec<Event>)> = siblings
        .par_iter()
        .map(|(p, key)| {
            let (m, e) = parse(p);
            (key.clone(), m, e)
        })
        .collect();

    let mut messages: Vec<Message> = Vec::new();
    let mut events: Vec<Event> = Vec::new();

    // cached messages for unchanged, non-sibling files
    for (_, key) in &hits {
        if sib_keys.contains(key) {
            continue;
        }
        if let Some(e) = cache.entries.get(key) {
            messages.extend(e.msgs.iter().map(CMsg::to_msg));
        }
    }
    // fresh: changed files (messages + events) + sibling files (messages cached-equal + events)
    for (key, mt, sz, m, e) in miss_parsed {
        cache.entries.insert(
            key,
            Entry {
                mtime: mt,
                size: sz,
                msgs: m.iter().map(CMsg::from).collect(),
            },
        );
        messages.extend(m);
        events.extend(e);
    }
    for (_key, m, e) in sib_parsed {
        // sibling cache entry is already valid (file unchanged); just take messages + events
        messages.extend(m);
        events.extend(e);
    }

    // forget files that no longer exist - but ONLY under this adapter's root
    cache
        .entries
        .retain(|k, _| !k.starts_with(&root_key) || present.contains(k));

    println!(
        "  [{}] {} files: stat {}ms · parse+materialize {}ms ({} changed, {} siblings)",
        root.file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default(),
        files.len(),
        stat_ms,
        t_parse.elapsed().as_millis(),
        misses.len(),
        siblings.len()
    );
    Pass {
        messages,
        events,
        parsed: misses.len() + siblings.len(),
    }
}
