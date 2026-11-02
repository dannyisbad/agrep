# 0.5.0 keyword engine

**Performance status:** several SPEC latency budgets remain unmet. The gate is unchanged; the final measurements, diagnosed costs and work needed to close the gaps are below.

This is the Rust foundation, not completion of the later command or semantic slices. `PORTING.md` lists executable commands. An unsupported command's exit 2 is not a successful zero result or a latency win.

## Physical layout decision

Measured on this Mac with the release `bench --layout` tool, 20,000 deterministic mixed prose/tool documents (no network, one transaction per index). Raw evidence: `measurements/layout.json`. Times below are milliseconds, sizes are bytes. These are component measurements, not CLI latencies. Count timings measure FTS posting operations; the product also verifies canonical text, and its end-to-end counts below include that cost.

| Layout | Build ms | Bytes | `error` count ms | `release AND workflow` count ms | Interior `ork` hits |
|---|---:|---:|---:|---:|---:|
| FTS5 trigram, full detail | 1420.21 | 64,012,288 | 1.608 | 8.572 | 17,593 |
| FTS5 trigram, no detail | 755.10 | 27,467,776 | 0.892 | 3.006 | 17,593 |
| FTS5 unicode61, full detail | 245.96 | 26,431,488 | 0.303 | 0.654 | 0 |

Chosen after the end-to-end scale experiment: prose uses a full-detail trigram index with two fields (normal text and text with phrase separators removed); tools use a separate no-detail trigram index. All queries are verified against canonical text. Tool literals expand to ANDs of trigrams because no-detail FTS cannot directly search a token longer than three characters. The full-detail prose index can find a joined phrase directly, without hydrating every bag-of-words candidate. Word FTS is faster, but loses interior substring matches; a full tool-text fallback would move the cost into ordinary reads. Neither FTS trigram variant answers two-character terms by itself. Prose block bounds include one-, two-, and three-character grams, preserving short queries.

Tool text is LZ4-compressed in SQLite; prose remains directly readable. Tool events retain metadata separately without another copy of their text. Prose and tools have independent row IDs, FTS trees and filters. The reference tools-on default is preserved. `--who` scopes that exclude tools and `set tools off` avoid the tool index completely. Both lanes have independent block bounds, so strong prose results can rule out unseen tool rows without reading tool text. The early prototype reports marked below used prose-only defaults; final reports use tools on.

## Ranking and stopping

Each lane’s row IDs form blocks of 512. Each block records the maximum occurrence count of each gram, maximum timestamps/role weight, and a conservative best tie key. For a query term, the minimum of its constituent gram maxima bounds occurrences; alternative spellings are summed, which can overestimate but cannot underestimate. Phrase repetition is bounded by `1 - 2^-n`. Role/recency bounds include the user floor explicitly. Boundary factors never exceed one.

The phrase tier runs first; only a complete, correctly ranked phrase page can bypass the all-terms tier. Chat heads still merge both with their lane weights. A minimal covering-span ceiling avoids full boundary scoring for all-terms candidates that cannot beat the page. Blocks are visited by descending admissible ceiling. A full top-k page stops only when the next block cannot improve the actual page, including its deterministic tie order. This removes the old `0.35` recency-floor versus `0.175` one-occurrence plateau. Within a block, cheap literal matching and a second admissible ceiling avoid Unicode scoring for rows that cannot enter the page. There is no candidate-count budget or planted first-hit shortcut. Exact counts exhaust the eligible stream; ordinary pages disclose exact or measured-floor totals. The dense test checks known winners and requires early termination in a single large session.

The retained Unicode 16 scorer grades original-text boundaries after NFKC case folding. Per-family short-token contributions and aggregate n/s statistics are committed with row changes. Phrase, all-terms and labeled content-term fallback remain separate tiers. Fallback is limited to human/compact prose output; JSON, TSV, counts and `--lexical` keep strict matching. Explicit word/regex queries bypass boundary penalties. Regex uses fancy-regex's backtrack limit and safe required-literal prefilters; constructs without a provable literal use a verified scan.

Selective agent/session/date/model filters are measured with a capped metadata probe. Small scopes scan their filtered rows directly, so a short Codex query does not traverse the large pi corpus. Broad scopes retain the FTS/block route.

## Ingestion and consistency

Every discovery pass enumerates the adapter's source files and stats them in parallel. Directory entries supply file types without a redundant serial stat of every file. Source fingerprints include size, nanosecond mtime, filesystem change identity and SQLite WAL metadata. Only changed sources are parsed. Content digests avoid rewriting unchanged rows/FTS entries. Numeric source IDs avoid repeating long source paths in every message and index entry. Removed sources remove their contributions; failed parses retain last-good rows and record a source error. Native replay copies are reconciled to one canonical message (preferring complete replies and attributed metadata), tool calls are deduplicated by event identity, and colliding native turns receive deterministic public positions. Noncanonical source contributions remain in the same database for incremental recovery; only visible rows enter FTS and block bounds. SQLite triggers maintain canonical row counts in the same transaction. Parser diagnostics are stored, not repeated on each search.

Normal refresh/reindex writes use one SQLite WAL transaction under a permanent OS advisory lock. Readers take no writer lock and hold a SQLite read snapshot. An opportunistic second writer serves the committed snapshot; explicit indexing waits. For the first build only, the writer creates a private SQLite database in DELETE journal mode, commits it, switches it to WAL, closes it, and renames it into place. This avoids a multi-GB initial WAL and its equally large checkpoint copy. No reader is pointed at the private build. It is disposable after interruption; it is not a second authority or compatibility format.

A second diagnostic 1M build was stopped after 230,020.87 ms, before commit. A host SQLite plan probe selected `prose_scope(agent)` before the family lookup, making statistics updates scan the agent corpus per family. The final query starts with `sessions_family` and joins through `prose_session`; the plan and probe version are retained in `measurements/family-query-plan.txt` and `plan-probe-version.txt`. The failure is retained in `dense-initial-aborted.json`.

An initial diagnostic attempt was stopped after 459,668.75 ms, before commit, with a roughly 2.7 GB WAL. It did not produce a usable index. The subsequent changes removed repeated tool-event copies, replaced per-event linear turn lookup with a sorted lookup, cached hot SQL statements, removed a quadratic census membership test, and changed initial construction as above. Raw failure record: `measurements/initial-build-aborted.json`.

## Measurement protocol

`crates/agrep/examples/bench.rs` is a release-built Rust tool. It generates native pi session files under a temporary HOME, including side sessions, assistant replies, varied prose lengths, many capped tool outputs and arbitrary query occurrences. The approximate target shape is 1,166k searchable rows, roughly 89% tools, with substantially longer tool text. Its corpus summary records the actual ingested counts. Dense runs deliberately use one-occurrence old prose and deterministic ties at 1M and 2M rows. Fixtures separately cover every adapter and the recorded Gemini histories.

Each sample starts a new complete CLI process and captures output through exit. For even sample counts the tool reports the upper middle order statistic as median; p95 uses the nearest-rank definition. No warm-up is discarded. `--runs` controls repetitions; `--build-runs` can separately bound the expensive first-build campaign. All benchmark launches use `nice -n 10`. Process-cold does not mean an OS cache purge. Synthetic sources and indexes are deleted by the tool's temporary-directory scope. The owner's sources are read-only and the real index is isolated under this worktree's ignored `scratch/` directory.

`replay-query-plan.json` is a read-only diagnostic plan probe using host SQLite 3.53.4; it is not substituted for the bundled-SQLite process timings.

The shared 12-CPU Apple M2 Max host had substantial concurrent load during this work, including load averages above 100 during the final build (`measurements/host-load.txt`). These are observed full-process latencies on that host, not isolated hardware measurements; the budgets are unchanged and failures remain failures.

Budgets live in `perf-budgets.json`. `--check` fails after preserving the measurements if a budget is exceeded. CI has no retry or advisory performance job. Local macOS results do not establish Linux/Windows latency or behavior; the workflow runs those platforms separately.

The first, prose-only 100k mixed run with no-detail prose took 305.36 ms for `release workflow` and 437.36 ms for `flaky windows cache`. The phrase index/source-ID revision reduced these to 159.38 and 414.57 ms, with first build falling from 15,387.53 to 10,612.02 ms. These are diagnostic iterations, retained in `synthetic-100000-initial.json` and `synthetic-100000-phrase.json`; the final reports below include the final family handling. They are not discarded warm-ups. All-terms ranking still does substantial work on the dense synthetic language workload.

The later layout experiment (`layout-final.json`) measured the dual-field full-detail index at 130,912,256 bytes and 3,668.75 ms for the same 20k mixed documents. The extra index is restricted to the much smaller prose corpus in the product; the 89% tool corpus keeps no-detail postings. This buys a direct phrase access path rather than declaring the word-only index semantically equivalent.

## Measured results

The pre-planner campaign used `b0279c4f` (`measurements/pre-mixed-prefilter-binary.sha256`), including canonical native replay handling. Prior files named `before-source-fix` and `before-replay-fix` remain diagnostic evidence. The source-error correction scopes failures correctly and retries only changed files; native replay copies now select complete canonical rows and preserve source contributions for deletion recovery. The CLI also keeps the reference opaque-session codec and exact output framing. Measurements are not represented as a performance-gate pass.

Raw reports retain every sample, exit code, corpus count, index byte total, and query arguments. Reports with `before-source-fix` in the name are retained diagnostic runs, not the final validation campaign. Adjacent `.events.jsonl` files preserve completed cases even if a process is interrupted. The 10k smoke and 100k mixed campaigns each use one first-build sample and respectively three/five fresh-process samples per query. The 1M/2M dense campaigns each use one build and twenty samples per query. First-build figures are single observations, not invented medians of repeated builds. The tool supports repeated first builds via `--build-runs` (defaults to `--runs`).


### Prior synthetic measurements

The complete pre-fix runs remain in `mixed-final-before-source-fix.json`, `dense-1000000-before-source-fix.json` and `dense-2000000-before-source-fix.json`. They measured default dense medians of 48.80 / 81.99 ms and retrieval without census of 14.17 / 16.46 ms. They examined 577 / 641 rows in two blocks. Those are diagnostic observations; the fresh final campaign is reported below.

### Real-build diagnostic before the source-error fix

The first completed real build took **655,267.07 ms**, with 1,481,555 changed rows from 10,413 source files. Internal phases were parse 14,881.93 ms, writes 322,647.69 ms, bounds 307,737.96 ms, and family statistics 8,897.57 ms. It committed successfully. `real-first-build.json` preserves that observation. Its subsequent refresh campaign was stopped after identifying two correctness defects: an unchanged failed file was retried every time, and a single pi failure set failure metadata on every pi source. The final code retries changed sources only, scopes failure metadata to the failed source/dependency, and preserves unchanged last-good errors. The regression test covers a failed file alongside a valid sibling and a later change to the valid sibling. A fresh final build follows below; no corrupted diagnostic labels are presented as final coverage.

The same review found repeated reads of a source's first 16 KiB to extract cwd for each message. The final code reads that once per parsed source batch. The prior phase numbers include that redundant I/O.

The first build's read-only object accounting (`real-storage-before-source-fix.json`, host SQLite 3.53.4) measured tool text 1,185,759,232 bytes, tool gram bounds 947,826,688, their block index 721,670,144, tool FTS data 648,318,976, event metadata 512,077,824, and prose FTS data 425,824,256. These explain why splitting FTS alone did not shrink total storage: the exact stopping bounds cost 1.67 GB in that corpus. The process sample records a peak physical footprint of 8.5 GiB; the parsed source batches are still too large. Neither cost is hidden in a separate sidecar.


The completed canonical-replay build in `real-build-engine8d50.json` took 933,622.12 ms. A subsequent read-only probe found the per-session reply-shape lookup scanning the full shape table: five samples had a 14.78 ms median; an indexed temporary copy returned the same two rows in 0.003 ms. The final schema adds `reply_shapes_session` and the query uses it explicitly. `replay-shape-index.json` records the probe, its host SQLite version, and every sample; this component result is not presented as a full-build speedup. The final campaign also uses match-only verification for ordinary counts, retaining full classification for tier counts.


### Mixed-length query and all-terms planner correction

The complete pre-planner campaign remains in `real-history-before-mixed-prefilter.json`. It measured the natural-language query at **86,005.00 ms**, with compact output at **106,468.93 ms**. Any two-character term previously disabled the FTS prefilter for all terms. The planner now retains every indexable long-term clause in an AND query and verifies the short terms on those candidates. OR fallback still cannot drop short alternatives without changing results. A regression test checks both prose and tools, plus a nonmatching short term.

A second correction gives the all-terms phase its own finite score ceiling: all-terms scores have no phrase repetition penalty. The bound uses a maximum factor of one, and chat scoring applies the documented tier factor. A test requires a full strong prose page to rule out lower-weight tool blocks; the exhaustive-ranking equivalence test remains in place. The final query campaign below uses both corrections against the same real index. Its plain natural-language median was still 97,442.06 ms on the heavily loaded host, so the prefilter change alone is not a demonstrated latency solution. The no-detail tool lane still has to verify whether scattered term occurrences form a phrase before it can finish that tier.
