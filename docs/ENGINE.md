# 0.5.0 keyword engine

## Merged binary baseline (V1)

The binary from merge `0e4aa605` completed a full build in **184.62 s** at
load **67.07 / 66.91 / 56.03**. Its database was **2,295,169,024 bytes**,
with 130,648 visible prose rows and 1,078,010 tool rows. The storage target
is met; the 60-second build target is not. Time in milliseconds: parsing
36,051; source writes 125,979 (row/event writes 35,503, parser graph 30,709,
FTS 35,952, canonicalization 8,304, dictionary 4,113, secondary indexes 2,606);
bounds 13,027; statistics 8,308; commit 521. The next build experiment batches
event inserts; graph conversion and FTS construction remain the largest
unresolved costs after that change.

Every completed keyword case below has seven fresh processes, no discarded
samples. Load is the median 1/5/15-minute triple sampled before each process.
The 0.4.9 rare case exited 2; current rare hits reflect history drift.

| Case | 0.4.9 ms | Packed V7 ms | Merged V1 ms | Load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| release | 457.09 | 216.60 | 245.05 | 36.00 / 57.45 / 54.66 |
| error | 347.78 | 89.10 | 85.82 | 36.00 / 57.45 / 54.66 |
| natural | 270.82 | 262.44 | 289.79 | 35.36 / 56.96 / 54.50 |
| flaky | 220.43 | 77.28 | 101.07 | 35.36 / 56.96 / 54.50 |
| rare | 269.63 | 49.48 | 123.38 | 35.36 / 56.96 / 54.50 |
| short | 408.58 | 166.58 | 240.03 | 35.36 / 56.96 / 54.50 |
| regex | 309.89 | 60.99 | 108.31 | 39.97 / 57.56 / 54.73 |
| list | 862.46 | 292.37 | 379.44 | 39.97 / 57.56 / 54.73 |
| json | 505.40 | 326.47 | 289.37 | 44.62 / 58.23 / 54.98 |
| count | 462.77 | 85.13 | 123.98 | 44.62 / 58.23 / 54.98 |
| rare_hit | 174.93 | 57.36 | 87.33 | 44.62 / 58.23 / 54.98 |
| rare_hit_pi_filtered | 173.82 | 98.13 | 100.23 | 57.66 / 60.51 / 55.82 |
| short_filtered | 2734.58 | 52.79 | 138.45 | 57.44 / 60.42 / 55.82 |
| compact_release | 508.09 | 282.30 | 227.04 | 44.62 / 58.23 / 54.98 |
| compact_error | 347.83 | 91.80 | 90.73 | 46.57 / 58.41 / 55.06 |
| compact_natural | 362.40 | 279.23 | 286.05 | 46.57 / 58.41 / 55.06 |
| compact_flaky | 293.81 | 47.13 | 94.86 | 46.57 / 58.41 / 55.06 |

V1 census was **63.87 ms**, with one observed phase split of 0.15 ms opening,
15.36 ms directory work, 35.03 ms stat work, and 8.40 ms comparison. Nineteen
sources were pending in that sample. Natural language remains slower than
0.4.9 and most cases miss the 0.5 target. These are baseline failures, not
accepted budgets.

Navigation was interrupted after a recall process exceeded one minute.
Process sampling showed repeated temporary-file writes from its event read;
the outer join to the tool view materialized far more rows than the window
needed. The next version reads event metadata and its referenced blob directly
through the event view, with an index on assigned position. There is no
completed V1 navigation median. The completed keyword samples remain in
[the aggregate report](measurements/real-query-merged-v1.json).

Two smaller probes tested candidate access. A 23.29 MB covering metadata index
reduced the sampled release metadata lookup from 4.44 to 2.82 ms and natural
from 2.38 to 1.11 ms: too small to explain the observed latency, so it is not
added to the product. Removing redundant containing-substring alternatives
from FTS reduced release candidate enumeration from 10.47 to 4.35 ms and
natural from 10.89 to 6.14 ms, with the same 629 and 212 candidate counts.
These are SQL microbenchmarks, not claimed full-command speedups.

The next planner experiment also removes a cache eligibility discontinuity:
the tool term estimates for `last`, `release`, and `drafting` were about 40k,
51k, and 93k. The previous 32,768 cutoff reopened a large word-posting query
in every block. Two selective required groups can instead intersect once;
the remaining terms retain canonical verification. No retrieval budget is
relaxed.


The command integration now uses the committed-snapshot engine throughout. A stat
census overlaps retrieval, one `__work` task list refreshes in the background, and
explicit indexing drains work in the foreground. Caller predicates run before
ranking in both lanes; postcompact refreshes only the caller's indexed sources.
Event readers consume stored timestamps, turns, visibility, and multiline input
boundaries. Integration validation passes 264 tests with zero failures or skips.
This merge makes no new latency claim; the packed V7 measurements below remain
the last measured engine baseline until the merged-binary campaign completes.
The strengthened dense generator now checks exact top-40 handles across ten
query shapes, with sparse older matches and varied boundaries and timestamps.


**Performance status:** several SPEC latency budgets remain unmet. The gate is unchanged; the final measurements, diagnosed costs and work needed to close the gaps are below.

This is the Rust foundation, not completion of the later command or semantic slices. `PORTING.md` lists executable commands. An unsupported command's exit 2 is not a successful zero result or a latency win.

## Packed storage follow-up

The fixed storage sample contains 99 sources, 475,378,004 source bytes, 2,619 prose rows and 30,541 tool rows. The original V9 sample database occupied 132,698,112 bytes. Integer event rows and shared text reduced it to 96,833,536 bytes (storage V1); packed ASCII bounds and event references reduced it to 76,582,912 bytes (V2). The V3 layout occupies **62,656,512 bytes**. Its conservative full-history projection is **2,320,276,161 bytes**, or 2,784,331,394 bytes with 20% temporary space. The projection retains the measured full V8 objects where unchanged, scales new tool/event objects by the larger physical-row ratio, parser pages by source bytes, cursors by source count, and the changed prose index by its fixed-sample size ratio. It is a projection, not a completed full-history measurement.

| Sample object | V2 bytes | V3 bytes |
|---|---:|---:|
| Tool text blobs | 18,944,000 | 14,454,784 |
| Parser graph | 6,455,296 | 4,517,888 |
| Parser cursors | 475,136 | 475,136 |
| Typed event rows | 4,132,864 | 4,132,864 |
| Event identity index | 2,523,136 | 2,523,136 |
| Prose FTS data | 15,343,616 | 7,983,104 |
| Gram postings | 1,449,984 | 1,449,984 |
| Block gram keys | 393,216 | 393,216 |

`tools` and `events` remain read-compatible views. Their private storage uses integer session/symbol keys and immutable native event revisions. Tool text is stored once per content digest and shared with parser branches. Rewriting or deleting a source collects unreferenced blobs; leaving and restoring branches retains their exact earlier tool results. Parser pages store compact entry/parent/order tuples, ignored-node ancestry without ignored payload, and references to typed event text. They no longer duplicate full tool payloads or the complete parsed native graph. The cursor retains the parser state and exact-prefix digest, without duplicate edge fragments.

The tool codec measurement round-tripped all 30,541 sample rows (23,127,335 uncompressed bytes). LZ4 used 16,956,839 bytes and decoded the entire sample in 17.6 ms median; DEFLATE level 3 used 12,608,556 bytes and decoded in 307.7 ms. Brotli level 4 used 12,534,672 bytes but encoded in 1,321 ms and decoded in 365 ms, versus DEFLATE's 554 ms encoding. Brotli is a benchmark-only dependency and is not used by the product. Runtime selection keeps small/unhelpful cases in LZ4 and uses DEFLATE when it saves at least 16 bytes. Full query measurements must establish the cost of decoding only winning rows. Graph pages use DEFLATE level 6 with parallel bucket compression. All codec trials retain their numeric load observations in `storage-codecs-*-v2.json`.

The duplicate joined-text prose FTS field is removed. The normal positional trigram field supplies candidates; canonical phrase verification retains punctuation, case folding and Unicode boundary behavior. Block bounds pack only ASCII alphanumeric grams into integer keys and compressed posting deltas. Unrepresented characters retain a conservative bound and complete matching, including Unicode-only blocks. The regression checks 700 Unicode rows, a 40-row page, and the exact count.

Storage V3's sample first build took 9,471.70 ms at load 71.73 / 61.35 / 43.28. Its internal parse/write/bounds/statistics times were 1,429.71 / 6,583.27 / 548.04 / 455.62 ms; within writes, FTS took 1,928.56 ms and graph persistence 2,255.58 ms. This does not satisfy or predict the full-history 60-second target. The typed event/blob writes are now included in `row_diff_ms`; earlier V1/V2 subphase counters omitted that work, though their total write and process times included it. Reports `storage-v1.json`, `storage-v2.json`, and `storage-v3.json` retain object accounting and projections; adjacent query files retain aggregate samples only. The original copied inputs are read-only and superseded sample indexes were deleted before the full-history run.

### Full-history storage V3

The full build verified the size projection: **2,153,697,280 bytes**, compared with V8's 5,152,804,864 bytes (58.2% smaller). It holds 130,592 visible prose rows and 1,075,371 visible tool rows in 7,675 sessions; inputs grew since V8. The run read 24,004,472,482 bytes from 10,465 parsed sources, with one source error retained in the private diagnostics. It completed in **303,885.23 ms**, starting at load **61.99 / 59.97 / 43.41**. This exceeds the 60,000 ms target. The disk monitor observed 2,204,899,917 peak data bytes and 178,138,628,096 minimum free bytes. No superseded full index was retained.

| Object group | V8 bytes | Storage V3 bytes |
|---|---:|---:|
| Tool text and digest key | 1,367,445,504 | 509,526,016 |
| Events and lookup indexes | 834,797,568 | 411,815,936 |
| Parser graph and indexes | 1,092,403,200 | 288,071,680 |
| Parser cursors | 180,789,248 | 48,332,800 |
| Gram postings and block keys | 507,617,280 | 51,818,496 |
| Prose full-text index | 357,273,600 | 186,884,096 |
| Tool full-text index | 307,597,312 | 308,756,480 |

The V3 write breakdown was graph persistence 78,317.54 ms, row/event/blob handling 55,659.68 ms, FTS 48,854.44 ms, replay reconciliation 31,942.70 ms, word dictionary 5,341.82 ms and secondary indexes 1,928.66 ms. Parsing took 39,928.72 ms, block bounds 18,815.04 ms, statistics 7,950.99 ms and commit 307.91 ms. Write subdivisions overlap their enclosing write total. Per-adapter source counts, overlapping parse wall times, source-parse sums and write times are retained in the aggregate JSON. The next changes target repeated graph SQL/decompression and substring hashing during bounds construction; no faster full build is claimed from those plans.

| Adapter | Sources | Source parse sum ms | Adapter parse wall ms | Source writes ms |
|---|---:|---:|---:|---:|
| Claude | 46 | 413.86 | 39,918.53 | 87.58 |
| Codex | 29 | 1,383.36 | 39,915.87 | 341.25 |
| pi/OMP | 10,390 | 301,922.30 | 39,923.65 | 147,792.50 |

Adapter wall times include shared-pool scheduling and overlap. Source-parse sums add per-source observations and are not the 39,928.72 ms elapsed parse phase.

Seven fresh processes per case, with no discarded warm-up:

| Case | 0.4.9 ms | V8 ms | Storage V3 ms | V3 load 1/5/15 min |
|---|---:|---:|---:|---|
| release | 457.09 | 191.06 | 247.32 | 28.12 / 42.64 / 40.73 |
| error | 347.78 | 68.03 | 88.04 | 28.12 / 42.64 / 40.73 |
| natural | 270.82 | 701.95 | 395.30 | 29.15 / 42.62 / 40.73 |
| flaky | 220.43 | 90.81 | 123.29 | 29.15 / 42.62 / 40.73 |
| rare | 269.63 (exit 2) | 41.61 | 65.73 | 29.15 / 42.62 / 40.73 |
| short | 408.58 | 153.08 | 179.01 | 29.15 / 42.62 / 40.73 |
| regex | 309.89 | 60.95 | 84.81 | 28.66 / 42.29 / 40.62 |
| list | 862.46 | 339.26 | 473.76 | 28.66 / 42.29 / 40.62 |
| json | 505.40 | 229.04 | 305.60 | 28.21 / 41.97 / 40.52 |
| count | 462.77 | 89.11 | 113.53 | 28.21 / 41.97 / 40.52 |
| rare_hit | 174.93 | 45.59 | 67.40 | 28.21 / 41.97 / 40.52 |
| compact_release | 508.09 | 224.64 | 289.04 | 28.21 / 41.97 / 40.52 |
| compact_error | 347.83 | 75.88 | 88.63 | 28.91 / 41.89 / 40.50 |
| compact_natural | 362.40 | 767.28 | 598.70 | 28.91 / 41.89 / 40.50 |
| compact_flaky | 293.81 | 89.37 | 132.67 | 27.64 / 41.41 / 40.34 |
| rare_hit_pi_filtered | 173.82 | 865.09 | 90.73 | 27.64 / 41.41 / 40.34 |
| short_filtered | 2734.58 | 47.21 | 64.22 | 27.90 / 41.23 / 40.28 |

Census was 62.70 ms median and still misses 15 ms. The storage target passes; natural and compact-natural still exceed 0.4.9, and every listed case remains above its tighter SPEC target. Higher load prevents attributing V8-to-V3 latency differences solely to code. `real-storage-v3-queries.json` retains all sample durations, loads, exit-code counts and aggregate work. `real-storage-v3.json` retains every SQLite object size. Unsupported pre-merge command cases exited 2 and are not counted as successful performance results.

## Engine checkpoint before command integration

The latest full database contains **2,279,034,880 bytes**, plus 11,189,952 bytes of WAL after the query campaign. `real-storage-packed-v6.json` records the complete object accounting. Storage is below 2.5 GB despite source growth: 130,627 visible prose rows, 1,076,916 visible tool rows and 7,681 sessions. All retained measurement files contain aggregate values and allowlisted query arguments only; raw diagnostics remain in ignored scratch storage.

| Object group | V8 bytes | Current bytes |
|---|---:|---:|
| Tool text and digest key | 1,367,445,504 | 523,837,440 |
| Typed events, session/symbol dictionaries and event indexes | 834,797,568 | 407,461,888 |
| Parser state and graph | 1,273,192,448 | 363,667,456 |
| Gram postings and block keys | 507,617,280 | 138,641,408 |
| Prose full-text index | 357,273,600 | 187,207,680 |
| Tool full-text index | 307,597,312 | 309,305,344 |

The block postings now retain the maximum timestamp and role weight of rows that actually contain each gram. The clock and tie-time ceilings use these postings; recent non-matching rows cannot inflate an old-match block's ceiling. Phrase occurrence ceilings use the original literal tokens, so overlapping plural alternatives cannot double-count one occurrence. Mandatory-separator bounds use existing cross-token grams. The sparse-old regression checks exact top-40 positions among newer non-matches and possessive/interior forms at both 2,048 and 8,192 rows. The repeated-phrase regression also checks SQLite work, not only hydrated rows.

The latest initial build took **167,212.01 ms**, starting at load **47.02 / 66.10 / 65.94**. Parsing took 24,364.89 ms, row/event writes 36,285.95 ms, graph persistence 26,344.59 ms, FTS construction 37,163.08 ms, replay reconciliation 8,433.79 ms, bounds 10,839.22 ms, statistics 7,787.84 ms and commit 206.81 ms. The first-build obsolete-row scan is gone. Tool and graph DEFLATE levels are now 1; repeated blob digests reuse their stored body before attempting compression. The 60-second target still fails. The measured next steps are batching the remaining per-event/blob SQL calls and overlapping bounded parsing with writes; the current collector retains all parsed source batches before writing. FTS tokenization and graph preparation also remain material costs. No build-budget increase is proposed.

The following are seven fresh processes per case using the isolated full-history database, with all individual durations and load readings retained in `real-query-storage-v7.json`. The baseline has no recorded load. These are pre-integration keyword results; unsupported command timings are excluded.

| Case | 0.4.9 ms | 39b78a24 ms | Current ms | Load 1/5/15 min |
|---|---:|---:|---:|---|
| release | 457.09 | 4888.84 | 216.60 | 27.10 / 39.01 / 52.48 |
| error | 347.78 | 8747.58 | 89.10 | 27.10 / 39.01 / 52.48 |
| natural | 270.82 | 97442.06 | 262.44 | 26.69 / 38.73 / 52.30 |
| flaky | 220.43 | 5356.16 | 77.28 | 26.69 / 38.73 / 52.30 |
| rare | 269.63 (exit 2) | 2031.27 | 49.48 | 26.69 / 38.73 / 52.30 |
| short | 408.58 | 16582.95 | 166.58 | 26.69 / 38.73 / 52.30 |
| regex | 309.89 | 4009.84 | 60.99 | 26.15 / 38.42 / 52.11 |
| list | 862.46 | 14171.59 | 292.37 | 26.15 / 38.42 / 52.11 |
| json | 505.40 | 5077.19 | 326.47 | 26.15 / 38.42 / 52.11 |
| count | 462.77 | 59237.10 | 85.13 | 25.98 / 38.18 / 51.95 |
| rare_hit | 174.93 | 3876.58 | 57.36 | 25.98 / 38.18 / 51.95 |
| compact_release | 508.09 | 5147.67 | 282.30 | 25.98 / 38.18 / 51.95 |
| compact_error | 347.83 | 6443.22 | 91.80 | 25.98 / 38.18 / 51.95 |
| compact_natural | 362.40 | — | 279.23 | 25.34 / 37.84 / 51.75 |
| compact_flaky | 293.81 | — | 47.13 | 25.34 / 37.84 / 51.75 |
| rare_hit_pi_filtered | 173.82 | — | 98.13 | 25.34 / 37.84 / 51.75 |
| short_filtered | 2734.58 | — | 52.79 | 24.58 / 37.27 / 51.38 |

The current keyword values beat the successful 0.4.9 values shown, but the SPEC's tighter targets remain unmet. Census was 124.55 ms median. A full-process count was 85.13 ms, below its 400 ms target; it does not hydrate every matching tool row. The synthetic-looking rare token now appears in the live inputs, so its exit status is 0; the source snapshot has grown during development. This is recorded rather than forcing the original zero-result assumption.

Query-only profiling separates these failures: release used 152.78 ms median in SQLite, 2.82 ms hydration and 10.74 ms scoring; natural language used 158.21 / 8.09 / 27.57 ms. Short-query scoring used 86.66 ms. The SQLite figure includes repeated candidate/metadata cursor work. Selective prose intersections now buffer a complete small posting stream after the first block fails to settle the page; common phrases can stop before that allocation. High-frequency single terms use the existing bounded row scan. A certified aligned ASCII occurrence can establish the maximum boundary quality without preparing unrelated Unicode text; the Unicode oracle checks the shortcut around combining marks, ZWJ and apostrophes.

Two paired cache experiments, with rotated order and seven processes per setting, tested 32 MiB SQLite cache and 256 MiB/full-file mapping. They did not improve the main query cases consistently, so product defaults are unchanged. `storage-v5-cache-probe.json` and `storage-v7-mmap-probe.json` retain these results. Bigger caches are not represented as a latency fix.

## Redesign after the 39b78a24 measurements

The measurements below are the before baseline, not claims about the redesigned engine. The current work moves source parsing to a detached task list, caches directory listings, uses positional trigram indexes and compressed block posting lists, and adds bounded coverage/regex retrieval plus caller predicates. Seven-sample real-history measurements with per-sample host load will be recorded before any speedup claim. Resumable source parsing and changed-row family statistics are implemented. See `ENGINE-API.md` for additive integration fields and entry points.

## Disk-limited redesign measurements

The first positional-tool real build was stopped and its partial database removed when the shared-disk constraint was tightened. It produced no completed query samples (`redesign-disk-stop.json`). The subsequent sample selected 338 source files across file-size groups: 830,668,818 source bytes, 4,071 visible prose rows and 57,105 visible tool rows. It built in 51,250.69 ms at load averages 282.94 / 216.07 / 321.91. The sample database was 861,433,856 bytes. Its parser graph alone used 557,711,360 bytes: ignored native payloads were being retained in graph nodes. The original positional-tool projection therefore does not fit the disk reserve (`tool-layout-projection.json`). That rejected layout was not built again. The compact graph and word-dictionary layout below supersede this projection.

A systematic 28,553-row tool sample compared candidate indexes. Full positional trigrams used 67,043,328 bytes; word positions used 10,866,688 bytes in the initial component run. Word-only matching is insufficient: `error` had 2,095 matches versus 3,780 substring matches, and it missed all seven interior `000d` matches. Adding a trigram dictionary of complete indexed words recovered all measured substring counts exactly. The second component run used 13,516,800 bytes including that dictionary, with 4,001,792 bytes attributed to dictionary objects. It expanded 465 / 106 / 48 / 5 word alternatives for the tested baseline-query terms. `tool-layout-components.json` and `tool-layout-components-v2.json` retain both runs; they are component timings, not full-process latency claims.

The revised tool candidate path uses word positions plus this dictionary for ASCII alphanumeric terms; other literals retain verified block scanning. The normal text and its full substring semantics remain available. It does not discard the middle of tool output: the sampled head/tail alternative saved almost no space because adapter summaries were already capped, and it lost observed matches. Parser nodes now retain only fields consumed by the parser; native tool-input summaries and original length counters survive. The next sample must measure the new complete schema and demonstrate space for the database, temporary work and the 2 GiB reserve before a full-history build.

### Fixed-sample follow-up

The 339-source follow-up keeps 4,140 visible prose rows and 57,588 tool rows. Its first build fell from 25,940.38 ms to 15,555.30 ms after bulk row insertion, deferred FTS construction and deferred secondary indexes. The latter run recorded load averages 51.87 / 67.82 / 112.60. Its measured phases were parsing 2,389.08 ms, writes 6,893.39 ms, block bounds 3,949.57 ms, family statistics 1,038.83 ms and commit 947.81 ms. Within writes, row handling took 509.01 ms, FTS construction 3,998.79 ms, parser chunks 849.34 ms, secondary indexes 371.07 ms, canonical reconciliation 67.28 ms and the word dictionary 460.94 ms. `sample-build-v3.json` records this sample and its conservative size projection. These are sample measurements; the full 60-second build target is not established by extrapolation.

The same sample's seven-process query campaign is retained in `layout-sample-queries-v2.json` and `layout-sample-queries-v3.json`, including every load observation. Selecting the rarest tool term reduced the natural-language median from 6,656.38 ms to 1,057.54 ms. Forcing the posting stream before metadata lookups reduced the filtered count from 7,717.54 ms to 64.66 ms. Most keyword targets still fail on the sample. The next iteration removed per-ASCII-grapheme normalization allocations, cached exact ASCII Unicode properties, and used the compact boundary scorer for phrase hits. Its fixed-sample first build took 11,538.87 ms, with FTS construction at 2,974.81 ms and block bounds at 2,170.95 ms (load 20.93 / 36.32 / 80.84). Seven-process sample medians were release 205.30 ms, error 102.26 ms, natural language 685.71 ms, short 148.88 ms, JSON 216.03 ms and count 40.58 ms. These remain failures of the keyword budgets. `sample-build-v4.json` and `layout-sample-queries-v4.json` retain the measurements; full-history validation remains required.

### Segment and boundary follow-up

The same sample was profiled in seven fresh processes per case, with every host-load observation retained. The separator-safe ASCII boundary path reduced the `error` component median from 98.9 to 45.9 ms and `hi` from 146 to 62.3 ms. Compacting the sample's 9 prose / 11 tool / 3 dictionary segments to one each took 1,861.11 ms and changed the database from 246,210,560 to 250,294,272 bytes (including free pages). On that copy, release retrieval fell from 195.0 to 85.2 ms, natural language from 675.0 to 239.2 ms, and count from 37.2 to 21.5 ms. The short-query run varied in off-CPU/plan time despite identical examined rows; both runs are retained rather than selecting the faster sample. These are query-component measurements, excluding CLI census and rendering: `query-profile-v4.json`, `query-profile-v5.json`, `query-profile-v5-optimized.json`, and `fts-optimize-components.json`.

Initial FTS construction now raises the insertion hash limit, defers automatic merging, then optimizes each tree and restores ordinary incremental settings. In the 28,794-tool-row component experiment, word-index construction took 316.73 ms with these settings versus 442.70 ms before; full trigram construction took 3,771.80 versus 3,567.22 ms. This choice trades a small one-time trigram build cost for compact posting streams during subsequent reads. The word dictionary preserves the measured substring counts; `merge-layout-components.json` records both runs and their loads. It is not evidence of a full-history speedup.

The compact complete sample projects a 5,618,538,450-byte real database, or 6,742,246,140 bytes with a conservative 20% temporary-space allowance. The full run is guarded by the shared-disk reserve and the worktree budget; superseded sample databases are removed first.


## Full-history diagnostic after segment compaction

The first completed redesigned build (engine `164da469`) took **461,011.26 ms** at load averages **11.65 / 16.92 / 46.10**. It parsed 23,933,489,701 input bytes from 10,437 sources and emitted 1,486,660 source rows before replay selection. Parsing took 21,840.39 ms; source writes 62,332.45 ms; canonical reconciliation 240,447.25 ms; FTS construction 71,624.97 ms; block bounds 39,689.67 ms; family statistics 12,746.52 ms; commit 572.16 ms. Row handling and node-chunk timings are subdivisions of source writes. Full details, including per-adapter work, are in `measurements/real-build-v6.json`. The 60-second build budget still fails.

After the completed diagnostic cases, the database occupied 5,162,680,320 bytes, with another 144,772,712 bytes in WAL. It held 130,441 visible prose rows and 1,071,580 tool rows in 7,649 sessions. Tool FTS data used 292,761,600 bytes; prose FTS data used 354,902,016 bytes. `real-storage-v6.json` accounts for the other objects. This is the compact word-position/dictionary layout; the larger full-tool-trigram layout was rejected before this build.

Seven-process medians included release 426.61 ms, error 130.64 ms, natural language 13,509.46 ms, short 238.41 ms, list 3,388.95 ms, JSON 1,475.60 ms and count 913.46 ms. The stat-only census median was 49.90 ms; its final internal observation was 37.661 ms. These fail the requested budgets. Every completed sample and load observation remains in `real-query-v6.json.events.jsonl`. The diagnostic was stopped during the filtered list case after finding a planner defect; unfinished cases have no reported median. The later filtered cases overlapped the next release build, so they are retained diagnostics, not the final comparison campaign. The diagnostic index was deleted, freeing 5,307,747,957 bytes when shared free space fell.

Two root causes were identified for the next iteration. First, event turn remapping chose a session-wide primary-key scan instead of the existing native-turn index. On a 27,020-event scope, a seven-sample read-only component probe returned the same four rows in 30.343 ms through that plan and 0.102 ms through the native-turn index (load 23.62 / 26.89 / 32.74; `event-lookup-v7.json`). Second, the Codex/14-day tool scope contained 4,135 rows, just above the fixed 4,096-row planner threshold, which selected repeated global posting searches. The next planner judges metadata selectivity relative to lane size. These component findings do not establish a full-build or CLI speedup; those require the next complete campaign.

## Complete V8 diagnostic campaign

The complete seven-process V8 campaign used the frozen binaries in `measurements/engine-v8-binaries.json`. These are intermediate results, retained before the document-frequency and incremental-write corrections. There were no discarded query samples. Load columns below are the per-case median of the recorded 1/5/15-minute `uptime` observations; the JSON retains all seven observations. The 0.4.9 numbers come from the supplied baseline and have no recorded host load. An exit-2 baseline is a command failure, not a latency success.

| Case | 0.4.9 ms | Before redesign ms | Final ms | Load 1/5/15 min |
|---|---:|---:|---:|---|
| release | 457.09 | 4888.84 | 191.06 | 13.72 / 25.18 / 37.62 |
| error | 347.78 | 8747.58 | 68.03 | 13.72 / 25.18 / 37.62 |
| natural | 270.82 | 97442.06 | 701.95 | 13.42 / 24.93 / 37.46 |
| flaky | 220.43 | 5356.16 | 90.81 | 13.42 / 24.93 / 37.46 |
| rare | 269.63 (exit 2) | 2031.27 | 41.61 | 13.42 / 24.93 / 37.46 |
| short | 408.58 | 16582.95 | 153.08 | 14.99 / 25.06 / 37.43 |
| regex | 309.89 | 4009.84 | 60.95 | 14.99 / 25.06 / 37.43 |
| list | 862.46 | 14171.59 | 339.26 | 14.99 / 25.06 / 37.43 |
| json | 505.40 | 5077.19 | 229.04 | 14.99 / 25.06 / 37.43 |
| count | 462.77 | 59237.10 | 89.11 | 14.51 / 24.79 / 37.26 |
| rare_hit | 174.93 | 3876.58 | 45.59 | 14.51 / 24.79 / 37.26 |
| rare_hit_pi_filtered | 173.82 | — | 865.09 | 13.60 / 24.26 / 36.93 |
| short_filtered | 2734.58 | — | 47.21 | 12.75 / 23.90 / 36.73 |
| error_filtered | 601.61 | — | 60.21 | 12.75 / 23.90 / 36.73 |
| compact_release | 508.09 | 5147.67 | 224.64 | 14.51 / 24.79 / 37.26 |
| compact_error | 347.83 | 6443.22 | 75.88 | 14.51 / 24.79 / 37.26 |
| compact_natural | 362.40 | — | 767.28 | 13.83 / 24.48 / 37.08 |
| compact_flaky | 293.81 | — | 89.37 | 13.83 / 24.48 / 37.08 |

V8 built in 188,365.31 ms at load 14.11 / 34.51 / 43.33. It read 24,030,515,506 source bytes and stored 130,469 visible prose rows, 1,071,908 tool rows and 7,649 sessions. The database was 5,152,804,864 bytes, plus 36,383,752 bytes of WAL. The guarded campaign reached a minimum 5,679,865,856 free bytes. Its index was removed under both OS locks, freeing 5,189,286,933 bytes. The earlier V7 full campaign is also retained; its first build took 194,938 ms at much higher load, 92.62 / 80.76 / 55.02. These observations do not isolate code changes from host contention.

The two V8 synthetic dense campaigns failed the unchanged gate. At 1M / 2M rows, first builds took 68,244 / 142,720 ms; census 19.18 / 26.92 ms; one-session refresh 102.06 / 169.11 ms; dense search 146.94 / 286.30 ms (p95 153.25 / 324.52). The 1M search still examined only 576 rows in two blocks. This ruled out a failure to stop candidate scoring: FTS vocabulary counts in planning were traversing the common posting lists. Seven read-only component samples on the same 2M index measured those five frequency lookups at 169.70 ms versus 0.066 ms through a materialized counter table, with identical document counts. Building that table took 3,606.55 ms. `prose-frequency-components-v9.json` records host SQLite 3.53.4 and every load observation; final bundled-SQLite runs are required to establish the product speedup.

The 1M monitor encountered a file-removal race after the benchmark had completed and deleted its temporary corpus. The completed benchmark report and failed gate are retained; no peak/minimum monitor report is claimed for that run. The monitor now tolerates removal during its size walk. The 2M monitor completed, recording 3,102,059,890 peak data bytes and 8,417,472,512 minimum free bytes.

## V9 checkpoint before storage work

The materialized frequency cache restores bounded dense-query startup. On 1M rows, twenty fresh-process samples gave 26.44 ms median and 30.87 ms p95; retrieval with `--no-auto` took 14.94 ms. First construction (87,651.40 ms), census (21.27 ms) and incremental refresh (72.69 ms) still fail the unchanged gates. No full V9 real-history build was attempted: the restarted host has insufficient free disk for the V8-size database plus the mandatory reserve. The next step is storage reduction before a fresh real-history campaign.

| Case | Median ms | Load 1/5/15 min |
|---|---:|---|
| first_index | 87651.40 | 12.35 / 14.86 / 23.21 |
| no_change_census | 21.27 | 20.07 / 16.30 / 22.89 |
| one_session_refresh | 72.69 | 20.07 / 16.30 / 22.89 |
| old_dense | 26.44 | 19.91 / 16.30 / 22.87 |
| old_dense_no_auto | 14.94 | 19.75 / 16.29 / 22.85 |
| version_control | 8.39 | 19.75 / 16.29 / 22.85 |

`measurements/dense-v9-1000000.json` retains all aggregate samples and loads. The full command correctly exited nonzero for the three missed gates. The successful query gate is not represented as a successful complete benchmark. Initial statistics were parallelized, but the measured total increased relative to V8 under different host load; no first-build speedup is claimed.

## Archived foundation measurements

The remainder records earlier engine iterations. Physical-layout descriptions in this archived section describe those measurements, not the disk-limited redesign above.

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

Committed reports retain aggregate timings, exit-code counts, corpus counts, index sizes and allowlisted baseline query arguments. Raw samples, plans and diagnostics stay under ignored `scratch/private-measurements/`. Reports with `before-source-fix` in the name are retained diagnostic runs, not the final validation campaign. Adjacent aggregate `.events.jsonl` files preserve completed case summaries even if a process is interrupted. The 10k smoke and 100k mixed campaigns each use one first-build sample and respectively three/five fresh-process samples per query. The 1M/2M dense campaigns each use one build and twenty samples per query. First-build figures are single observations, not invented medians of repeated builds. The tool supports repeated first builds via `--build-runs` (defaults to `--runs`).


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


## Measurement privacy

Committed real-history evidence contains aggregates and baseline query arguments only. Session references, source paths, source-derived text and raw process/SQL diagnostics are kept in ignored scratch storage. `bench/validate_repo_privacy.py --worktree` checks the repository snapshot. The first redesign build was interrupted before completion to relocate its raw output and incorporate the parser checkpoint schema; its incomplete duration is not a completed build result.


### Prefix verification constraint

An interior overwrite followed by an append left both 4 KiB edge guards unchanged on a 256 MiB fixture. SHA-256 verification of the prior prefix detected it. Seven warm measurements took 261.27 ms median (223.08–398.73 ms); aggregate evidence is `measurements/source-guard-cost.json`. This is a component experiment using the host SHA-256 implementation, not a CLI latency claim.

The checkpoint reader therefore verifies the old prefix before resuming. Parsing is proportional to appended bytes; exact arbitrary-rewrite detection still requires reading prior bytes. The 50 ms large-file target cannot be inferred from bounded parsing alone. Trusting the native append-only contract with edge guards would remove that read but would relax detection of a simultaneous interior rewrite and append. The implementation retains verification until that tradeoff is explicitly changed.


### Active-session diagnosis

The initial checkpoint build measured a 47.52 ms median append refresh at 1,000 rows and 1,160.38 ms at 100,000 rows. The last large-session sample spent 920.93 ms rebuilding family statistics, 118.15 ms in row/session writes, 78.08 ms parsing and verifying the prefix, and 6.74 ms rebuilding block bounds. These synthetic diagnostics are retained as `active-before-1000.json` and `active-before-100000.json`. They are not presented as real-history speedups.

The subsequent implementation records row contributions before mutation, updates per-family token counts within the same transaction, and limits family resolution to affected sessions and descendants. Ordinary single-source appends keep their existing public positions and avoid a session-wide reconciliation scan. SHA-256 uses the pure-Rust ARM-accelerated backend in sha2 0.11; exact digest output is tested. Seven fresh-process samples after the change measured 32.61 ms for 1,000 rows and 49.08 ms for 100,000 rows. The large-session last sample spent 16.77 ms in prefix verification/parsing, 0.76 ms in writes, 6.65 ms in bounds, and 3.79 ms in statistics. It parsed 180 appended bytes while reading 21,495,518 bytes for exact prefix verification. The corresponding first builds were 210.07 ms and 15,494.69 ms; changed-row counters add first-build cost. These are synthetic measurements, not a real-history speedup claim. Every sample and its three numeric load averages is summarized in `active-after-1000.json` and `active-after-100000.json`.

### Storage V3 component diagnosis

Seven read-only fresh-process retrieval probes separate CLI census from retrieval. Release spent 197.86 ms median in SQLite, 3.01 ms hydrating text and 16.89 ms scoring. Natural language spent 392.18 / 2.11 / 28.59 ms in those phases. Tools-only natural language spent 18,567.46 ms in SQLite, 332.99 ms hydrating and 56.84 ms scoring, examining 24,725 rows across 1,594 blocks. The tools-only error case spent 1,310.40 ms in SQLite and 7.15 ms hydrating. These identify repeated candidate/metadata work, rather than decoding winning tool text, as the dominant retrieval cost. The complete numeric observations and per-sample loads are in `storage-v3-profile.json`.

The codec comparison used two copies of the same real-source sample index, alternating mixed-codec and all-LZ4 order across seven fresh processes with `--no-auto`. Mixed / LZ4 medians in ms were release 30.75 / 26.97, error 20.88 / 21.55, natural 67.05 / 47.50, short 50.27 / 50.50, tool error 26.11 / 22.35 and count 12.54 / 12.37. These are sample observations, not full-history codec speedups. Both copies preserve the same text and postings; the LZ4 copy was deleted after measurement. `storage-v3-codec-latency.json` retains paired durations, exit codes and load observations. The storage decision remains lossless mixed coding; the larger latency gaps must be closed in candidate traversal and initial construction.

### V4 diagnostic and work-bound corrections

The V4 full build occupied 2,149,715,968 bytes but took **526,333.47 ms**, starting at load 66.78 / 43.75 / 40.01. The observed load rose above 130 during this run. A single one-second stack sample was taken during construction; this is an instrumented diagnostic, not an isolated acceptance run. Parsing took 20,071.37 ms, row/event handling 45,471.00 ms, graph persistence 27,327.57 ms, FTS 44,523.69 ms, replay reconciliation 9,551.62 ms, bounds 8,443.65 ms and statistics 6,522.52 ms. The full report retains all phases and per-adapter counts in `real-build-storage-v4.json`.

The source-write total was 430,103.24 ms. Its roughly 357-second remainder exposed an initial-load regression: deferring the source lookup index made the inactive-event cleanup scan the accumulated event table for every newly inserted source. A first build has no obsolete contributions. Its source writer now returns before those diff and cleanup operations. A regression measures SQLite VM work for 128 versus 512 fresh sources with secondary indexes deferred; four times the input must remain below six times the work. This corrects the quadratic path without adding an index that the initial load does not need. A subsequent full build is required to quantify the correction.

The common-query work regression now also covers multiword phrases. It exposed another loose bound: a one-occurrence two-word row scores below 0.5 because its separating space increases the span, while the former block ceiling remained 0.5. Existing cross-token gram postings now prove when a zero-character join is impossible. Each proven mandatory separator contributes one character to the phrase's minimum span. Unknown or Unicode bridges retain the conservative ceiling. This makes the common multiword fixture stop without changing its winning rows or loosening its work assertion.

Metadata exclusion is now part of candidate filtering before totals and top-k selection, including the explicit coverage lane. A single best metadata hit is retained only as the documented fallback when no ordinary evidence exists. The regression places an ordinary result behind 80 newer metadata results and requires it to be returned with a one-row page.
