# Semantic initial-embed throughput

Measured on one Apple-silicon macOS host with Python 3.14 and
ONNX Runtime 1.27.0. Unless a section names its own fixture, the fixture is the
same 128-row short-heavy/mixed-length batch used by `bench/resources.py`.
Rows/s includes tokenization, padding, inference, CLS pooling, and L2
normalization. It excludes model download and matrix publication.

macOS background media analysis made some runs unusably noisy. The tables use
isolated medians or paired runs; the discarded samples ranged as low as 23
rows/s and are not presented as model performance.

## Result

Two engines produce these vectors and this document measures both. Everything
from here through the int8 parity section is the ONNX int8 CPU lane
(`onnx-int8-cpu`). The Metal lane (`mlx-fp16-metal`) has its own section below
and is the default wherever it can open.

For the CPU lane, keep the shipped 50.1 MiB int8 ONNX model on the CPU
provider. Ordinary work keeps six threads; only idle, AC-powered catch-up may
use eight. CoreML is not a hidden accelerator for this export: it leaves most of
the transformer on the CPU and runs about 2.4x slower. FP16 CPU is only about
14% faster while nearly doubling model bytes.

The shipped exact-length path measures 155.6 rows/s (128 rows in 822.7 ms).
Eight-thread catch-up reaches 164-174 rows/s. Inference deduplication improves
effective throughput when a corpus contains repeated text; reuse-dependent
rates are outside this synthetic benchmark.

That is a real improvement, but it does not meet the 350 rows/s overnight floor
or the aspirational 1,000 rows/s target, and the conservative raw-rate
projection is about 16-18 hours for 10M rows. No ONNX session-option toggle
closes that gap; reaching the floor takes a different model, a static-shape
accelerator export, or a different engine, each with its own quality
evaluation. The Metal lane is the engine answer, measured at 10.9x the CPU lane
on Apple silicon.

## CPU thread and session sweep

| q8 CPU setting | steady rows/s | verdict |
|---|---:|---|
| 1 thread | 46.9 | too slow |
| 2 threads | 80.1 | polite pressure mode |
| 4 threads | 126.2 | |
| 6 threads | 155.9 | shipped default |
| 7 threads | 165.4 | |
| 8 threads | 164.1-174.0 | idle AC catch-up only |
| 9 threads | 92.8 | efficiency-core cliff |
| 10 threads | 76.5 | reject |
| 12 threads | 44.9 | reject |

Sequential execution, all graph optimizations, the CPU arena, and memory
patterns remain the winner. `ORT_PARALLEL` measured 89.9 rows/s with two
inter-op threads and 72.0 with four. Disabling memory patterns or the arena did
not beat the default. Basic graph optimization produced no repeatable win and
changed vectors, so the calibrated graph contract stays unchanged.

## Batching and tokenizer

Tokenizing the 128-row fixture one row at a time took a 10.681 ms median. An
isolated `encode_batch` trial produced IDs in 4.157 ms, but the production
tokenizer has global padding enabled. End to end, that API padded every row to
the longest 905-token input, defeated length bucketing, and regressed the batch
to 10.25 seconds and 61.5 CPU-seconds. It is rejected. Production encodes rows
individually inside each bounded 512-row window, then batches exact lengths.

| exact-length padded-token budget | median wall | rows/s |
|---:|---:|---:|
| 512 | 806.1 ms | 158.8 |
| 768 | 795.1 ms | 161.0 |
| 1,024 | 808.1 ms | 158.4 |
| 1,536 | 786.0 ms | 162.8 |
| 2,048 | 782.8 ms | 163.5 |
| 3,072 | 892.2 ms | 143.5 |

The 2,048 budget's 3% gain is not worth increasing activation/RSS pressure for
background work. The shipped 1,024 budget remains the footprint-oriented
default. Batch shape causes ordinary floating-point drift (minimum cosine
0.99949-0.99961 against the 1,024 run), so it is not a free exactness win.

## Provider and precision sweep

| model/provider | init | batch | rows/s | disposition |
|---|---:|---:|---:|---|
| q8 CPU, 6 threads | 48 ms | 820 ms | 156.1 | keep |
| q8 CPU, 8 threads | 48 ms | 736-780 ms | 164-174 | idle AC only |
| q8 CoreML/ANE | 496 ms | 1,944 ms | 65.9 | reject |
| q8 CoreML/ALL | about 500 ms | about 2,000 ms | 64.0 | reject |
| FP16 CPU, 6 threads | 80 ms | 722 ms | 177.3 | reject footprint trade |
| FP16 CoreML/ANE | compiled | 779 ms | 164.4 | reject |

The q8 CoreML NeuralNetwork path captured 306 of 701 nodes in 84 fragments;
395 nodes stayed on CPU. MLProgram did not accept the dynamic/unbounded export.
FP16 CoreML captured only one trivial partition per bucket, so its output was
effectively CPU work. This matches ONNX Runtime's warning that dynamic shapes
and unsupported-node fallback can erase CoreML gains.

## Int8 parity against the pinned FP32 reference

`bench/embed_model_parity.py` holds tokenizer, 1,024-token truncation, pooling,
normalization, and batching fixed, then compares complete q8-query/q8-passage
retrieval against FP32-query/FP32-passage retrieval. The aggregate below used
20 frozen queries and 128 bounded passage samples from a private evaluation
snapshot; the task text and passages are not committed.

| measure | result |
|---|---:|
| query-vector cosine, mean / min | 0.999368 / 0.998964 |
| passage-vector cosine, mean / min | 0.999148 / 0.997756 |
| top-1 agreement | 95.0% |
| mean top-5 overlap | 90.0% |
| mean top-10 overlap | 94.5% |
| mean / p99 absolute score delta | 0.001736 / 0.005578 |
| 0.82 floor disagreement | 0.1172% |
| 0.84 strong-band disagreement | 0.0% |

The int8 model passes this precision check. The remaining semantic-quality gate
is the frozen answerability evaluation, not a score-only proxy.

## Metal lane on Apple silicon

The int8 ONNX graph is not the only way to reach these vectors. The same Granite
weights, taken before quantization, run as fp16 directly on Metal through MLX.
Both engines were timed interleaved in one process, so each saw the same machine
load rather than a favorable moment:

| lane | engine | median wall, 300 messages | texts/s |
|---|---|---:|---:|
| `onnx-int8-cpu` | ONNX Runtime, CPU provider | 8.62 s | about 35 |
| `mlx-fp16-metal` | MLX, Metal | 0.79 s | about 380 |

Median of four alternating runs over 300 real corpus messages under agrep's own
1,024-token bucketing. That fixture is longer and heavier than the 128-row
short-heavy batch used by the tables above, so the two are not one scale: what
this table establishes is the **10.9x ratio** between engines on identical work.

Applying that ratio to the CPU lane's 16-18 hour 10M projection puts a full 10M
backfill near 1.5-1.7 hours, and it takes a three-hour backfill to roughly 16
minutes. Those are divisions of a measured ratio, not separately measured 10M
runs. The overnight floor the CPU sweeps above could not reach is reachable by
changing engine, not by tuning ONNX.

### The vector contract decides whether the lane opens

Rows embedded on Metal land in the same index as rows embedded by ONNX, and a
query may be embedded by either engine, so the two must produce the same space
rather than merely a good one. Capability is not evidence of that, so
`embedder._start_metal_lane` refuses to open the lane until it reproduces the
ONNX vectors numerically: worst-case cosine on a fixed probe set, against a
0.995 floor. The probes span short and long rows deliberately, because the
failure this gate exists to catch grows with sequence length and a short-only
probe set would score 0.999 and pass a broken lane.

The gate is not theoretical. A first implementation built on a general-purpose
MLX embedding library defaulted to mean pooling against agrep's CLS contract and
measured 0.878 mean cosine, 0.769 minimum, on real corpus messages while every
shape and finiteness check passed. The encoder therefore lives in
`py/mlx_modernbert.py` with CLS as the only thing it can do. Weights are pinned
by byte size and SHA-256 exactly like the ONNX artifacts, in a 90.9 MiB
`model.safetensors` alongside its config: an accelerator is not a reason to
relax provenance when those bytes decide a vector space.

| measure | result |
|---|---:|
| parity floor required to open the lane | 0.995 |
| worst-case probe cosine measured against ONNX | 0.99933 |
| fixture queries whose result changed at a threshold | 3 of 45 |

The third row is why agreement is not identity. Roughly 0.999 cosine is
arithmetic agreement, and that still permits a different top-1 or a hit that
becomes a refusal near the calibrated similarity floors.

### One store, one lane

Because the lanes are close but not identical, the lane is part of the vector
space the way sequence length is: it is recorded in `embeddings.meta`, and one
store only ever holds one of them. `embedder.resolve_lane` conforms to the rows
already on disk rather than re-deciding from the environment, so a Metal store
keeps getting Metal rows without any variable set and a CPU store stays CPU even
with one set. Where a store's recorded lane cannot open, the identities disagree
on purpose: a refusal on the query side and an announced rebuild on the build
side, because silently serving CPU vectors against Metal rows is the failure
worth being loud about.

The two failure directions are deliberately asymmetric. A predicted lane for a
brand-new store may quietly land on CPU when it cannot open, because nothing is
committed yet and a working CPU store beats an error. A lane a store already
recorded fails loudly instead, because that is the case where the wrong answer
would be silently mixing vector spaces.

The CPU lane's identity string is byte-for-byte what it was before lanes
existed, so no store built earlier re-embeds; only Metal carries a suffix.
`agrep reindex --full` is the one sanctioned lane move, discarding every row and
re-deciding from the machine default.

### When the lane is chosen

Metal is the default wherever it can actually open: Apple silicon, the `metal`
runtime present (the default install carries mlx on supported Apple silicon), and the machine idle
enough to share the GPU. No environment variable is required to get it.
`AGREP_MLX=off` opts out entirely and `AGREP_MLX=on` pins the lane through load,
which is the right answer for a foreground index the owner is already waiting
on.

The idle check runs once, when a store first picks a lane, not before every
batch. A machine too busy to share the GPU starts a CPU store rather than one
that alternates engines by load and ends up half in each vector space.

The courtesy matters more on this hardware than the name suggests: the GPU is
also the display compositor, with WindowServer measured at 46% GPU on this host,
so a backfill that saturates Metal makes scrolling and window animation stutter.
macOS exposes no unprivileged GPU-utilization metric, so the gate is one-minute
load average per core against a 0.9 ceiling. That ceiling is a measured choice:
0.7 flapped, and a flat 6.0 never yielded at all.

The interleaved A/B above is not committed as a bench harness, so the numbers in
this section are reported rather than reproducible from this tree. The behavior
around them is checkable: the parity gate runs on every lane open and is covered
by `py/test_mlx_embed.py`, and this prints which lane a machine will use:

```console
agrep setup
```

## Power and footprint policy

- The indexer starts aggressive catch-up only on AC, while idle, with at least
  25% memory available. macOS uses six threads; other systems cap at eight.
- Activity, battery, high CPU, or moderate memory selects a two-thread,
  below-normal/nice pass. Critical memory starts no pass. Battery below 30%
  defers the child entirely.
- Windows samples native system-wide CPU utilization with `GetSystemTimes`; its
  first unknown sample stays in polite mode. Unrelated CPU saturation cannot be
  mistaken for idle AC capacity.
- Each pass remains bounded and exits, returning model memory. Query residency
  remains separately pressure-aware because inert mapped/model memory is cheap
  until the host actually needs it.
- The Metal lane adds its own courtesy gate on top of this one, and its own
  90.9 MiB of pinned weights beside the 50.1 MiB int8 graph. Both are under the
  shared model root, so one directory still reclaims everything.

Reproduce the precision comparison with a verified full-precision artifact:

```console
python bench/embed_model_parity.py --reference-model /path/to/model.onnx
```
