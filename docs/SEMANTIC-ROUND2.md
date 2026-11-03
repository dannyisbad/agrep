# Semantic performance, round 2

The release performance bar remains open. This change adds a blocking
owner-scale CI workload and retains failures as failures. The authoritative
limits and their reasons are in [perf-budgets.json](perf-budgets.json).

## Measurement protocol

The machine is an Apple M2 Max with 12 logical CPUs. Each CPU run uses six Rayon
or ONNX threads, one BLAS thread, disabled tokenizer parallelism and nice 10.
Both backends receive the exact committed 128-row fixtures: 200-token inputs
and a disclosed synthetic conversation-length mix. Both retain the complete
1,024-token window. Model load time is separate from bulk throughput; tokenizing
and the first batch are included. Final bulk sampling interleaves 0.4.9, the
previous Candle algorithms selected in the same executable, and the new paths.
Every batch and process sample is retained.

The initial bulk campaign required a one-minute start load at most equal to the
logical CPU count. It completed ten processes, retained as `matched-quiet-partial`,
before a long period of host contention. After more than twenty minutes without
another quiet window, the complete comparison was run separately and explicitly
labeled contended. Neither campaign discards a completed sample. The separate
CI reproduction also uses ordinary execution without a quiet-start condition.
Its host was heavily contended. This limits performance conclusions; it does not
waive a failed gate.

Queries use fresh processes and ordinary freshness checks on 200,000 references
in 4,000 native synthetic conversation files. The fixture builder deterministically
perturbs the frozen oracle vectors; it does not run 200,000 model inferences.
Every query uses the actual model, must return meaning results, and records
keyword execution separately. The 0.4.9 query numbers retained from round 1 are
contended context, not a matched-host speedup claim.

All evidence in [measurements/semperf2](../measurements/semperf2/README.md) is
aggregate-only. It includes failed experiments, every completed timing sample,
the load readings and the two invalidated quantization probes. Instrumented
per-operation completion times include synchronization and scheduling. They
are not hardware GPU timestamps or throughput measurements.

## Bulk results

These are **contended** medians, three processes per cell and four batches per
process. Parentheses give the one-minute load range at sample boundaries; every
record also retains the five- and fifteen-minute averages. They establish the
observed comparison in this campaign, not the machine's quiet performance bar.

| Rows/s | 0.4.9 | Previous Candle paths | New Candle paths |
|---|---:|---:|---:|
| CPU, 200 tokens | 8.09 (52.5–135.4) | 17.87 (66.0–120.8) | 28.47 (61.1–123.0) |
| CPU, mixed | 9.94 (50.4–117.3) | 14.00 (44.4–132.5) | 24.47 (42.3–135.8) |
| Metal, 200 tokens | 155.64 (50.0–129.1) | 111.57 (50.0–132.0) | 236.81 (52.1–135.8) |
| Metal, mixed | 404.52 (52.1–123.0) | 251.59 (52.1–125.8) | 459.65 (52.1–125.8) |

The partial quiet-start campaign retained one completed reference/control/new
triple for Metal fixed (226.36/113.71/252.81 rows/s), Metal mixed
(413.01/230.90/414.42), and CPU fixed (40.57/46.64/54.59). It also retained the
next CPU mixed reference (55.10 rows/s) before load prevented the remaining
comparisons. Ten processes are preserved; none is dropped from the evidence.
This is insufficient to establish all four quiet-machine comparisons.

At the observed contended mixed medians, 130,000 one-window rows project to
about **4.7 minutes on Metal and 88.6 minutes on CPU**. This is embedding and
tokenization time, excluding initial model load and database publication. Longer
rows can require multiple windows. These estimates describe the recorded load,
not a promised completion time on a quiet machine.

## Query results and tails

Each current cell contains twenty fresh processes. Values are p50 / p95 ms.
The 0.4.9 column is the retained, contended round-one measurement, not a matched
comparison with the current host epoch.

| Query | 0.4.9, prior contended run | Before, current run | After, current run |
|---|---:|---:|---:|
| Semantic only | 252.43 / 4712.38 | 217.12 / 345.36 | 192.81 / 293.44 |
| Hybrid | 885.18 / 9458.15 | 565.18 / 10163.44 | 590.77 / 9176.20 |
| Keyword only | 180.82 / 2019.31 | 63.65 / 9230.31 | 71.31 / 9641.18 |

Reference load ranges were approximately 55–62. Current before/after ranges
were 20.1–33.5 / 19.6–33.1. The final executable's separate hybrid keyword span
was **524.52 / 9100.11 ms**; these are measured spans, not differences of medians.
The keyword implementation was not changed.

| Final semantic-only stage | p50 ms | p95 ms |
|---|---:|---:|
| Ordinary freshness | 30.73 | 45.82 |
| Model read-ahead | 5.24 | 5.71 |
| Exact coverage count | 0.03 | 0.08 |
| Vector-block opening | 9.67 | 18.16 |
| Model and tokenizer load, overlapped | 58.46 | 77.23 |
| Tokenizer construction within that load | 58.10 | 76.07 |
| Weight construction within that load | 41.15 | 58.49 |
| Query inference | 33.57 | 60.17 |
| Vector scan | 7.17 | 14.48 |
| Refinement and result resolution | 14.25 | 28.54 |
| Outside the timed search | 16.69 | 46.28 |

Overlapping stages and separately computed percentiles cannot be added to
reconstruct a percentile of the whole command. Verification was a 0.09 ms median;
all final samples reused verified converted weights, wrote no model files, and
launched no worker. Freshness reported zero changed rows.

`tail-attribution.json` attributes all 27 final samples above twice their own
mode/variant median, and all reproduced outliers from the earlier campaigns.
The final before-semantic cold outlier was 1047 ms with a coverage-table scan
and vector page-ins. The final after-semantic outlier was its first process,
500 ms, including **256 ms outside the timed search** despite resident model
pages. This remains a startup/exit/scheduling gap; the evidence does not identify
one particular loader or scheduler operation. It is not relabeled as model I/O.
All seven final after-hybrid outliers were dominated by keyword query execution;
one also incurred model page-ins. No changed-source refresh or concurrent
embedding explains those tails.

The original round-one audit is separate: its eight hybrid outliers spent
97–99% of wall time in keyword execution. Its two semantic page-in explanations
remain explicitly inferred from reproduction because the original runs lacked
the counters needed to prove them retrospectively.

The new blocking CI workload was also executed in full: 200,000 deterministic
references, actual query embeddings, twenty samples per mode. It exited **1**,
with semantic **170.28 / 215.39 ms** and hybrid **544.83 / 10241.77 ms**, at load
83–180. `owner-ci.json` keeps every sample and each failed limit. It used the
same CPU path before the final Metal-only initialization cleanup. No budget was
loosened. Neither that run nor the final query run passes the owner-scale gate.

## Changes and measured causes

The original semantic tails included cold model/vector page-ins and a full
reference count. Queries now request read-ahead for nonresident converted model
pages and mapped vector blocks. Transactional insert/delete triggers maintain an
exact reference count, so coverage uses a point lookup. Publication uses UPSERT
to avoid the delete/insert trigger ambiguity of SQLite replacement. Tests cover
replacement, rollback, invalidation and concurrent snapshots.

The CPU attention path packs bounded tiles, uses Accelerate matrix operations
and vector exponentials, and shares the existing Rayon pool across heads. It
does not materialize the full score matrix. Fused residual normalization and
GeGLU remove temporary tensor operations. A system-erf fallback in the negative
activation tail preserves the existing numerical tolerance. Two striped input
streams share the same bounded pool, balance sorted lengths and restore input
order, including odd batches.

Metal keeps F16 weights and intermediates in reusable private buffers. Packed
rotary QKV, residual normalization and GeGLU use dedicated kernels. The measured
selection uses Candle 0.9.2 full SDPA with finite padding/local masks, and the
custom attention kernel for short sequences. The direct SDPA call supplies byte
offsets and writes the final output layout. The layer loop has no tensor-buffer
allocation or explicit host readback; a batch reads its final output once.
Workspace generation events verify reuse. A larger command-buffer allowance
and tighter padding were measured and rejected.

The final startup-only cleanup stops compiling three inactive tensor-path
libraries when the Metal arena is selected. Bulk/profile timings were collected
before that cleanup; its numerical operations and scheduling are identical.
Final-source tests, model gates and query measurements are collected afterward.

The per-channel Q8_0 probe through Candle's quantized matmul was slower than
Accelerate F32 in all tested shapes. It remains a standalone diagnostic and is
not enabled in the product. Its first setup attempt failed; its second attempt
had an owned-byte lifetime bug exposed by the new round-trip test. Only the
corrected third probe supports the reported comparison.

## Per-operation profiles

The following are total instrumented milliseconds for one 128-by-200-token
fixture, not unfenced throughput. Reference CPU load was 21.6; Candle CPU load
was 78.2–73.1. Reference Metal load was 22.4; Candle Metal load was 71.8. These
different conditions prevent a precise speedup claim. The complete mixed-input
and fixed-input comparisons are in `per-op-comparison.json`; 168 individual
layer records are in `per-layer-final.json`.

| CPU operation | 0.4.9 ONNX wall ms | Candle wall ms |
|---|---:|---:|
| Linear projections | 1442.5 | 1418.8 |
| Attention | 624.6 | 762.9 |
| GELU / fused GeGLU | 203.3 | 597.1 |
| Layer normalization / fused residual normalization | 302.8 | 187.1 |
| Rotary embedding | not isolated by the ONNX aggregate | 140.4 |
| Full fixture | 3091.0 | 3375.7 |

Operator boundaries differ: reference GELU excludes its separate gate multiply;
the fused operations include their residual/gate work. Candle layer wall time
was 3330.2 ms, with 10687.5 ms of process CPU time. Attention used 3028.7 ms and
GeGLU 2579.9 ms of process CPU time. Non-projection work remains substantial;
the profile does not prove an irreducible CPU throughput ceiling.

| Metal operation | 0.4.9 MLX completion ms | Candle completion ms |
|---|---:|---:|
| QKV projection | 181.8 | 90.5 |
| Rotary embedding | 305.8 | 29.3 |
| Attention | 235.4 | 70.6 |
| Attention output projection | 165.4 | 43.4 |
| Normalization, including embedding norm | 289.9 | 66.1 |
| MLP input projection | 213.5 | 186.9 |
| GELU / fused GeGLU | 188.3 | 317.2 |
| MLP output projection | 189.5 | 106.6 |
| Full fixture | 2655.1 | 994.6 |

Candle command encoding for the listed layer operations totals about 17 ms;
completion includes GPU execution, per-operation fences and scheduling. The
normal arena path omits those diagnostic fences. Padding remains bounded by
the existing limit; tightening it further increased dispatch costs in the
retained sweeps. The quantized CPU projection probe's median slowdown ranged
from 9.1x to 111.3x across its tested shapes, so it was rejected before a full
quantized-model quality experiment.

## Validation and integration

`cargo fmt --all --check`, release workspace clippy with `-D warnings`, and
`cargo test --release --workspace --all-targets` pass. The workspace reports
217 tests passed, zero failed and zero ignored. The benchmark suites add
18 passing Python and six passing Node tests, with zero skips.

The final independent-oracle minimum cosine is 0.9999999999982052 on CPU and
0.9999823635014371 on Metal. Both preserve token IDs, all ten ordered top-five
lists, all labeled recall tasks and all ten long-tail labels. The real-model
gate also exercises an odd 41-input batch against the independent oracle.

Shared changes are confined to the semantic implementation and its diagnostics,
coverage storage, the search/main timing hooks, examples, tests and documentation.
The only workflow edit is the existing performance step: it runs the new
owner-scale gate after the smaller semantic workload and preserves a nonzero
status from any performance workload. No keyword engine implementation, ranking,
adapter, fusion rule, setup consent or existing numeric budget is changed.

`__work` selects Metal where available. Its CPU fallback retains the tested
half-logical-CPU cap and background priority with single-threaded BLAS. The
direct four-row smoke test selected the verified F16 Metal model and drained
the queue. Its cold startup was delayed by Metal compiler IPC; a process sample
places the wait in library compilation, before embedding.
The final binary repeated the four-row check successfully in 0.51 seconds with
the shader cache warm. The original cold run took 145.74 seconds; these are
different cache/host conditions, not a paired startup speedup claim.

## Open risks

The foundation keyword engine remains responsible for multi-second hybrid
tails; its replacement is outside this change. Cold model pages and Metal
compiler startup still incur real I/O or service waits. Host load readings alone
cannot establish GPU idleness or remove scheduling noise. The synthetic length
mix and vector geometry are disclosed approximations, not a measurement of
private owner history. Cross-platform CI still has to execute the new blocking
workload; local success of numerical tests does not waive its latency failures.

The quiet-machine throughput bar is **not established**. The available profiles
also do not prove that every remaining CPU cost is irreducible within the
constraints. The defensible outcome is a tested implementation, retained gains
and misses, and a blocking gate, rather than a claim that all targets now hold.

## Commits and reproduction

- `8626d202`: blocking owner-scale CI gate, gate tests and budget additions.
- `ba55725a`: fused CPU/Metal execution, bounded workspaces, query read-ahead,
  transactional coverage count and regression tests.
- `dbf88d81`: omit inactive Metal shader compilation during arena startup.
- The accompanying report commit retains the scripts and aggregate evidence.

The existing shared files changed are `semantic.rs`, `semantic_model.rs`,
`semantic_bert.rs`, `semantic_store.rs`, `semantic_profile.rs`, `lib.rs`,
`cmd/search.rs`, `main.rs`, `semantic_eval.rs`, the test-skip registry, the
performance budget file and the workflow's performance step. New attention,
normalization and arena modules contain the backend implementations. No keyword
engine file is changed.

To reproduce the blocking workload from a built checkout:

```sh
nice -n 10 env CARGO_BUILD_JOBS=4 cargo build --locked --release --workspace --bins --examples
nice -n 10 python3 bench/semantic_owner.py --binary target/release/agrep --builder target/release/examples/semantic_scale --evaluator target/release/examples/semantic_eval --runs 20 --output semantic-owner.json
```

The runner provisions in its own temporary model, home and data roots unless an
explicit existing model directory is supplied. `scripts/semantic_matched.py`,
`semantic_kernel_bench.py` and `semantic_tails.py` expose the recorded comparison
protocols; their inputs and conditions are described in the evidence README and
`plan.json`. No installed `agrep` is invoked by these commands.

Generated model/corpus scratch, the build target and Python bytecode caches
were removed after validation. `cleanup.json` records the cleanup and confirms
that no measurement or worker process was left running. Nothing was pushed.
