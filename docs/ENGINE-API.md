# Keyword engine integration surface

The engine work on `w05` keeps the columns of `sources`, `sessions`, `prose`, `tools`, `events`, and `reply_shapes`, and keeps `store::reader`, `store::decode`, and `query::search` signatures. FTS, bounds and parser-state tables are engine internals and require a fresh index for this iteration.

Additions for the other slices:

- `Results.pending_sources: usize`: number of source fingerprints differing from the committed snapshot. The CLI fills this after its census; presentation owns how to show it.
- `Filters.caller: Option<CallerFilter>`. CallerFilter has `agent`, `session`, `family`, `exclude_from_turn: Option<i64>`, `exclude_family: bool`, and `excluded_sessions: Vec<String>`. The session list excludes live descendants before candidate selection. Set a window start to exclude that session's positions at or after it. Set `exclude_family` for `--no-self`; leave both exclusions unset for `--self`. Returned rows in the caller session carry `Hit.self_marker`. Identity detection remains the presentation slice's job.
- `freshness::pending(&DataRoot) -> Result<Pending>` with `changed_sources` and `census_ms`. This stats inputs without parsing or taking a writer lock.
- `freshness::spawn(&DataRoot, &Path) -> Result<bool>` launches the passed binary's detached `__work refresh` when the work lock is free.
- `cmd::work` owns the detached task list under `work.lock`; refresh takes `writer.lock`, followed by semantic work, reclamation, scheduled archive and the post-index hook. `__work` accepts a task list, defaulting to `refresh`.

Normal reads build inline only when no index exists. Subsequent reads census, schedule work, and read the committed snapshot. Explicit `index` and `reindex` still refresh synchronously. `__census` is an internal full-process benchmark entry point, returning Pending JSON without scheduling work.

Current internal storage uses positional trigrams for prose and positional words plus a trigram word dictionary for tools, over already normalized text, with compressed per-gram block postings. The external tables and text codec are unchanged. Exact literal counts use FTS without hydrating text when all terms are indexable. Coverage scoring weights terms using inverse document frequency from the least-common constituent trigram and uses conservative block bounds.

Parser checkpoint additions: `Refresh.resumed_sources`, `bytes_read`, and `bytes_parsed`; private `parser_cursors` and `parser_graph` tables. `Event.turn: Option<u32>` carries native ordering when known, avoiding timestamp-tie attribution. Existing event-table columns are unchanged.

Append refresh parses only new JSONL bytes and carries the current row, pending calls, reply ordering, model and lineage. Pi branches replay persisted native entries instead of rereading the source. File identity/length/edge guards reject common rewrites; SHA-256 verification of the old prefix detects an arbitrary interior rewrite combined with growth. This exact verification reads prior bytes and is a known latency constraint, distinct from parsing.

The CLI overlaps census and retrieval. `pending_sources` is the census observation; another process may commit after that observation. FTS candidate clauses are ordered by document-frequency estimates, and a proven tiny posting stream can be exhausted directly.

Family-statistic maintenance now uses private `row_stats`, `stats_dirty`, `family_token_counts`, and `session_sources` tables. No stable table columns changed. The workspace SHA-256 dependency is 0.11; `store::digest` remains the string-returning helper for callers.

Tool dictionary tables (`tool_words`, `tool_lexicon`, `tool_word_dirty`, `tool_word_batch`) are private. ASCII alphanumeric substring counts expand to complete word postings; other literals use verified block scans. The stable row columns and tool-text decoder are unchanged.

Every `events.body` now carries `ts` (milliseconds), `native_turn`, and assigned `turn`/`position`. Canonical replay resolution updates the assigned fields together. Control events retain these fields even when no tool row is emitted. For tool events, `input_end` is the character offset of the separating newline and `output_start` is the following character offset; both are null when there is no output. Offsets count Unicode scalar values in the stored tool text. When the prefix is empty, output starts at zero after trimming.

First-build profiling adds aggregate `Refresh` fields: `row_diff_ms`, `fts_ms`, `secondary_indexes_ms`, `canonical_ms`, `dictionary_ms`, `node_chunks_ms`, `commit_ms`, `adapter_parse_ms`, `adapter_source_parse_ms`, `adapter_sources`, and `adapter_write_ms`. Per-adapter wall times overlap under parallel parsing; source-parse sums describe work rather than elapsed time. Row/FTS/dictionary/node timings are subdivisions of `write_ms`.

The native graph uses private `parser_graph` pages keyed by source and hash bucket. Lossless integer/string tuples retain entry identity, parent and order; only parser-consumed payload survives. Tool inputs and outputs refer to immutable typed event revisions and shared compressed text. Branch replay reads these pages and revisions, without reparsing the source prefix. `parser_cursors` contains the resumable parser state, without duplicate prefix/tail guards; exact SHA-256 prefix verification remains.

`Results.timings` adds aggregate `plan_ms`, `sqlite_ms`, `hydrate_ms`, `score_ms`, and `total_ms` for retrieval diagnostics; no source text or identifiers are included.

`freshness::Pending` additionally reports numeric `read_ms`, `listing_ms`, `stat_ms`, and `comparison_ms`. They partition its stat-only census time; `comparison_ms` includes remaining sorting and fingerprint comparison work.

`Hit.meta_marker` is serialized as `meta` only when true. The `--no-meta` query policy checks structured tool-call lineage, preserves ordinary control rows, and retains one marked row when it is the only observed evidence. Counts with this policy are rejected. Unknown command wrappers remain eligible.

`query::coverage(&Connection, &Options) -> Results` is the explicit IDF-weighted recovery lane; it returns labeled content-term hits independently of the primary page. Presentation keeps its output separate and preserves the primary exit status. All scope and caller predicates apply.

Directory cache columns and tool dictionary frequency columns are private. The directory cache stores relative entry names in length-prefixed blobs and streams the sorted source comparison. Tool document frequencies are updated from the old and new changed-row terms within the same transaction.

`prose_terms` is a private exact trigram document-frequency cache for the normal-text prose FTS field. Initial construction snapshots `fts5vocab`; changed visible rows update its counts transactionally before `stats_dirty` is cleared. Query planning no longer counts the common prose posting streams.

`tools` and `events` retain their read columns as SQLite views over private integer-keyed `event_rows`, `event_sessions`, `symbols`, and `tool_blobs`. Readers must continue using `store::decode("tools", bytes)`; tool bytes now carry a codec discriminator for lossless LZ4 or DEFLATE. `store::encode_tool` is available for fixtures and controlled codec experiments. The event body still exposes `ts`, `native_turn`, assigned `turn`/`position`, and Unicode-scalar `input_end`/`output_start`. Old native revisions and their text remain available while a parser branch can reference them.

Block posting keys pack lowercase ASCII letters and digits into integers. Other characters use a conservative universal bound; their complete FTS and verification semantics are unchanged. Per-block key lists and posting deltas are compressed private blobs, not reader-facing tables.

Each private gram posting now carries its matching-row maximum timestamp and role weight. Query clock and tie-time ceilings therefore exclude unrelated rows in the same physical block. The postings codec changed and requires a fresh index (or the isolated diagnostic bounds rebuild); no compatibility reader was added. The matching-row metadata does not change any public table column.

OS lock contention is recognized by the platform error returned by `fs2`, as well as `WouldBlock`. This covers Windows `ERROR_LOCK_VIOLATION`; lock files remain empty, and no reader accesses their locked byte range.

Merged command additions:

- `store::refresh_session(&DataRoot, agent, session)` refreshes only already indexed source paths for the caller. Other source stamps and global harness-policy state do not advance.
- `events` additionally exposes `id`, `ts`, `position`, `visible`, and encoded `text`. Its prior four read columns remain. `body` also retains `visible`. The indexed `(session_key, position, ts)` access path avoids materializing unrelated event text.
- `tool_text` formats and splits canonical tool text using the recorded Unicode-scalar boundary; all event readers share it.
- Explorer `with_freshness` supplies the same pending-source count as terminal queries.

The private block tables additionally store `all_ascii`, determined from original
text before normalization. It permits an attainable all-terms span ceiling;
Unicode blocks keep the universal bound. Prose phrase candidates use positional
term order before canonical verification. These derived-table changes require a
fresh index and do not change the stable row or result types.

Bounded candidate retrieval uses the prose primary key and a private integer-ID
projection of tool events. This prevents broad metadata indexes from being
rescanned for each block; the stable `prose`, `tools`, and `events` read columns
and the `query::search` signature are unchanged. The semantic merge adds a
trigger-maintained reference count in private state; keyword readers do not
scan the semantic reference table on startup.
