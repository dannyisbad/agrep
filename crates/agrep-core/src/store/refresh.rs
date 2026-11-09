use super::bounds::{raise_changed_bounds, update_blocks};
use super::connection::{open_writer, reader, schema, search_indexes, writer};
use super::families::resolve_families;
use super::fts::build_fts;
use super::replay::{canonicalize, settle_turnless, source_sessions};
use super::rows::{remove_source, replace};
use super::sources::{gone, stamp_present};
use super::text::digest;
use super::{Changes, Refresh, Stamp, WRITE_TIMES};
use crate::data::DataRoot;
use crate::ingest::registry::{self, Adapter, ADAPTERS};
use crate::intake_batch::{IntakeBatch, SourceBatch};
use crate::intake_pipeline::{parse_ahead, Parsed};
use crate::source_token::SourceToken;
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::OptionalExtension;
use rusqlite::{params, Connection, Transaction};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

pub fn refresh(root: &DataRoot, explicit: bool, full: bool) -> Result<Refresh> {
    refresh_inner(root, explicit, full, false, None, None)
}
/// Wait for preceding writers, then refresh changes from the caller's census against
/// their committed stamps. Sources they already covered need no parsing.
pub fn refresh_checked(root: &DataRoot, check: crate::freshness::Check) -> Result<Refresh> {
    refresh_inner(root, true, false, false, None, Some(check.census))
}
pub fn reindex(root: &DataRoot) -> Result<Refresh> {
    refresh_inner(root, true, true, true, None, None)
}
/// Refresh only sources already attributed to this transcript. Other changed
/// sources remain pending and their recorded stamps are not advanced.
pub fn refresh_session(root: &DataRoot, agent: &str, session: &str) -> Result<Refresh> {
    let c = reader(root)?;
    let paths = c.prepare("SELECT s.path FROM sources s JOIN session_sources x ON x.source=s.rowid WHERE x.agent=? AND x.session=?")?
        .query_map([agent, session], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<HashSet<_>>>()?;
    anyhow::ensure!(!paths.is_empty(), "caller has no indexed source");
    drop(c);
    refresh_inner(root, true, false, false, Some(&paths), None)
}
fn refresh_inner(
    root: &DataRoot,
    explicit: bool,
    full: bool,
    rebuild: bool,
    scope: Option<&HashSet<String>>,
    census: Option<crate::freshness::Census>,
) -> Result<Refresh> {
    let start = Instant::now();
    WRITE_TIMES.set((0, 0));
    let first = !root.db().exists();
    let lock = match root.lock(explicit || first)? {
        // A merge step is bounded and yields to queued writers, so wait for it rather
        // than leave the change pending until another command starts a worker.
        None if root.merging()? => root.lock(true)?,
        lock => lock,
    };
    if lock.is_none() {
        return Ok(Refresh {
            skipped_lock: true,
            ..Default::default()
        });
    }
    let first = !root.db().exists();
    // A rebuild writes a new database as a first build does and then replaces the live one,
    // which readers keep using meanwhile; a crash leaves it whole.
    let fresh = first || rebuild;
    let initial = root.path().join("index.building.db");
    if fresh || initial.exists() {
        // Left by a build that did not finish.
        clear_initial(&initial)?;
    }
    let opening = Instant::now();
    let mut c = if fresh {
        open_writer(&initial, false)?
    } else {
        writer(root)?
    };
    let open_ms = opening.elapsed().as_secs_f64() * 1000.;
    if fresh {
        defer_initial_bookkeeping(&c)?;
    }
    let old = {
        let _span = crate::semantic_profile::span("refresh_known_sources");
        crate::freshness::indexed(&c)?
    };
    let (harness, policy_digest, previous) = harness_policy(root, &c)?;
    let full = full || previous.as_deref() != Some(&policy_digest);
    let tick = Instant::now();
    let mut census = refresh_census(&c, &old, census)?;
    let mut stamps = std::mem::take(&mut census.stamps);
    if let Some(paths) = scope {
        stamps.retain(|source| paths.contains(&source.path));
    }
    let census_ms = tick.elapsed().as_secs_f64() * 1000.;
    let (changed, removed) = source_changes(&old, &stamps, scope, full);
    let mut info = Refresh {
        sources: stamps.len(),
        census_ms,
        open_ms,
        ..Default::default()
    };
    if changed.is_empty() && removed.is_empty() && !fresh {
        info.total_ms = start.elapsed().as_secs_f64() * 1000.;
        return Ok(info);
    }
    if first {
        // 0.4.9's notice (py/indexd_runtime.py:944-946): the size is unknown here.
        crate::errln!(
            "building the search db (a first build can take a while on a large history) …"
        );
    } else if rebuild {
        crate::errln!("rebuilding the search db (a rebuild can take a while on a large history) …");
    }
    let changed_stamps: Vec<_> = stamps
        .iter()
        .filter(|s| changed.contains(Path::new(&s.path)))
        .collect();
    let (active, cursors) = parsing_state(&c, &changed_stamps, &removed, &changed, full)?;
    let parse_start = Instant::now();
    let parsers = parser_pool(fresh || full);
    let parser_width = std::thread::available_parallelism()
        .map_or(1, usize::from)
        .min(if fresh || full { 8 } else { 4 });
    let batches = Collection {
        root,
        stamps: &stamps,
        changed_stamps: &changed_stamps,
        changed: &changed,
        active: &active,
        cursors,
        full,
    }
    .collect(&c, parsers)?;
    info.parse_ms = parse_start.elapsed().as_secs_f64() * 1000.;
    info.adapter_parse_ms = batches
        .iter()
        .map(|(name, ms, _)| (name.clone(), *ms))
        .collect();
    info.adapter_sources = batches
        .iter()
        .map(|(name, _, b)| (name.clone(), b.batches.len() + b.pending.len()))
        .collect();
    let batches = batches.into_iter().map(|(name, _, batch)| (name, batch));
    let write_start = Instant::now();
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    if scope.is_none() {
        crate::freshness::save(&tx, &census)?;
    }
    let mut state = RefreshState::new(parsers, fresh);
    for key in &removed {
        state.retire_source(&tx, key)?;
        tx.execute("DELETE FROM sources WHERE path=?", [key])?;
    }
    let sizes: HashMap<&str, u64> = changed_stamps
        .iter()
        .map(|s| (s.path.as_str(), s.size))
        .collect();
    let mut parse_wait = std::time::Duration::ZERO;
    for (adapter, mut b) in batches {
        let adapter_write_start = Instant::now();
        state.prepare_batch(&tx, &mut b, &active)?;
        let tokens = &b.tokens;
        let mut source_parse_ms = 0.;
        let mut write = |parsed: Parsed| -> Result<()> {
            source_parse_ms += parsed.0.parse_ms;
            state.write_source(&tx, parsed, &harness, tokens, &mut info)
        };
        for s in b.batches {
            write((s, Vec::new()))?;
        }
        parse_wait += parse_ahead(parser_width, b.pending, &sizes, &mut write)?;
        info.adapter_source_parse_ms
            .insert(adapter.clone(), source_parse_ms);
        info.adapter_write_ms
            .insert(adapter, adapter_write_start.elapsed().as_secs_f64() * 1000.);
    }
    let errors = state.finish(&tx, &mut info, stamps, &changed, parse_wait, write_start)?;
    tx.execute("INSERT INTO state VALUES('refreshed',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[chrono::Utc::now().timestamp_millis().to_string()])?;
    if scope.is_none() {
        tx.execute("INSERT INTO state VALUES('harness',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[policy_digest])?;
    }
    let phase = Instant::now();
    tx.commit()?;
    close_refresh(c, fresh, &initial, root, rebuild && !first)?;
    info.commit_ms = phase.elapsed().as_secs_f64() * 1000.;
    let (row_ns, fts_ns) = WRITE_TIMES.get();
    info.row_diff_ms = row_ns as f64 / 1_000_000.;
    info.fts_ms = fts_ns as f64 / 1_000_000.;
    info.errors = errors;
    info.total_ms = start.elapsed().as_secs_f64() * 1000.;
    drop(lock);
    Ok(info)
}

struct Collection<'a> {
    root: &'a DataRoot,
    stamps: &'a [Stamp],
    changed_stamps: &'a [&'a Stamp],
    changed: &'a HashSet<PathBuf>,
    active: &'a HashSet<String>,
    cursors: HashMap<String, Vec<u8>>,
    full: bool,
}
struct RefreshState {
    first: bool,
    changes: Changes,
    turnless_before: HashSet<(String, String)>,
    canonical_sessions: HashSet<(String, String)>,
    fast_sessions: HashSet<(String, String)>,
    forced_sessions: HashSet<(String, String)>,
    errors: HashMap<String, String>,
    pages: crate::incremental::GraphPages,
}
impl RefreshState {
    fn new(parsers: &'static std::sync::LazyLock<rayon::ThreadPool>, first: bool) -> Self {
        Self {
            first,
            changes: Changes {
                bulk: first,
                ..Default::default()
            },
            turnless_before: HashSet::new(),
            canonical_sessions: HashSet::new(),
            fast_sessions: HashSet::new(),
            forced_sessions: HashSet::new(),
            errors: HashMap::new(),
            pages: crate::incremental::GraphPages::new(parsers),
        }
    }
    fn retire_source(&mut self, tx: &Transaction<'_>, key: &str) -> Result<()> {
        source_sessions(tx, key, &mut self.canonical_sessions)?;
        source_sessions(tx, key, &mut self.forced_sessions)?;
        for t in ["prose", "tools"] {
            remove_source(tx, t, key, &mut self.changes)?;
        }
        Ok(())
    }
}
fn clear_initial(initial: &Path) -> Result<()> {
    for suffix in ["", "-journal", "-wal", "-shm"] {
        let p = PathBuf::from(format!("{}{suffix}", initial.display()));
        match fs::remove_file(p) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

fn defer_initial_bookkeeping(c: &Connection) -> Result<()> {
    search_indexes(c, false)?;
    // Initial rows are invisible until the final commit. Build their derived
    // bookkeeping in bulk instead of maintaining counters and dirty queues
    // for each of the million inserted rows.
    for name in [
        "prose_count_insert",
        "prose_count_delete",
        "prose_count_visibility",
        "prose_user_count_insert",
        "prose_user_count_delete",
        "prose_user_count_visibility",
        "tools_count_insert",
        "tools_count_delete",
        "tools_count_visibility",
        "prose_stats_insert",
        "prose_stats_delete",
        "prose_stats_update",
        "session_stats_family",
        "tool_bound_update",
        "tool_bound_delete",
        "tool_word_dirty_insert",
        "tool_word_dirty_update",
        "tool_word_old_update",
        "tool_word_old_delete",
        "semantic_insert",
        "semantic_update",
        "semantic_delete",
    ] {
        c.execute_batch(&format!("DROP TRIGGER IF EXISTS {name}"))?;
    }
    Ok(())
}

fn refresh_census(
    c: &Connection,
    old: &[crate::freshness::Fingerprint],
    census: Option<crate::freshness::Census>,
) -> Result<crate::freshness::Census> {
    Ok(match census {
        Some(mut census) => {
            // A preceding writer may have ingested a newer stamp than this census saw.
            // Recheck only differing files, so its completed work is not parsed again.
            let mut indexed = old.iter().peekable();
            census.stamps.retain_mut(|s| {
                while indexed.peek().is_some_and(|old| old.0 < s.path) {
                    indexed.next();
                }
                if indexed.peek().is_some_and(|(path, size, mtime, token)| {
                    *path == s.path && s.size == *size && s.mtime_ns == *mtime && s.token == *token
                }) {
                    return true;
                }
                match stamp_present(&s.agent, Path::new(&s.path)) {
                    Some(current) => {
                        *s = current;
                        true
                    }
                    None => false,
                }
            });
            census
        }
        None => crate::freshness::scan(c, false)?,
    })
}

fn source_changes(
    old: &[crate::freshness::Fingerprint],
    stamps: &[Stamp],
    scope: Option<&HashSet<String>>,
    full: bool,
) -> (HashSet<PathBuf>, Vec<String>) {
    let mut changed = HashSet::new();
    let mut removed = Vec::new();
    let mut next = 0;
    for (path, size, mtime, token) in old {
        while next < stamps.len() && stamps[next].path < *path {
            changed.insert(PathBuf::from(&stamps[next].path));
            next += 1;
        }
        if let Some(stamp) = stamps.get(next).filter(|s| s.path == *path) {
            if full || stamp.size != *size || stamp.mtime_ns != *mtime || stamp.token != *token {
                changed.insert(PathBuf::from(path));
            }
            next += 1;
        } else if scope.is_none_or(|paths| paths.contains(path)) && gone(Path::new(path)) {
            removed.push(path.clone());
        }
    }
    changed.extend(stamps[next..].iter().map(|s| PathBuf::from(&s.path)));
    (changed, removed)
}

type ParsingState = (HashSet<String>, HashMap<String, Vec<u8>>);
fn parsing_state(
    c: &Connection,
    changed_stamps: &[&Stamp],
    removed: &[String],
    changed: &HashSet<PathBuf>,
    full: bool,
) -> Result<ParsingState> {
    let mut active: HashSet<String> = changed_stamps.iter().map(|s| s.agent.clone()).collect();
    let mut removed_agent = c.prepare_cached("SELECT agent FROM sources WHERE path=?")?;
    for key in removed {
        if let Some(agent) = removed_agent
            .query_row([key], |r| r.get::<_, String>(0))
            .optional()?
        {
            active.insert(agent);
        }
    }
    drop(removed_agent);
    let mut cursors = HashMap::new();
    if !full {
        let mut q = c.prepare("SELECT body FROM parser_cursors WHERE path=?")?;
        for path in changed {
            if let Some(body) = q
                .query_row([path.to_string_lossy()], |r| r.get::<_, Vec<u8>>(0))
                .optional()?
            {
                cursors.insert(path.to_string_lossy().into_owned(), body);
            }
        }
    }
    Ok((active, cursors))
}

fn parser_pool(bulk: bool) -> &'static std::sync::LazyLock<rayon::ThreadPool> {
    static PARSERS: std::sync::LazyLock<rayon::ThreadPool> = std::sync::LazyLock::new(|| {
        rayon::ThreadPoolBuilder::new()
            .num_threads(
                std::thread::available_parallelism()
                    .map_or(1, usize::from)
                    .min(4),
            )
            .build()
            .expect("parser thread pool")
    });
    static BUILD_PARSERS: std::sync::LazyLock<rayon::ThreadPool> = std::sync::LazyLock::new(|| {
        rayon::ThreadPoolBuilder::new()
            .num_threads(
                std::thread::available_parallelism()
                    .map_or(1, usize::from)
                    .min(8),
            )
            .build()
            .expect("bulk parser thread pool")
    });
    if bulk {
        &BUILD_PARSERS
    } else {
        &PARSERS
    }
}

impl Collection<'_> {
    fn collect(
        self,
        c: &Connection,
        parsers: &std::sync::LazyLock<rayon::ThreadPool>,
    ) -> Result<Vec<(String, f64, IntakeBatch)>> {
        let Self {
            root,
            stamps,
            changed_stamps,
            changed,
            active,
            cursors,
            full,
        } = self;
        let source_paths: HashMap<&str, Vec<PathBuf>> =
            changed_stamps
                .iter()
                .fold(HashMap::new(), |mut paths, source| {
                    paths
                        .entry(source.agent.as_str())
                        .or_default()
                        .push(PathBuf::from(&source.path));
                    paths
                });
        let mut token_cache = HashMap::new();
        for adapter in ADAPTERS.iter().filter(|a| {
            active.contains(a.name()) && a.fingerprint() == registry::Fingerprint::Token
        }) {
            let lower = format!("token:{}:", adapter.name());
            let upper = format!("token:{};", adapter.name());
            let mut query = c.prepare_cached(
                "SELECT path,change_token FROM sources WHERE path>=?1 AND path<?2",
            )?;
            let mut tokens = HashMap::new();
            for row in query.query_map(params![lower, upper], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, SourceToken>(1)?))
            })? {
                let (path, token) = row?;
                let SourceToken::Opaque(token) = token else {
                    anyhow::bail!("invalid adapter token")
                };
                tokens.insert(path, token);
            }
            token_cache.insert(adapter.name(), tokens);
        }
        let collect_adapter = |a: &&dyn Adapter| {
            let adapter_start = Instant::now();
            let tokens = token_cache.get(a.name()).cloned().unwrap_or_default();
            let mut batch = IntakeBatch {
                changed: if a.name() == "gemini" {
                    stamps
                        .iter()
                        .filter(|s| s.agent == "gemini")
                        .map(|s| PathBuf::from(&s.path))
                        .collect()
                } else {
                    changed.clone()
                },
                force: full,
                cursors: cursors.clone(),
                database: Some(root.db()),
                defer: true,
                source_paths: source_paths.get(a.name()).cloned().unwrap_or_default(),
                tokens,
                ..Default::default()
            };
            crate::intake::take_diagnostics();
            let (m, e, o, issues) = a.collect_checked(&mut batch);
            for message in crate::intake::take_diagnostics() {
                batch.issues.push(crate::intake_batch::SourceReadIssue::new(
                    a.name(),
                    a.runtime_issue_root(),
                    "source-read-failed",
                    message,
                ));
            }
            batch.issues.extend(issues);
            if a.fingerprint() == registry::Fingerprint::Always {
                batch.batches.push(SourceBatch {
                    key: format!("@{}", a.name()),
                    agent: a.name().into(),
                    messages: m,
                    events: e,
                    error: if matches!(
                        o,
                        crate::intake_batch::ReadOutcome::Complete
                            | crate::intake_batch::ReadOutcome::Undone
                    ) && batch.issues.is_empty()
                    {
                        None
                    } else {
                        Some(format!("{} source parse incomplete", a.name()))
                    },
                    cwds: std::mem::take(&mut batch.cwds),
                    ..Default::default()
                })
            }
            (
                a.name().to_owned(),
                adapter_start.elapsed().as_secs_f64() * 1000.,
                batch,
            )
        };
        let batches: Vec<_> = if active.len() <= 1 {
            ADAPTERS
                .iter()
                .filter(|a| active.contains(a.name()))
                .map(collect_adapter)
                .collect()
        } else {
            parsers.install(|| {
                ADAPTERS
                    .par_iter()
                    .filter(|a| active.contains(a.name()))
                    .map(collect_adapter)
                    .collect()
            })
        };
        Ok(batches)
    }
}

impl RefreshState {
    fn prepare_batch(
        &mut self,
        tx: &Transaction<'_>,
        b: &mut IntakeBatch,
        active: &HashSet<String>,
    ) -> Result<()> {
        let Self {
            canonical_sessions,
            forced_sessions,
            changes,
            errors,
            ..
        } = self;
        for key in &b.retired {
            source_sessions(tx, key, canonical_sessions)?;
            source_sessions(tx, key, forced_sessions)?;

            for table in ["prose", "tools"] {
                remove_source(tx, table, key, changes)?;
            }
        }

        if b.issues.is_empty() {
            for key in b
                .tokens
                .keys()
                .filter(|key| key.starts_with("token:") && !b.seen_tokens.contains(*key))
            {
                let agent = key.split(':').nth(1).unwrap_or("");
                if active.contains(agent)
                    && ADAPTERS.iter().any(|a| {
                        a.name() == agent && a.fingerprint() == registry::Fingerprint::Token
                    })
                {
                    source_sessions(tx, key, canonical_sessions)?;
                    source_sessions(tx, key, forced_sessions)?;

                    for table in ["prose", "tools"] {
                        remove_source(tx, table, key, changes)?;
                    }
                    tx.execute("DELETE FROM sources WHERE path=?", [key])?;
                }
            }
        }
        for issue in std::mem::take(&mut b.issues) {
            errors.insert(issue.path.to_string_lossy().into_owned(), issue.reason);
        }
        Ok(())
    }
}

impl RefreshState {
    fn write_source(
        &mut self,
        tx: &Transaction<'_>,
        (mut s, prepared): Parsed,
        harness: &crate::row_class::HarnessPolicy,
        tokens: &HashMap<String, String>,
        info: &mut Refresh,
    ) -> Result<()> {
        let Self {
            first,
            canonical_sessions,
            forced_sessions,
            fast_sessions,
            turnless_before,
            changes,
            errors,
            pages,
            ..
        } = self;
        let first = *first;
        s.messages = crate::row_class::normalize_messages(std::mem::take(&mut s.messages), harness);
        if s.key.starts_with('@') {
            s.messages = crate::intake_batch::canonicalize_messages(s.messages);
        }
        info.parsed += 1;
        if let Some(error) = s.error {
            if s.key.starts_with('@') || s.key.starts_with("token:") {
                let token = SourceToken::Opaque(tokens.get(&s.key).cloned().unwrap_or_default());
                tx.execute("INSERT INTO sources VALUES(?1,?2,0,0,?3,?4) ON CONFLICT(path) DO UPDATE SET change_token=?3,last_error=?4", params![s.key,s.agent,token,error])?;
            }
            errors.insert(s.key.clone(), error);
            return Ok(());
        }
        if !s.append {
            source_sessions(tx, &s.key, canonical_sessions)?;
            source_sessions(tx, &s.key, forced_sessions)?;
        }
        let changed_sessions: HashSet<_> = s
            .messages
            .iter()
            .map(|m| (m.agent.to_string(), m.session.to_string()))
            .chain(
                s.events
                    .iter()
                    .map(|e| (e.agent.to_string(), e.session.clone())),
            )
            .collect();
        if !first {
            for (agent, session) in &changed_sessions {
                if !tx.prepare_cached("SELECT EXISTS(SELECT 1 FROM prose INDEXED BY prose_session WHERE agent=?1 AND session=?2)")?.query_row(params![agent,session], |r| r.get::<_,bool>(0))? {
                turnless_before.insert((agent.clone(),session.clone()));
            }
            }
        }
        if s.append {
            fast_sessions.extend(changed_sessions);
        } else {
            forced_sessions.extend(changed_sessions);
        }
        canonical_sessions.extend(
            s.messages
                .iter()
                .map(|m| (m.agent.to_string(), m.session.to_string())),
        );
        canonical_sessions.extend(
            s.events
                .iter()
                .map(|e| (e.agent.to_string(), e.session.clone())),
        );
        let changed_rows = replace(tx, &s, prepared, changes, first)?;
        info.changed_rows += changed_rows;
        info.bytes_read += s.bytes_read;
        info.bytes_parsed += s.bytes_parsed;
        info.resumed_sources += usize::from(s.append);
        if let Some(cursor) = &s.cursor {
            tx.execute("INSERT INTO parser_cursors VALUES(?1,?2) ON CONFLICT(path) DO UPDATE SET body=excluded.body",params![s.key,cursor])?;
            let source: i64 =
                tx.query_row("SELECT rowid FROM sources WHERE path=?", [&s.key], |r| {
                    r.get(0)
                })?;
            let phase = Instant::now();
            let session = s
                .messages
                .first()
                .map(|m| (m.agent, m.session.as_ref()))
                .or_else(|| s.events.first().map(|e| (e.agent, e.session.as_str())));
            crate::incremental::save_nodes(
                tx,
                source,
                std::mem::take(&mut s.nodes),
                s.reset_nodes,
                session,
                &s.events,
                pages,
            )?;
            info.node_chunks_ms += phase.elapsed().as_secs_f64() * 1000.;
        }
        if s.key.starts_with('@') || s.key.starts_with("token:") {
            let token = SourceToken::Opaque(tokens.get(&s.key).cloned().unwrap_or_default());
            tx.execute("INSERT INTO sources VALUES(?1,?2,0,0,?3,NULL) ON CONFLICT(path) DO UPDATE SET change_token=?3,last_error=NULL",params![s.key,s.agent,token])?;
        }
        Ok(())
    }
}

impl RefreshState {
    fn finish(
        self,
        tx: &Transaction<'_>,
        info: &mut Refresh,
        stamps: Vec<Stamp>,
        changed: &HashSet<PathBuf>,
        parse_wait: std::time::Duration,
        write_start: Instant,
    ) -> Result<usize> {
        let Self {
            first,
            mut changes,
            turnless_before,
            canonical_sessions,
            mut fast_sessions,
            forced_sessions,
            errors,
            pages,
        } = self;
        let phase = Instant::now();
        pages.finish(tx)?;
        info.node_chunks_ms += phase.elapsed().as_secs_f64() * 1000.;
        save_source_stamps(tx, stamps, changed, &errors)?;
        crate::tool_store::collect(tx)?;
        if first {
            let phase = Instant::now();
            search_indexes(tx, true)?;
            info.secondary_indexes_ms = phase.elapsed().as_secs_f64() * 1000.;
        }
        fast_sessions.retain(|s| !forced_sessions.contains(s));
        let phase = Instant::now();
        canonicalize(tx, &canonical_sessions, &fast_sessions, &mut changes, first)?;
        settle_turnless(tx, &turnless_before, &canonical_sessions, first)?;
        info.canonical_ms = phase.elapsed().as_secs_f64() * 1000.;
        changes.fts.apply(tx)?;
        let presence = if first { build_fts(tx)? } else { None };
        let phase = Instant::now();
        crate::tool_index::refresh(tx, first, presence)?;
        info.dictionary_ms = phase.elapsed().as_secs_f64() * 1000.;
        // Sources parse while earlier ones are written; only the writer's waits for a
        // parse count as parsing.
        info.parse_ms += parse_wait.as_secs_f64() * 1000.;
        info.write_ms = write_start
            .elapsed()
            .saturating_sub(parse_wait)
            .as_secs_f64()
            * 1000.;
        let phase = Instant::now();
        if first {
            update_blocks(tx, &changes.blocks, None)?;
        } else {
            raise_changed_bounds(tx, &mut changes)?;
        }
        tx.execute("DELETE FROM bound_updates", [])?;
        tx.execute("DELETE FROM tool_bound_updates", [])?;
        info.bounds_ms = phase.elapsed().as_secs_f64() * 1000.;
        let phase = Instant::now();
        finish_statistics(tx, first, &canonical_sessions)?;
        info.stats_ms = phase.elapsed().as_secs_f64() * 1000.;
        Ok(errors.len())
    }
}
fn save_source_stamps(
    tx: &Transaction<'_>,
    stamps: Vec<Stamp>,
    changed: &HashSet<PathBuf>,
    errors: &HashMap<String, String>,
) -> Result<()> {
    // A source the census could not stamp fails for that reason; reading it then fails too,
    // but only as a consequence.
    let mut stamped_failures = HashSet::new();
    for s in stamps {
        if s.error.is_some() {
            stamped_failures.insert(s.path.clone());
        }
        let error = s
            .error
            .or_else(|| errors.get(&s.path).cloned())
            .or_else(|| {
                errors.iter().find_map(|(path, error)| {
                    Path::new(&s.path)
                        .starts_with(Path::new(path))
                        .then(|| error.clone())
                })
            });
        if !changed.contains(Path::new(&s.path)) {
            if let Some(error) = error {
                tx.prepare_cached(
                    "UPDATE sources SET last_error=?2 WHERE path=?1 AND last_error IS NOT ?2",
                )?
                .execute(params![s.path, error])?;
            }
            continue;
        }
        tx.prepare_cached("INSERT INTO sources VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(path) DO UPDATE SET size=?3,mtime_ns=?4,change_token=?5,last_error=?6")?.execute(params![s.path,s.agent,s.size,s.mtime_ns,s.token,error])?;
    }
    for (path, error) in errors
        .iter()
        .filter(|(path, _)| !stamped_failures.contains(*path))
    {
        tx.execute(
            "UPDATE sources SET last_error=?2 WHERE path=?1",
            params![path, error],
        )?;
    }
    Ok(())
}

fn finish_statistics(
    tx: &Transaction<'_>,
    first: bool,
    canonical_sessions: &HashSet<(String, String)>,
) -> Result<()> {
    // A chat is listed while search holds any of its rows: a turn, or a tool call.
    if first {
        tx.execute("DELETE FROM sessions WHERE NOT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=sessions.agent AND p.session=sessions.id) AND NOT EXISTS(SELECT 1 FROM event_sessions es JOIN event_rows e ON e.session_key=es.id WHERE es.agent=sessions.agent AND es.session=sessions.id AND e.active=1 AND e.blob IS NOT NULL)",[])?;
    } else {
        let mut q = tx.prepare_cached("DELETE FROM sessions WHERE agent=?1 AND id=?2 AND NOT EXISTS(SELECT 1 FROM prose p INDEXED BY prose_session WHERE p.agent=?1 AND p.session=?2) AND NOT EXISTS(SELECT 1 FROM event_sessions es JOIN event_rows e ON e.session_key=es.id WHERE es.agent=?1 AND es.session=?2 AND e.active=1 AND e.blob IS NOT NULL)")?;
        for (agent, session) in canonical_sessions {
            q.execute(params![agent, session])?;
        }
    }
    resolve_families(tx, canonical_sessions)?;
    crate::prose_index::refresh(tx, first)?;
    if first {
        crate::row_stats::build(tx)?;
    } else {
        crate::row_stats::refresh(tx)?;
    }
    if first {
        schema(tx)?;
        let (rows, user_rows): (i64, i64) = tx.query_row(
            "SELECT count(*),coalesce(sum(who!='agent'),0) FROM prose WHERE visible=1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        for (key, value) in [("prose_count", rows), ("prose_user_count", user_rows)] {
            tx.execute("INSERT INTO state(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", params![key, value])?;
        }
        tx.execute("INSERT INTO state(key,value) SELECT 'tools_count',count(*) FROM tools WHERE visible=1 ON CONFLICT(key) DO UPDATE SET value=excluded.value",[])?;
        tx.execute(
            "INSERT INTO semantic_queue SELECT id,digest,ts FROM prose WHERE visible=1",
            [],
        )?;
    }
    Ok(())
}

/// Seals a new database and puts it in place of `index.db`: renamed, for a first build, or
/// installed over the live index, for a rebuild (see [`install`]).
fn close_refresh(
    c: Connection,
    fresh: bool,
    initial: &Path,
    root: &DataRoot,
    replace: bool,
) -> Result<()> {
    if !fresh {
        let _span = crate::semantic_profile::span("refresh_connection_close");
        return c.close().map_err(|(_, e)| e.into());
    }
    c.execute_batch("PRAGMA journal_mode=WAL;")?;
    c.close().map_err(|(_, e)| e)?;
    // A connection that has not used the FTS tables caches none of their structures.
    let mut sealing = open_writer(initial, true)?;
    let tx = sealing.transaction()?;
    crate::fts_segments::seal(&tx)?;
    if replace {
        carry(&reader(root)?, &tx)?;
    }
    tx.commit()?;
    sealing.close().map_err(|(_, e)| e)?;
    if replace {
        install(initial, root)?;
    } else {
        fs::rename(initial, root.db())?;
        #[cfg(unix)]
        fs::File::open(root.path())?.sync_all()?;
    }
    Ok(())
}

/// Replaces the live index with the rebuilt `initial`. While no other connection has the
/// live index open, the file is renamed over it as a first build's is. Otherwise it is
/// copied in one SQLite transaction, which the readers' snapshots survive. Either way a
/// crash before the end leaves the old index whole.
fn install(initial: &Path, root: &DataRoot) -> Result<()> {
    // Windows refuses to rename over a file another process has open.
    #[cfg(unix)]
    if rename_unshared(initial, root)? {
        return Ok(());
    }
    let rebuilt = open_writer(initial, true)?;
    let mut live = writer(root)?;
    let copy = rusqlite::backup::Backup::new(&rebuilt, &mut live)?;
    anyhow::ensure!(
        copy.step(-1)? == rusqlite::backup::StepResult::Done,
        "the live index stayed locked"
    );
    drop(copy);
    live.close().map_err(|(_, e)| e)?;
    rebuilt.close().map_err(|(_, e)| e)?;
    clear_initial(initial)
}

/// Renames `initial` over `index.db` under an exclusive lock on it, which SQLite grants only
/// while no other connection has read it: a WAL connection keeps its shared lock until it
/// closes. The old file's write-ahead log is checkpointed and emptied first, so the rebuilt
/// file inherits no frames of it. A connection that opened the old file but first reads
/// after the rename sees its file moved and reopens the path (`connection::unmoved`).
#[cfg(unix)]
fn rename_unshared(initial: &Path, root: &DataRoot) -> Result<bool> {
    let live = Connection::open(root.db())?;
    live.busy_timeout(std::time::Duration::ZERO)?;
    match live.execute_batch(
        "PRAGMA locking_mode=EXCLUSIVE; PRAGMA wal_checkpoint(TRUNCATE); BEGIN EXCLUSIVE;",
    ) {
        Ok(()) => {}
        Err(rusqlite::Error::SqliteFailure(e, _))
            if matches!(
                e.code,
                rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
            ) =>
        {
            return Ok(false)
        }
        Err(e) => return Err(e.into()),
    }
    fs::rename(initial, root.db())?;
    fs::File::open(root.path())?.sync_all()?;
    // The old file is gone, so closing neither checkpoints into it nor deletes the log.
    live.close().map_err(|(_, e)| e)?;
    Ok(true)
}

/// What a rebuild keeps from the index it replaces, since no transcript holds it: the meaning
/// vectors and the rows they still cover, and every state value the build did not set.
fn carry(old: &Connection, tx: &Transaction<'_>) -> Result<()> {
    let mut values = old.prepare("SELECT key,value FROM state WHERE key!='semantic_ref_count'")?;
    let mut keep = tx.prepare("INSERT OR IGNORE INTO state VALUES(?1,?2)")?;
    for row in values.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, rusqlite::types::Value>(1)?,
        ))
    })? {
        let (key, value) = row?;
        keep.execute(params![key, value])?;
    }
    crate::semantic_store::carry(old, tx)
}

fn harness_policy(
    root: &DataRoot,
    c: &Connection,
) -> Result<(crate::row_class::HarnessPolicy, String, Option<String>)> {
    let harness_body = match fs::read_to_string(root.path().join("harness_prefixes.txt")) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e.into()),
    };
    let harness = crate::row_class::HarnessPolicy::parse(&harness_body);
    let policy_digest = digest(&harness_body);
    let previous: Option<String> = c
        .query_row("SELECT value FROM state WHERE key='harness'", [], |r| {
            r.get(0)
        })
        .optional()?;
    Ok((harness, policy_digest, previous))
}
