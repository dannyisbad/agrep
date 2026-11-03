# Semantic and keyword integration

Merge commit `1f94289e` combines the semantic performance branch with `f0ea6030`.
The semantic kernels, full input window and quality gates are retained alongside
the snapshot keyword engine and audit/security corrections.

## Conflict decisions

| File | Combined behavior |
|---|---|
| `.github/workflows/ci.yml` | Keep pinned actions, retained semantic model data and explicit model-quality execution; retain the blocking 200k-reference workload and accumulate failures across performance workloads. |
| `bench/test-skips.json` | Union the platform tests and partial assertions. Register inherited module gates recognized by the stronger checker; do not add runtime skips. |
| `semantic.rs` | Retain parallel vector scanning and diagnostics; use the shared semantic thread/error handling and the single background-work launcher. |
| `semantic_model.rs` | Use shared hash/stamp verification before model loading; retain converted F32/F16 weights, compact tokenizer loading, read-ahead and private model directories. |
| `cmd/search.rs` | Preserve snapshot caller filtering, disclosed keyword fallback, coverage output and asynchronous census; retain per-stage semantic and keyword timings. |
| `cmd/work.rs` | Run selected tasks in refresh/semantic/reclaim/archive/post-index order under one work lock; configure background priority and the half-CPU cap before work begins. |
| `main.rs` | Keep the asynchronous census lifecycle and pending-source reporting; full reindex uses the vector reset before the ordered post-index tasks. |
| `docs/perf-budgets.json` | Keep incoming unscaled SPEC latency limits, first-index scaling by the larger row/text fraction, and dense headroom; preserve every semantic owner-scale limit and quality requirement. |

The vector-reset integration test additionally checks that the transactional
reference count becomes zero. The query profiler records census execution and
join time separately; it does not restore inline source parsing. Normal command
output does not include profiling events.

## Evidence handling

The stronger privacy checker found workstation paths and identity tokens in
older semantic evidence. They were replaced with synthetic labels; timing values
were preserved. One oversized profile was split at record boundaries, with its
original digest and record count retained in a manifest. The checker and its
size limit were not weakened. These changes do not rewrite prior Git objects.

The integration measurements use only the committed synthetic inputs and native
200k-reference fixture. The reference checkout is read-only. All processes use
nice 10; inference uses six CPU threads and one BLAS thread. Every completed
sample retains its load averages. Raw test/build logs and model files are local
scratch; committed evidence contains aggregates and public fixture identifiers.

## Pre-merge validation

Formatting and release clippy with `-D warnings` pass. The full release workspace
suite passes 280 tests across 20 suites with zero failures or ignored tests;
doc tests pass. The bench Python suite passes 25 tests, and all six Node tests
pass. The stricter skip-policy checker and third-party license check pass.

Both actual-model quality gates pass, including token IDs, ordered top-five
agreement, labeled recall and long-tail recall. The owner-scale latency and bulk
comparison are measured after the merge commit and reported separately below.

The worktree and staged snapshot pass the privacy checker. Reachable history
still contains a known violating ancestor blob; a bounded check records that
failure in `privacy-history.json`. The full traversal was stopped after that
proof. No history rewrite or relaxation of the history gate was performed.

## Measurements after the merge

One matched process per engine/backend/fixture embedded four batches, including
the first batch. Throughput includes tokenization; model loading is recorded
separately. Every process used six inference threads, one BLAS thread and nice 10.

| Bulk rows/s | 0.4.9 | Merged binary | One-minute load, reference / merged |
|---|---:|---:|---|
| CPU, 200 tokens | 9.01 | 24.62 | 86.9–97.3 / 97.3–97.6 |
| CPU, mixed | 23.32 | 37.20 | 97.3–97.6 / 93.1–97.3 |
| Metal, 200 tokens | 352.57 | 440.84 | 84.1–86.4 / 86.4–86.4 |
| Metal, mixed | 694.12 | 707.56 | 86.4–86.4 / 86.4–86.9 |

All four observed bulk comparisons favor the merged binary. This is one paired
run per case on a busy shared host, not a confidence interval or a quiet-machine
ceiling. Raw batch timings and all three load averages are retained.

The query workload contains 200,000 vector references built by the deterministic
fixture builder, with actual model inference for every query. Each mode has
twenty fresh-process samples; every sample returned meaning results.

| Query measurement | p50 ms | p95 ms |
|---|---:|---:|
| Semantic only | 127.90 | 171.81 |
| Hybrid | 197.19 | 1935.37 |
| Keyword stage inside hybrid | 115.79 | 1886.01 |

One-minute loads ranged from 54.8 to 64.9. Hybrid p50 passes; semantic p50,
semantic p95 and hybrid p95 miss their unchanged owner limits. The workload
returned exit 1, as the blocking CI gate requires. No failed sample was removed.

The census ran concurrently. Its semantic-query median was 22.51 ms, while the
final join waited only 1.16 ms. No sources changed and no worker launched during
these samples. Model load was 38.29 ms median, query inference 21.04 ms, vector
scan 6.46 ms and refinement 9.36 ms. These overlapping stage statistics cannot
be added to reconstruct an end-to-end percentile.

Six hybrid samples exceeded twice the hybrid median; all were dominated by
keyword execution. The single semantic outlier was the first process, with a
startup/exit/scheduling gap outside the timed search. These records and their
stage counters are retained in `summary.json` and `owner-query.json`.

Both backends retained token-ID parity, 10/10 ordered top-five agreement, full
labeled recall and 10/10 long-tail recall. Minimum oracle cosine was
0.9999999999982052 for CPU and 0.9999823635014371 for Metal. The full 1024-token
window remains unchanged.

The current-tree privacy check and all requested correctness checks pass. The
owner latency gate and historical privacy finding remain open; neither gate
was relaxed. Nothing was pushed.

Temporary models, generated corpus data, build output and Python bytecode caches
were removed after measurement. `cleanup.json` records the cleanup and confirms
that no work or measurement process was left running.
