# In-process semantic search

The embedding implementation uses pinned Candle 0.9.2 and the spike's ModernBERT backbone. The local backbone preserves the checkpoint architecture; fused operations are checked against the full-precision F32 oracle. Metal uses finite F16 masks and Candle's pinned fused attention kernel for sequences longer than eight tokens; short sequences retain the original path because the vector kernel does not honor explicit masks. Normalization uses the existing fused layer-normalization operation with an explicit zero bias. CPU queries use F32. Their GeGLU stage fuses the existing Candle erf-and-gate expression and parallelizes independent rows; it does not replace the activation with an approximation. Background inference selects F16 Metal on macOS when a device opens and F32 CPU elsewhere. On macOS the CPU links Apple's Accelerate framework. The semantic dependencies require no additional C compiler, external BLAS installation, Python, ONNX Runtime, daemon, or IPC service.

The pinned IBM revision is `2ab6fa8ea2d674564defd37171ae19079b864b33`. `semantic_model::FILES` records exact SHA-256 digests and lengths of its config, tokenizer and weights. Provisioning serializes downloads, verifies temporary files before renaming them, and preserves prior verified files on a failed download. It records the pinned hashes with file sizes, timestamps and file identity in `verified.json`; unchanged files reuse that verification. Any metadata mismatch triggers full hashing before inference. The cache is written only during explicit provisioning, and read-only queries do not update it. `AGREP_MODEL_DIR` names the actual model directory; otherwise it is `<data root>/model/granite-embedding-small-english-r2`.

`index --semantic` explicitly provisions and drains the embedding queue. Setup should call the same `semantic::provision` API after consent. Automatic hybrid never downloads. Missing-model `-s` prints `semantic model not installed: run agrep setup` and exits 2. A semantic-only zero exits 1, including when coverage is partial. Coverage is always numeric: `semantic N/M rows`, and `semantic_coverage: {indexed, total, pending, fraction, complete, order}` in JSON.

## Preparation and publication

Inputs have no query/document prefix. The pinned tokenizer applies NFC, right truncation at 1024 total tokens including CLS/SEP, and right padding. Final hidden-state token zero is pooled and L2 normalized to 384 dimensions. Inputs are sorted by token count and restored to original order. CPU batches allow 16,384 padded tokens and Metal batches 32,768, with quadratic attention-cell caps of 4,194,304 and 8,388,608 respectively. Thus full-length inputs remain limited to four CPU or eight Metal rows while 200-token inputs use substantially wider batches. No shorter window is selected by the product.

Indexing uses the reference's text windows: at most 1022 NFC UTF-8 bytes, a preferred newline in the latter half, up to 102 characters of overlap, and at most 32 chunks (first eight contiguous, then uniform endpoints-inclusive sampling). Pi recap preambles are stripped only before a recognized setext heading in the first 4096 characters.

`semantic_queue`, `semantic_refs`, `semantic_chunks`, and `semantic_blocks` live in `index.db`. Prose insert/update/delete triggers maintain the queue and invalidate references inside the same transaction as source changes. The content key combines the full source text's digest with its preparation variant. A completed inference attaches only when the same row ID and digest are still visible. A row changed during inference stays queued with its new digest. Identical text/preparation reuses existing chunks.

Blocks contain scaled signed int8 vectors and F16 rerank vectors. The block is written, synced and renamed before the referencing SQLite transaction commits. Queries use the existing SIMD int8 dot product and conservative Cauchy-Schwarz error bounds before F16 refinement; a fixed candidate count never truncates a filter/family search. The retained ranked list is bounded by the requested result count, while admissible quantization-error bounds determine when scanning can stop and ties remain deterministic across agents. Published blocks remain immutable so a WAL reader can finish against its snapshot. Background work reclaims unreferenced blocks, including files left before reference publication, only after their modification time is more than ten minutes old.

`__work` holds `work.lock` for its lifetime, loads weights once, selects newest queue rows, embeds length-bucketed batches, and acquires `writer.lock` only while publishing each batch. It exits when drained. Commands with a prepared model and a backlog start a detached process. Normal output contains no process coordination state. Failures go to a bounded `log/agrep.log` with one rotated copy.

## Retrieval and integration surface

`semantic::search(root, options, family_diverse)` returns normal `query::Results` and numeric coverage; it requires no change to `Filters`, `Options` or `Hit`. It honors scope, role/model/date filters and side-family diversity. The semantic default excludes control, synthetic, recap and harness roles, which remain available through an explicit `--who`.

The automatic lane runs on a thread alongside `query::search` for eligible natural language in compact agent output. Identifier-only patterns, explicit matchers, lexical/count/coverage output, tools-only scope and non-score sorts stay lexical. The reference's floor 0.82 and strong threshold 0.84 remain unchanged. The merge preserves strong lexical leads, deduplicates rows/text/families, allows strong meaning rows to lead scattered lexical results, and never converts a lexical zero into success using only sub-strong neighbors.

CLI command modules are `cmd/search.rs`, `cmd/index.rs`, and `cmd/work.rs`. The main module's shared changes are their dispatch, the `index --semantic` flag, optional search maximum (40 keyword / 10 semantic), semantic count conflicts, automatic scheduling in the search/index/status commands and after reindex/settings, and status coverage. `render::emit_semantic` adds coverage, `lane: semantic`, `sem_score`, chat units and visible `meaning match` labels. `Style` gains `semantic: bool`.

The caller-identity slice must carry its final self/family exclusions through semantic options too. The integration/setup slice must invoke `semantic::provision` after accepted model consent. No legacy semantic storage or process state is read.

## Reproduction

All commands require an isolated `AGREP_HOME` and `AGREP_DATA_DIR`. Build/test commands use `nice -n 10`.

- `cargo test --workspace` includes stale-inference publication, invalidation, queue ordering/locking, scope/family retrieval, missing-model output, numeric partial-zero output, reference chunking, and the frozen labeled F32-to-q8/F16 recall gate.
- `cargo run --release -p agrep-core --example semantic_eval -- MODEL_DIR cpu` compares real model token IDs, vectors and top five neighbors with the committed full-precision ONNX oracle. `metal` checks the F16 device path. It also runs the four labeled reference recall tasks.
- Append `batch` to measure the committed 128-row, 200-token corpus. An additional token-limit argument is experimental measurement only; the product keeps 1024.
- `semantic_scale DATA_DIR 200000` constructs a disclosed synthetic vector scale fixture from deterministic perturbations of the ONNX corpus. It does not claim to have run 200,000 model inferences. The separate labeled model gate establishes retrieval quality.

The committed oracle contains 40 public synthetic strings, their token IDs and 384-dimensional full-precision ONNX vectors. Ten queries label the corresponding ten document sessions; required recall@5 is 1.0. The reference's four-task fixture is also committed. On this oracle, q8 alone changed three ordered top-five lists (one top-five membership); this is why F16 refinement is present.

## Measured model and fixture parity

The actual optimized CPU model produced minimum cosine **0.9999999999985653** against the 40-text full-precision ONNX oracle, with identical token IDs, 10/10 ordered top-five lists, labeled recall@5 1.0, all four reference task labels at rank one, and 10/10 long-tail labels at rank one. The actual F16 Metal model also preserved token IDs, all ten top-five lists and the four reference rank-one labels; minimum cosine was 0.9999823763926029. `measurements/final-*-quality.json` retains the final-source reports. The executable quality gate checks both CPU and Metal cosine thresholds and all three labeled recall sets.

The same 20 native pi fixture conversations were ingested independently by 0.4.9 and 0.5.0. Their explicit semantic CLI queries produced the same top handles:

| Query | Both versions' top handle | 0.4.9 cosine | 0.5.0 cosine |
|---|---|---:|---:|
| connection pool timeout | `@semantic-fixture-000:0.d9df` | 0.915167 | 0.921074 |
| Rust borrow checker | `@semantic-fixture-001:0.3325` | 0.898544 | 0.909298 |
| database migration failure | `@semantic-fixture-002:0.e956` | 0.879869 | 0.885142 |
| Metal attention NaNs | `@semantic-fixture-004:0.27b8` | 0.916306 | 0.927193 |

The score differences are expected: 0.4.9 uses quantized ONNX, while this profile uses F32 query inference and F16-stored Candle document vectors. Both versions report 20/20 covered rows, chat result units, matching full-text handles, and exit 0. Raw JSONL and stderr are retained in `measurements/side-by-side.json`, `reference-semantic.jsonl`, and `reference-cases.json`. A fresh index with the final optimized binary again matched all four reference handles; `final-cli-fixture.json` retains those final scores and also verifies detached queue draining after `status` exits.

Coverage counts canonical prose rows; all chunks of a row must be present before that row counts as embedded. This makes the numerator auditable against the transactional source table rather than counting multiple chunk vectors as separate conversations.
