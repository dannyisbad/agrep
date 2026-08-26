# agrep - the Python side

This directory is the Python half of agrep: terminal search/recall, the semantic lane,
and the housekeeping that keeps both fresh. The Rust core (`crates/agrep-core`)
produces the transcript index these modules read.

Everything runs under the CLI's Python. Platform/Python combinations supported
by upstream ONNX Runtime can install the three optional semantic dependencies:
`numpy`, `onnxruntime`, and `tokenizers`. Other combinations retain the complete
keyword path and report semantic search as unavailable. On supported Apple
silicon, a base install carries one more dependency, `mlx`, and makes the Metal
embedding lane the default where it can open; `AGREP_MLX=off` opts out and
`AGREP_MLX=on` pins it. Torch is required by neither lane.

---

## The shared data contract

`data/` below means the active agrep data dir, which defaults to the OS user data
dir and can be overridden with `AGREP_DATA_DIR`. Pinned model weights use the
shared per-user model cache instead; `AGREP_MODEL_DIR` overrides its root.

| File | Format |
|---|---|
| `data/messages.jsonl` | one JSON per line: `{id, agent, project, session, ts, turn, who, text, model?, model_source}`. `id == "agent:session:turn"`. Produced by `agrep index`. `who=user` rows are real prompts; control/synthetic/recap/harness rows are searchable but excluded from model-attribution denominators. |
| `data/replies.jsonl` | one JSON per line: `{id, reply}`. Agent replies are sidecar rows joined by `id`. |
| `data/boundary_stats.json` | ingest-generation sidecar mapping normalized short subtokens to root-family substring/aligned counts. |
| `data/embeddings.meta` | publish-last semantic manifest. Version 2 binds the transcript generation, model identity, coverage, immutable vector segments, shadow sets, and native segment-set manifest. The model identity also names the engine lane that wrote the vectors, so a store is only ever read and extended by the lane that built it. |
| `data/embedding-segments/seg-<generation>.f32` | little-endian normalized `f32` vectors retained for incremental publication and compatibility checks. The production Granite profile is 384-dimensional; its 1,024 value is the token cap, not the vector width. |
| `data/embedding-segments/seg-<generation>.q8` / `.q8g` | native int8 candidate index and root-family grouping used for bounded top-k retrieval. |
| `data/embedding-segments/seg-<generation>.f16` | exact-rerank source for candidates returned by the q8 index. |
| `data/embedding-segments/seg-<generation>.ids` / `.hashes` | row ids and source-text fingerprints aligned to each immutable segment. Reply ids use `#r`. Rows longer than the model window embed as several vectors: the unsuffixed id keeps the head chunk and `#cN` siblings carry the rest, each fingerprinting the full source text. |
| `data/embedding-segments/seg-<generation>.refs.sqlite` | sealed source locators and filter metadata for resolving only winning rows. |

The reader still recognizes the earlier flat `embeddings.f32/.ids/.hashes`
layout so an existing index stays searchable while one background owner migrates
its unchanged vectors to segmented version 2. A verified segmented publication
retires the flat artifacts and its generation marker. Every stored row is
normalized, so cosine similarity is a dot product.

---

## The semantic lane

One pinned model, two engines that can run it:

- `embedder.py` - pinned Granite small English R2 profile: 384
  dimensions, CLS pooling, symmetric query/document encoding, and a 1,024-token
  runtime cap. Revision and per-file SHA-256 pins make model drift fail closed.
  `PROFILE_STRING` is part of the manifest identity, so a profile change cannot
  reuse vectors from another space. It also owns the lane: `default_lane` picks
  one for a store that has none, and `resolve_lane` then conforms to the lane the
  store already records, so a Metal store keeps getting Metal rows without the
  environment being set and a CPU store stays CPU even with it. Only `--full` is
  allowed to re-decide, because a half-and-half backfill is the failure the rule
  exists to prevent.
- `mlx_embed.py` / `mlx_modernbert.py` - the Metal lane: the same pinned
  weights pre-quantization, run through MLX in fp16. Measured interleaved in one
  process over 300 messages, median of 4, the ONNX CPU pass took 8.62 s and this
  one 0.79 s, which turns a multi-hour backfill into minutes. The vector
  contract is the whole risk, since rows from both engines land in one index: the
  encoder lives here rather than in `mlx-embeddings` precisely so CLS is the only
  pooling it can do (that library's ModernBERT defaults to MEAN and measured
  0.878 mean cosine against this contract while every shape and finiteness check
  passed), and `embedder._start_metal_lane` refuses to open below 0.995 cosine
  against the ONNX vectors for a fixed probe set spanning short and long rows.
  Shared-weight arithmetic drift measured 0.99875 there; a pooling mismatch
  scores about 0.77. Weights are pinned by size and SHA-256 like the ONNX
  artifacts and live under the shared model root, so an accelerator is not a
  softer path into the model directory. On Apple silicon the GPU is also the
  display compositor, so a backfill paces itself: `_embedding_backfill_policy`
  widens the interval between batches while the owner is active, on battery,
  or under memory pressure. Lane choice is a capability question, not a load
  one - a store keeps its vector space for life, so `AGREP_MLX=off` opts out
  and `AGREP_MLX=on` pins on, but a passing load spike never strands a store
  on the slow engine.
- `embed.py` - messages + replies to immutable semantic segments. Incremental by
  default; `--full` re-embeds; `--max-new N` bounds one background pass. Holds a
  cross-process claim so embedders never stack, and binds each segmented
  manifest to the transcript generation it read only while that source remains
  current. Background passes use resource-adaptive low-priority threads and
  publish/yield at 500-row boundaries if an active transcript advances. An idle
  AC-powered machine runs bounded catch-up passes back-to-back; battery,
  activity, CPU load, and memory pressure shrink or pause them. A delta
  publication writes only new segment and shadow artifacts; compaction is a
  separate bounded operation.
- `semantic.py` - the local engine every semantic owner calls: generation-exact
  freshness (`embedding_coherence`), one deduped background refresh
  (`ensure_fresh_async`), and `search()` through q8 candidate selection with f16
  reranking. `semworker.py`
  keeps headless agent searches hot in one serial process, then exits after an
  adaptive idle lease so native RSS is actually returned. A stale lane degrades
  to keyword and heals in the background; it never serves old vectors. Missing
  candidate refs also fall back immediately and start a claim-deduped
  `embed.py --background --refs-only` repair, before ONNX is loaded.
- `ask.py` - retrieval internals (sealed immutable candidate-refs snapshots with
  compact source byte locators, cached vectorized session grouping, root/child
  conversation-family diversification, and summary-vector enrichment when an enricher's
  vectors share the profile space).
  Candidate resolution reads and proves only the bounded winning source rows.
  Refs builders have their own lock and never hold the transcript ingest lock.

The CLI wires it together: classic `agrep` stays exact keyword; compact agent
search and prose recall combine keyword and meaning evidence independently (rows
labeled `"lane": "semantic"`); `-s` forces meaning; `--lexical` opts out.

### Operational behavior

The first semantic publication is a bounded newest-first slice, so semantic
coverage becomes usable before a full historical backfill finishes. Later passes
publish immutable deltas and disclose partial coverage until complete. Missing or
stale vectors and refs cause an immediate keyword fallback plus one deduplicated
background repair; a query never reads a mixed generation. The resident worker's
idle lease adapts to artifact size, reuse, and available memory. Background work
also adapts thread count and priority to battery, CPU load, and memory pressure.

Scale, resource, and quality measurements belong to the executable harnesses and
their reports under `bench/`; keeping machine-specific snapshots out of this data
contract prevents documentation from silently becoming a performance promise.

```
python embed.py --smoke 8      # quick end-to-end check on the first 8 messages
python embed.py                # incremental embed of the active data dir
```

---

## File map

| File | Role |
|---|---|
| `common.py` | compatibility re-exports plus nine store-summary, bounded-log, and log-stamping helpers. |
| `settings.py` | data-dir provenance and atomic user-setting reads/writes. |
| `index_lock.py` | Rust-shared corpus-generation lock protocol, liveness policy, and exact tombstone reclaim. |
| `removal_fence.py` / `legacy_cleanup.py` | background-removal ownership and one-shot retired-explorer cleanup. |
| `embedding_store.py` | message loading and the embedding identity, publication, commit-verification, and legacy-matrix contract. |
| `search.py` / `recall.py` / `around.py` | the terminal engines (keyword tiers, meaning lane, windowed packs). |
| `surface_policy.py` / `console.py` | dependency-light CLI vocabulary, thresholds, terminal safety, and process-safe command rendering. |
| `boundary_rank.py` | Unicode/code boundary scoring. Segmentation must match the Rust sidecar byte-for-byte: `fixtures/boundary_conformance.json` is the shared contract (tests on both sides consume it) - boundary-rule changes update the fixture first, and sidecar-affecting ones bump its BUILD_ID. |
| `compact.py` | frozen compact result pages, continuation snapshots, and safe deeper-search replay. |
| `corpusdb.py` | the derived sqlite FTS index the keyword engines ride. |
| `ownerfile.py` / `proc.py` / `lifetime.py` / `winjob.py` | exact process ownership, liveness, teardown, and Windows Job Object policy. |
| `session_context.py` / `events.py` | current-session exclusion and normalized event storage/querying. |
| `embedder.py` / `embed.py` / `semantic.py` / `ask.py` | semantic model, publication, freshness, and retrieval orchestration. |
| `mlx_embed.py` / `mlx_modernbert.py` | the Metal embedding lane: capability and GPU-etiquette gating, pinned weights, and the CLS-only encoder. |
| `embedding_segments.py` / `segment_query.py` / `semantic_q8.py` | immutable vector layout and native bounded candidate querying. |
| `semantic_segment_build.py` / `semantic_segment_compact.py` / `semworker.py` | segment construction/compaction and the leased semantic worker. |
| `explore.py` / `conceptpair.py` | read-only search, window, and enrichment data layers. |
| `indexer.py` / `indexd.py` / `indexd_runtime.py` | auto-indexing, embedding freshness, daemon ownership, and client-side health checks. |
| `audit.py` | `agrep audit`: per-file intake accounting vs an independent raw census. |
| `doctor.py` / `teach.py` / `archive.py` / `resume.py` / `livetui.py` / `tail.py` | setup checks, agent teaching, store snapshots, native resume, live views. |
| `nudge_default.md` / `nudge_codex.md` | the taught instruction blocks `teach.py` installs. Selftest hash-pins the default block against its version number and checks both blocks still name every command they route to. |
| `postcompact.py` | `agrep postcompact`: the exact pre-boundary root tail of the calling session family, as a bounded supplement to a lossy provider summary. |
| `hookinstall.py` / `hooks/` | the compaction integrations that point a resumed agent at recovery: Claude's `PreCompact`, Codex's `SessionStart` with matcher `^compact$`, and the shared pi/oh-my-pi extension. Installed by `agrep setup` unless `--no-hook`; none overwrites a user's own hook or extension. |
| `hookless/` | the agent observation layer: store tailing (`live.py`), process discovery, launch capture, per-agent resume/cwd resolution (`native.py`). Imports nothing from the product layer (see CONTRIBUTING.md). |
| `selftest.py` | the whole feature matrix, one pass/fail run. |
