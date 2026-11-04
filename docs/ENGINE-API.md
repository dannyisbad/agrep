# Engine library surface

`crates/agrep-core` is the engine; `crates/agrep` is the command-line binary built on
it. This page lists what the binary, the explorer and the examples rely on. Everything
not listed here (FTS tables, block bounds, parser state, dictionaries, statistics) is
private and may change with the format marker; see [ENGINE.md](ENGINE.md).

## Reader tables

Open a snapshot with `store::reader(&DataRoot)`: a read-only connection inside one
transaction. Commands read these tables and views:

- `sources(path, agent, size, mtime_ns, change_token, last_error)`: one row per source
  file. Paths beginning with `@` or `token:` name sources that are not single files (a
  whole store, or a keyed entry inside one).
- `sessions(agent, id, project, cwd, parent, family, side, title, first_ts, last_ts)`.
- `prose(id, source, agent, session, project, turn, who, model, model_source, ts, text, digest, extra, position, visible)`:
  `text` is UTF-8 bytes, `digest` its SHA-256 in hex, `turn` the source's own turn and
  `position` the canonical one. Only `visible = 1` rows are canonical.
- `tools`: a view with the same columns for tool calls (`who` is `tool`, `extra` the
  call id).
- `events(source, session, call_id, body, id, ts, position, visible, text)`: a view.
  `body` is JSON with `kind`, `name`, `input_chars`, `output_chars`, `output_bytes`,
  `ok`, `call_id`, `child_session`, `meta`, `state`, `ts`, `native_turn`, `visible`,
  `turn`, `position`, `input_end` and `output_start`.
- `reply_shapes(source, session, turn, body)`.

Tool text in `tools.text` and `events.text` is encoded; decode it with
`store::decode("tools", bytes)` (`store::encode_tool` is the inverse, for fixtures). A
tool event's text (`tool_text::format`) is a head, the tool name marked `[failed]` for a
failed call, then `: ` and the input, then a newline and the output when there is one.
`input_end` is the character offset of that newline and `output_start` the offset after
it (0 when there is no head or input); both are null without output, and offsets count
Unicode scalar values. `tool_text::split` recovers the parts.

`store::digest` returns a text's SHA-256 as hex. `store::counts` returns the session,
visible prose, visible tool and source-error counts.

## Retrieval

- `query::search(&Connection, &Options) -> Result<Results>` runs a keyword search;
  `query::search_with` also takes `query::Plans`, which keeps word expansions and term
  estimates across searches of one pattern over different scopes.
- `query::coverage(&Connection, &Options)` is the explicit IDF-weighted content-terms
  lane. It refuses `-w`, `-E`, counts and non-score sorts.
- `Filters.caller: Option<CallerFilter>` withholds the caller's rows: `agent`,
  `session`, `family`, `exclude_from_turn` (withhold that session's rows at or after
  this position), `exclude_family`, and `excluded_sessions` (withhold these sessions of
  the agent). `caller::options` fills it from a resolved `caller::Caller`;
  `query::caller_allows` applies the same predicate to a hit, and `caller::excludes` to
  a session list. `caller::search` and `caller::search_with` search with it and set each
  hit's `handle` and `self` marker.
- `Filters.session_rows` restricts a scope to one session from a position on, and
  `Filters.sessions` to a list of sessions; both read through the session indexes.
- `Results.total_basis` is `exact` or `floor`. `Results.pending_sources` is filled by
  the binary from its census. `Results.timings` has `plan_ms`, `sqlite_ms`,
  `hydrate_ms`, `score_ms` and `total_ms`, without text or identifiers.
- `Hit.meta_marker` serializes as `meta`, only when true: the row is the sole proven
  `~meta` evidence `--no-meta` retained.
- `semantic::search(&DataRoot, &Options, family_diverse)`, or `semantic::begin` and
  `semantic::finish` around a keyword search, return `semantic::SearchResults`: normal
  `Results` and a numeric `Coverage`. `hybrid::merge` combines a keyword and a meaning
  result for display.

## Freshness and refresh

- `freshness::pending(&DataRoot) -> Result<Pending>` runs the stat-only census without a
  lock. `Pending` has `changed_sources` and `merge_pending`, plus diagnostic timings and
  counts: `census_ms`, `read_ms`, `listing_ms`, `stat_ms`, `comparison_ms`,
  `listed_directories`, `cached_directories` and `stat_files`. `agrep __census` prints
  it as JSON without scheduling anything.
- `freshness::spawn(&DataRoot, binary)` starts a detached `__work` when `work.lock` is
  free; `freshness::spawn_merge` starts `__work merge` when `merge.lock` is free.
  Callers spawn a merger only when `fts_segments::pending` reports merge work.
- `store::refresh(&DataRoot, explicit, full)` and `store::reindex` refresh the index;
  `store::refresh_session(&DataRoot, agent, session)` refreshes only the sources already
  attributed to that session, leaving other sources' fingerprints alone.
- `Refresh` reports `sources`, `parsed`, `changed_rows`, `errors`, `resumed_sources`,
  `bytes_read`, `bytes_parsed` and timings (`census_ms`, `open_ms` for the writer
  connection with any WAL recovery, `parse_ms`, `write_ms`, `bounds_ms`, `stats_ms`,
  `commit_ms`, `total_ms`, and per-adapter maps). `surface::refresh_json(refresh, extra)`
  is the line `index` and `reindex` print once they finish; it omits `skipped_lock` and
  appends `extra`: `data::lock_waits()` (`wait_foreground_lock_ms`,
  `wait_writer_queue_ms`, `wait_writer_lock_ms`, `wait_work_lock_ms`, the time the whole
  process was blocked on each lock file) and `after_refresh_ms`, the time from the
  refresh's end to the line.
- `Explorer::with_freshness` takes a closure that runs the census and returns the
  pending-source count; `agrep serve` and `agrep ui` pass the command line's own, so the
  explorer reports the same count as the terminal.

## Profiling

`AGREP_QUERY_TIMING` prints phase timings as JSON lines on stderr; `AGREP_SEM_PROFILE`
adds model and batch detail. Normal commands print neither.
