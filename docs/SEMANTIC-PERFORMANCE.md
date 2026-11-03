# Semantic performance investigation

The latest merged measurements are in [SEMANTIC-INTEGRATION.md](SEMANTIC-INTEGRATION.md). The preceding kernel investigation is in [SEMANTIC-ROUND2.md](SEMANTIC-ROUND2.md). This file preserves the first round's measurements.

The performance bar is not met. The changes below pass the correctness gates,
but do not establish a release that is faster than 0.4.9 everywhere.

The measurements in `measurements/semperf/` are from an Apple M2 Max with twelve
logical CPUs. They supersede the load-contended historical throughput claims as
a comparison on this machine; they do not establish performance on an M5.
`machine.json` records the compiler, OS, source revisions and baseline executable
hashes. Performance limits and their derivations remain in
[`perf-budgets.json`](perf-budgets.json).

## Protocol

The final matched bulk runs use six Rayon/ONNX inference threads, single-threaded
BLAS, disabled tokenizer parallelism, nice 10, and fresh processes. CPU runs wait
until the one-minute load is at most the machine's logical CPU count before
starting; later load changes never cause a sample to be discarded. Wait records
are retained separately. Each process loads
one model and embeds eight batches; three processes per engine/fixture are
retained. Tokenization and the first batch are included. Weight loading is also
recorded separately. The reference imports the read-only 0.4.9 checkout through
its Python environment; neither MLX nor ONNX Runtime is a product dependency.

Both implementations receive the exact committed JSON strings. The first fixture
has 128 rows of 200 tokens. `semantic-mixed.json` contains 128 synthetic prose
windows: 24 of 80 characters, 32 of 180, 32 of 400, 24 of 700 and 16 of 1,000.
It is a disclosed length mix, not a sample of private conversation content.
The tokenizer lengths and original padding costs are in `padding-before.json`.

The query fixture contains 200,000 vector references in 4,000 native synthetic
conversation files. Its geometry is deterministic perturbations of the frozen
ONNX oracle, as in `semantic_scale`; these are not 200,000 model inferences.
Each query process performs normal freshness, retrieval, rendering and exit.
Twenty samples per mode are retained, interleaving semantic, hybrid and lexical
commands across the same ten queries. Lexical-only latency is reported separately
from hybrid; the keyword engine is not replaced in this worktree.

Every timed run records `uptime`, before/after load averages, settings, exit status
and output. The final table uses a predeclared interleaved protocol, not earlier
exploratory runs. The earlier matched campaign also encountered a load spike
above 170 and remains as `loaded-*` evidence; it does not establish a CPU ceiling.
Exploratory runs with six BLAS threads, wide reference batching, or a reused
reference process remain in the directory with their actual settings. One bulk
child was cancelled to end an accidental overlap with a query campaign; its
failed record and explanation are retained. A queued campaign was subsequently
stopped while waiting for acceptable load. No failed sample becomes a passing
measurement.

The host restarted after the first complete CPU/query campaign. Every earlier
sample is retained as `pre-restart-*`. File identities changed during restoration,
so explicit provisioning refreshed the verification/conversion cache before the
post-restart campaign. Its first cold diagnostic is retained separately; neither
cold nor failed samples are silently relabeled as warm results.

The final reference-query run is explicitly labeled contended. The host stayed
fully CPU-busy after a prolonged quiet-start wait, so this last context run kept
nice 10 and the six-thread cap while recording every sample without a load gate.
It is not used to claim a matched-host query speedup. No acceptance budget or
completed before/after/bulk condition was changed. The empty quiet-start attempt,
wait history, and an initial interpreter-path setup failure are retained.

Profiles deliberately synchronize stages to distinguish CPU command encoding
from device completion waits. Those waits include GPU queue/driver contention;
they are not hardware GPU timestamps. Instrumented timings are not throughput
samples. The first Metal profile also preserves its shader compilation costs.

## Measured causes and changes

The original mixed batches wasted 49.4% of CPU padded tokens and 63.1% on Metal.
The scheduler now also bounds padding waste, while retaining the complete
1,024-token input window. Sorted outputs are restored to input order.

GeGLU was a substantial part of CPU and Metal layer time. Making Metal slices
contiguous alone did not help; that experiment is retained and was replaced by a
single kernel with the same Candle erf polynomial and intermediate F16 rounding.
CPU uses Darwin's system `erff` where available and Candle's fused last-dimension
softmax instead of separate reduction/subtraction/exponential/division tensors.
The isolated arithmetic check
found at most one ULP difference from Candle's software routine. The independent
full-model quality gates decide whether that rounding is acceptable.
Large CPU projections are split across the existing Rayon pool. Each projection
retains the same dot-product precision and row order; BLAS remains single-threaded.
The added regression compares values and order against Candle's original linear
operator. This change does not affect Metal or short query matrices.

Explicit provisioning now writes F32 and F16 safetensors and compact tokenizer
JSON. Their metadata is bound to the verified source checkpoint. Queries accept
unchanged converted files, otherwise use the verified original without writing
the cache. Tokenizer and weight construction overlap. Unused rotary positions
above the existing input window are no longer constructed at every model load.

At owner scale, the original profile spent about 21 ms opening vector blocks and
30 ms scanning them. Block opens and scans now run in parallel; supported ARM
processors use integer dot-product instructions. Conservative candidate bounds,
F16 refinement, filter handling and deterministic ties are retained.

`__work` lowers its own priority even when launched directly. It selects Metal
where available. CPU work uses at most half the available logical CPUs, with a
single BLAS thread to prevent nested oversubscription. The measured CPU background
projection therefore uses the same six/one thread policy as the matched runs.

The final scheduling policy retains the larger CPU batch allowance; the CPU
diagnostic sweep favored it on both fixtures. Metal uses smaller batches: its
measured 1,024-token allowance outperformed the larger tested alternatives. This
changes scheduling and GPU working-set size, not the input window.

The paired Metal diagnostic at a fixed 128-by-200 shape reduced total GeGLU
completion time from 476 ms to 195 ms across twelve layers, and layer time from
1,551 ms to 1,101 ms. CPU command encoding for the seven instrumented stages
was only about 5.6 ms after the change. The remainder includes normalization,
residual/layout/mask operations and device completion. The smaller production
batches were chosen by uninstrumented throughput; their many inserted profile
barriers make instrumented end-to-end timing unsuitable for that comparison.
Candle's full Metal SDPA path is already used, with explicit local/padding masks
and an F16-safe finite mask. The short-sequence fallback preserves mask handling.

The post-restart mixed CPU profile processed five length buckets (60 layer
invocations): linear projections consumed 622 ms, attention 363 ms and GeGLU
342 ms out of 1,544 ms of layer time. The full per-layer records are retained in
`profile-final-cpu-mixed.json`. Padding was 15,452 scheduled tokens for 12,016
real tokens, or 22.24% waste, versus 49.43% in the original CPU scheduler.

## Results and validation

<!-- final-results -->
Bulk values are median rows/s across three fresh processes, each with eight
batches. Parentheses show the one-minute load range at sample boundaries;
the raw records retain all three load averages and every batch time.

| Bulk fixture | 0.4.9 | Before | After |
|---|---:|---:|---:|
| CPU, 200 tokens | 44.73 (9.68–12.60) | 36.70 (9.73–16.95) | 34.41 (9.68–21.65) |
| CPU, mixed | 116.64 (8.08–9.73) | 48.46 (7.38–9.68) | 82.32 (8.08–9.29) |
| Metal, 200 tokens | 307.84 (9.91–12.40) | 116.98 (9.91–12.87) | 155.39 (10.44–11.88) |
| Metal, mixed | 600.87 (10.51–12.87) | 88.31 (9.77–12.87) | 314.28 (10.51–12.12) |

Fresh-process query values are p50 / p95 milliseconds, with the same load
notation. There are twenty samples per mode. The reference uses its
default Apple-silicon MLX lane with daemon use disabled.

| Query mode | 0.4.9 (contended) | Before | After |
|---|---:|---:|---:|
| semantic | 252.433 / 4712.384 (56.75–62.18) | 167.837 / 185.536 (9.16–11.27) | 120.036 / 925.172 (8.62–11.63) |
| hybrid | 885.185 / 9458.152 (55.17–62.18) | 414.670 / 7281.839 (9.16–11.27) | 440.373 / 7193.407 (8.49–12.67) |
| keyword | 180.824 / 2019.307 (55.17–62.18) | 33.948 / 6857.987 (9.16–11.27) | 37.093 / 6807.355 (8.49–17.64) |

The reference query run was explicitly contended after the host supplied no quiet window. Its values are context only, not a matched-host speedup comparison. Before/after query and all bulk sampling conditions were unchanged.

Inside the after-hybrid command, the keyword stage was 382.48 ms p50 / 7144.67 ms p95. This is measured directly; it is not a subtraction of medians.

At the measured mixed-length rate, 130,000 rows project to **6.9 minutes on Metal** and **26.3 minutes on CPU**. These are model-only estimates, including tokenization but excluding indexing/publication, with one window per row.

<!-- /final-results -->

The measured fixed-length CPU median regressed in the final campaign, and the mixed CPU
workload still trails ONNX q8. The Metal implementation remains about half of
MLX throughput on both fixtures. Per the requested stopping condition, no MLX or
ONNX Runtime backend was added to work around the remaining gap in the pinned Candle path.

The query path retains ordinary freshness checks. Its final after-profile spent
31.9 ms preparing the query, 7.2 ms opening vector blocks, 38.9 ms loading the
model/tokenizer in parallel, 13.2 ms in model layers, 5.8 ms scanning, 0.6 ms
sorting candidates and 7.0 ms refining/resolving results. Cold diagnostics are
also retained; baseline block-opening profiles immediately following bulk work
had cold file pages and are not used as a warm speedup claim.

Validation: `fmt-final-projection.log` and `clippy-final-projection.log` passed;
`tests-final-projection.log` records **207 passed, 0 failed, 0 ignored**. The
actual-model `quality-final-cpu.log` / `quality-final-metal.log` gates passed:
CPU minimum cosine 0.9999999999976904; Metal 0.9999823763926029; token IDs match,
ordered top-five agreement is 10/10, labeled recall is complete, and long-tail
recall remains 10/10. `budget-audit.json` confirms that no existing numeric budget
was changed. The new task-specific gates are defined only in `perf-budgets.json`.

A direct quality check of the reference q8 ONNX embedder used the same frozen
F32 oracle. Token IDs matched, but its minimum cosine was 0.9617269184 and only
3/10 ordered top-five lists matched. That is evidence against substituting this
particular quantized profile under the current gate; it does not prove that every
possible quantization scheme fails. No ONNX Runtime or MLX dependency was added.

<!-- retained-pre-restart -->
### Retained pre-restart campaign

All three bulk samples and all twenty query samples per mode remain in the
`pre-restart-*` records. Each cell includes the one-minute load range across
the beginning and end of its samples. This is a separate host epoch.

| Measurement | 0.4.9 | Before | After |
|---|---:|---:|---:|
| CPU, 200-token rows/s | 29.30 (load 11.6–17.6) | 27.64 (load 11.6–20.3) | 29.07 (load 11.4–12.8) |
| CPU, mixed rows/s | 31.34 (load 11.4–30.6) | 20.75 (load 10.4–14.5) | 31.44 (load 11.1–14.4) |
| semantic p50/p95 ms | — | 236.7 / 319.2 (load 7.3–11.2) | 123.8 / 152.9 (load 6.3–7.5) |
| hybrid p50/p95 ms | — | 555.8 / 10698.9 (load 7.3–11.6) | 437.0 / 9079.6 (load 6.3–7.5) |
| keyword p50/p95 ms | — | 52.7 / 9035.2 (load 7.3–11.6) | 38.7 / 8283.6 (load 6.3–7.5) |

The pre-restart after-hybrid keyword stage itself was 385.0 ms p50 and
9,022.4 ms p95; these are in-command timings, not subtraction of medians.

<!-- /retained-pre-restart -->

## Integration surface

Model changes are contained in `semantic_model.rs`, `semantic_bert.rs`, and the
new Metal GeGLU implementation. `semantic_store.rs` parallelizes immutable block
mapping; `semantic.rs` parallelizes scanning and defines background resource
policy. `semantic_q8.rs` adds the guarded ARM dot instruction. `lib.rs` exports
the private implementation modules. The only command changes are profiling in
`cmd/search.rs` and the initial resource policy call in `cmd/work.rs`.
No keyword ranking, source adapter, setup consent, archive policy or fusion rule
is changed.

The measurement examples and scripts accept explicit fixture/model paths. New
regressions cover padding-aware batching, converted artifact invalidation,
parallel block order/path validation, background CPU bounds, fused F16 GeGLU,
and bounded CPU projection values/order.

## Remaining limits

These wall-clock measurements still encountered changing shared-host load. The
CPU 200-token results in particular have a wide range and do not prove an
uncontended hardware ceiling. The query p50 misses its limit by 0.0365 ms; it is
reported as a miss, not rounded into a pass. Hybrid also misses: its measured
keyword stage dominates, and that engine is being replaced separately.

The corpus-time estimates assume the disclosed mixed distribution (mean 93.875
tokens, one embedding window per row). Longer rows requiring more windows,
indexing, publication, deduplication and background scheduling add work. No
130,000-row private corpus was read or embedded to obtain these estimates.
Performance on M5, Linux and Windows was not measured here.

The final reference fixture uses the exact exported F16 geometry, re-normalized
for the legacy F32 input format, and the same synthetic source rows. After archive
restoration, the original indexer was run with `index --full` to restore its
publication proof before calling its native vector-publication API. The initial
failed publication and successful repair are both preserved.

## Constraint exception

One initial history lookup mistakenly invoked the installed CLI by absolute path.
It used the task's empty temporary home/data directories and is not part of the
performance evidence. Subsequent reference commands import or execute only the
read-only reference checkout with explicit temporary roots and model paths.

## Source commits

- `ee5454ca`: fused inference, provisioning cache, parallel scans and background resource policy.
- `21e080cb`: bounded CPU projection scheduling and its regression test.

The accompanying evidence commit contains the fixtures, runners, every retained
measurement, and this report. No branch was pushed, merged or rebased.
