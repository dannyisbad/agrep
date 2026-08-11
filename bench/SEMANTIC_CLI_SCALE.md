# Full-exit segmented semantic CLI scale gate

Measured on an Apple M4 Pro with macOS and Python 3.14.6. Reproduce the target
gate with:

```sh
python bench/semantic_cli_scale.py --check --runs 3
```

This is process start through process exit, not an internal scanner timer. Each
command runs the production `cli.py`, freshness check, resident semantic worker,
segmented q8 Rust scanner, exact f16 rerank, result policy, rendering, and exit
status path. Cold samples stop the worker before every command. They retain the
OS page cache and downloaded model, so they are process-cold rather than a claim
about a first-ever download or purged disk caches.

## Measured result

| contract | 1M rows | 2M rows | target | result |
|---|---:|---:|---:|---|
| warm semantic, full exit | 88.4 ms | 108.6 ms | 150 ms at 2M | pass |
| warm hybrid, full exit | - | 241.4 ms | 250 ms at 2M | pass |
| process-cold semantic, full exit | 377.2 ms | 432.3 ms | 700 ms at 2M | pass |
| projected warm semantic at 10M | - | 269.8 ms | 300 ms | pass |

The gate ran three completion samples at each size and exited zero. The 10M
value is an affine fit of the measured 1M and 2M full-exit medians. It preserves
fixed Python startup and IPC cost and extends only the observed per-row slope.
It is an extrapolation, not a simulated 10M result; the companion q8 benchmark
independently checks native scanner scale and exact-rerank parity.

The fixture is a real `segments-v2` publication created through
`embedding_segments.publish_base`. Its publish-last manifest references a
generation-bound integrity proof. A representative 2M cold run spent 7.0 ms in
artifact open and validation and completed in 447.5 ms. The three-sample median
above is the release result.

## Resource footprint

The process group includes the Python semantic owner and native q8 scanner. CPU
is cumulative user plus system time added during one command. RSS includes clean
file-backed q8 pages and is reclaimable by the OS. These are representative
diagnostics from the full 1M/2M run immediately before the three-sample gate.

| resource | 1M warm semantic | 2M warm semantic | 2M warm hybrid | 2M cold |
|---|---:|---:|---:|---:|
| query CPU | 72.7 ms | 89.6 ms | 83.7 ms | 381.7 ms |
| max RSS | 569.0 MiB | 954.5 MiB | 954.6 MiB | 954.4 MiB |
| max private footprint | 138.9 MiB | 150.8 MiB | 150.8 MiB | 150.6 MiB |
| disk read during query | 0 MiB | 0 MiB | 0 MiB | 54.9 MiB |
| disk write during query | 0 MiB | 0 MiB | 0 MiB | 0.004 MiB |

The 2M fixture is 6.13 GiB logical and 3.29 GiB allocated. It includes a
3.072 GB sparse f32 source, 1.536 GB f16 rerank matrix, 776 MB q8 matrix,
480 MB segmented refs database, 625 MB corpus database, and the real IDs,
hashes, family groups, native set, and proof artifacts. The sparse f32 source
keeps disposable-fixture allocation bounded while retaining its real row count.

## Correctness proof

The fixture has real cardinality for q8 vectors, family groups, segmented refs,
hashes, IDs, and the keyword FTS index. Every run must satisfy all of these:

- the publisher produces `segments-v2` plus a publication proof;
- the CLI exits successfully and prints exactly one completion record;
- timing contains `q8_retrieval` and does not contain the f32 `matmul` phase;
- explicit-semantic results belong to planted meaning-only families;
- hybrid output contains keyword and labeled planted meaning-only lanes;
- warm samples reuse one resident worker PID.

The fixture cannot silently fall back to the legacy layout: publishing adopts
the flat staging inputs into immutable segment artifacts, and the harness rejects
any fixture without the segmented proof before starting a query.

`bench/semantic_q8_scale.py` owns the vector-quality oracle. Its frozen
100k/1M/2M campaigns require exact top-1 and top-8 recall and at least 0.95
top-40 recall against f32 for row and session-family ranking.

## Regressions found by the honest fixture

The previous CLI scale fixture published a legacy flat matrix. It therefore
never exercised segmented cold open, and a 2M refs validation scan could remain
hidden behind a green result.

Segment publishers now validate rows, hashes, liveness, and uniqueness before
the publish-last manifest becomes visible. The manifest binds a small immutable
proof containing the validated artifacts' descriptors and file identities. New
cold readers verify that proof with bounded stats and header checks. Legacy
generations remain readable through the prior exhaustive hash and liveness
fallback, so the optimization does not weaken corruption detection.

The first true segmented quick run also found 128 one-row corpus lookups after
native candidate selection. They consumed about 310 ms at only 50k rows. Result
resolution now uses one bounded `VALUES` join forced through the corpus
`msgs_session` index; the same quick retrieval phase fell to about 2.7 ms while
retaining per-row text-hash proof.

CI runs the 1M/2M segmented campaigns with three completion samples and the
portable-runner multiplier. The target ceilings above remain embedded in the
benchmark and are the Apple-silicon release contract.
