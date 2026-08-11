# Semantic scale gate

Measured on one Apple-silicon macOS host with Python 3.14.6 and
NumPy 2.5.1. The disposable fixture contains normalized 384-dimensional vectors
and an adversarial session map: 75% of all rows belong to one giant session,
while the rest are four-row sessions.

Reproduce the blocking campaign with:

```sh
python bench/semantic_q8_scale.py --check \
  --rows 100000 1000000 2000000 --repeats 3
```

The timed retrieval path is the production Rust mmap scanner returning the top
128 session families with eight candidate heads per family, followed by an exact
f16 rerank and a 40-session page. Process-cold means a new scanner process with
header, size, and generation validation; it does not pretend that the OS page
cache was purged.

## Current initial-embed wall

The existing real-model harness (`bench/resources.py --check-semantic --json`)
measured the current quantized Granite ONNX path on a 128-row mixed-length batch.
The batch itself took 824.697 ms, or **155.209 rows/s**; process start through exit
took 1,036.954 ms, peak RSS was 374.984 MiB, and resident semantic RSS settled at
179.375 MiB. It uses the pinned production model and tokenizer, not random-vector
math. Provider, thread, batching, precision, and lane results are recorded in
`bench/EMBED_SPEED.md`.

This wall is the ONNX int8 CPU lane, which is the floor available on every
supported platform. On Apple silicon the Metal lane measured 10.9x this engine
in an interleaved A/B, so the hours below divide accordingly; `EMBED_SPEED.md`
holds that measurement and the parity gate the lane has to pass first.

| rows | projected model inference | final q8 publication | lower bound |
|---:|---:|---:|---:|
| 100k | 10.7 min | 0.3 s | 10.7 min |
| 1M | 1.79 h | 2.1 s | 1.79 h |
| 2M | 3.58 h | 4.7 s | 3.58 h |
| 5M | 8.95 h | 11.6 s | 8.95 h |
| 10M | 17.90 h | 23.2 s | 17.90 h |

These are inference lower bounds, not claims that publication is free. A 500-row
active inference tranche is about 3.2 seconds and publishes an immutable delta
segment proportional to that tranche. The segmented scale gate covers immutable
delta publication; `py/test_semantic_segment_compact.py` covers compaction and
accelerator rebuild correctness.

The q8 build timings below measure full-build and compaction throughput. They are
not the cost of publishing a normal small update.

Inference deduplication can materially improve effective throughput on histories
with repeated text. Private reuse measurements are intentionally omitted, and the
scale table stays on raw inference throughput so synthetic and future corpora do
not inherit an unearned duplicate-rate assumption.

## Measured ladder

| rows | q8 + groups | f16 | q8 build | +10k full rebuild | process-cold | grouped scan max | scan + f16 rerank max | scanner RSS / private |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100k | 37.4 MiB | 73.2 MiB | 0.262 s | 0.249 s | 5.5 ms | 1.6 ms | 6.7 ms | 44.4 / 2.7 MiB |
| 1M | 373.8 MiB | 732.4 MiB | 2.097 s | 1.996 s | 9.0 ms | 16.1 ms | 23.9 ms | 387.0 / 9.1 MiB |
| 2M | 747.7 MiB | 1,464.8 MiB | 4.671 s | 4.833 s | 9.2 ms | 32.5 ms | 38.0 ms | 764.0 / 12.3 MiB |

RSS includes clean file-backed q8/group pages and is reclaimable by the OS.
Private memory is the better paging-pressure measure. The f16 candidate pages
are mapped by the Python worker and only the selected rows are read on the timed
path.

## Recall parity

Direct q8 ranking is diagnostic only. Across the synthetic queries its worst
top-40 overlap with f32 was 92.5%, despite small score error; that is not enough
to make q8 the final ranker. The adopted contract is q8 candidate generation,
then f16 reranking.

At 100k, 1M, and 2M, all three frozen queries achieved 100% minimum recall for:

- row top-1, top-8, and top-40;
- session top-1, top-8, and top-40 under the giant-session adversary;
- f16 reranked top-1 and top-40 versus f32.

The native group sidecar is required for this result. A fixed row-overfetch pool
cannot preserve distinct-session ranking: the unit adversary demonstrates that
top-128 rows recover less than half of the exact 40-session head.

## 5M and 10M projection

Artifact sizes are exact format math. Scan, retrieval, build, and top-up use a
Theil-Sen affine fit across the three measured sizes. Header-open is the largest
measured value because it is payload-size independent. Private memory is the
larger of that fit and an analytic all-unique-session bound: 16 bytes per row of
native group scratch plus the largest measured fixed overhead.

| rows | q8 | groups | f16 | q8 + groups + f16 | grouped scan | full retrieval | q8 build | +10k rebuild | cold open | mapped RSS / private |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 5M | 1.807 GiB | 0.019 GiB | 3.576 GiB | 5.402 GiB | 81.2 ms | 87.6 ms | 11.6 s | 12.1 s | 9.2 ms | 1,952 / 82 MiB |
| 10M | 3.614 GiB | 0.037 GiB | 7.153 GiB | 10.803 GiB | 162.4 ms | 170.1 ms | 23.2 s | 24.1 s | 9.2 ms | 3,897 / 159 MiB |

The 10M retrieval projection leaves about 130 ms inside the 300 ms full-CLI
budget for query embedding, Python startup, IPC, and rendering. The full-exit CLI
gate measures that complete contract rather than treating this component timing
as user-visible latency.

## Release budgets

The release workflow runs the full 100k/1M/2M ladder with portable-CI slack,
checks bounded pending-plan latency, and gates segmented publication at 10M rows.
The target 10M ceilings are 180 ms grouped scan, 220 ms scan plus f16 rerank,
50 ms process-cold open, 45 s q8 build, 50 s top-up rebuild, 256 MiB private
memory, 3.70 GiB q8, and 11.0 GiB for q8 + group map + f16. Candidate parity
thresholds do not receive platform slack.
