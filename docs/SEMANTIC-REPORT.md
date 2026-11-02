# Semantic slice report

The semantic lane is implemented in process with pinned Candle 0.9.2, verified model provisioning, content-keyed immutable q8/F16 blocks, transactional references and an embedding queue, detached queue draining, semantic-only search and parallel compact-profile hybrid search. No earlier agrep data or process format is read.

**Performance acceptance remains open.** The measured fixed budgets have not all been achieved. The rows below are observed results, not relaxed limits or projections.

## Integration surface

- `crates/agrep-core/Cargo.toml` and `Cargo.lock`: exact Candle 0.9.2 and tokenizers 0.22.2; pure-Rust HTTPS provider; macOS Metal/Accelerate feature activation; `half` and `memmap2`. All six target dependency audits show bundled SQLite as the only `cc` build dependency.
- `crates/agrep-core/src/lib.rs`: exports `semantic` and `semantic_model`; private `semantic_bert`, `semantic_policy`, and `semantic_store` modules.
- `semantic::begin(root, connection, options, family_diverse)` and `finish(connection, work)` keep all SQL on the caller's WAL snapshot while query inference and q8 scanning run on another thread. `search` is the standalone convenience API. `search_vector` supports frozen-vector tests. **No fields were added to `Filters`, `Options`, or `Hit`.**
- `semantic::{provision, initialize, schedule, work, work_explicit, installed, coverage}` are the setup/index/status integration APIs. Setup must call `provision` after model consent, then initialize/schedule after its ordinary index is ready. Explicit `index --semantic` already does provisioning and a full queue drain.
- `main.rs`: `cmd` declarations; search maximum defaults and semantic count conflicts; `index --semantic`; search/index/`__work` dispatch; scheduling in the owned search/index commands and status/reindex/settings dispatch arms; status model/coverage fields. Search, index and work implementations live in their required `cmd/*.rs` files.
- `render.rs`: `emit_semantic` preserves ordinary `emit`; `Style` adds `semantic: bool`. It emits numeric coverage, semantic row fields/labels and chat units for `-s`.
- The caller-context integrator must apply its new self/family filters to semantic retrieval too. `--coverage`'s broader keyword recovery remains the engine slice's responsibility; this slice adds numeric semantic coverage.

## Correctness evidence

The committed oracle contains 40 synthetic texts, independent full-precision ONNX vectors and token IDs, ten labeled query/session pairs, the reference's four-task recall fixture, and exact reference chunking cases. The gate requires recall@5 1.0 and matching ordered top-five neighbors after q8/F16 ranking. Model checks additionally cover the four reference tasks and ten long rows whose answer is beyond token 128 but inside the 1022-byte row window.

- F32 CPU minimum cosine against the ONNX oracle: **0.9999999999985653**; token IDs identical; 10/10 top-five lists; recall gates 1.0.
- F16 Metal with finite fused-attention masks: **0.9999823763926029**; token IDs identical; 10/10 top-five lists; recall gates 1.0.
- q8 alone changed three ordered top-five lists, including one membership change. F16 refinement restores the oracle ordering; its inclusion is evidence-driven.
- The first fused-attention attempt produced a non-finite embedding. Its failure is retained. Finite masks prevent all-masked tiles from producing `-inf - -inf`; the corrected path passed the unchanged oracle.
- Four independent 0.4.9/0.5.0 CLI comparisons returned exactly the same top handles. `side-by-side.json` retains raw envelopes, rows, stderr, scores and timings. The final optimized binary repeated all four matches on a fresh index (`final-cli-fixture.json`), then proved that detached work drained a newly queued row to 21/21 coverage and released `work.lock` after `status` exited.
- Regression coverage includes stale text and recap-preparation races, atomic publication, canonical row counts, queue priority, lock exclusivity, filters, family diversity, deterministic ties, winning-chunk snippets, optional failure preserving lexical answers, first-use missing-model output and partial-zero exit 1.

Final validation: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, and `cargo test --workspace` pass. The workspace suite has **66 passing tests, zero failures and zero ignored tests**. Transcripts are in `measurements/`; all runs used nice 10, and CLI tests use isolated homes and data directories. Windows x64 and ARM64 checks were attempted; both stopped in bundled SQLite because this host lacks Windows C headers. They are **not** reported as passed cross-builds.

## Measurements and interpretation

All measurements use nice 10. Earlier 128-row samples used six CPU threads. The later bulk protocol processes sixteen 128-row batches in one model instance, includes the first batch, and uses the machine's normal twelve-thread capacity. Every row in the 128-row batch fixture has exactly 200 tokenizer tokens, including special tokens (`batch-token-counts.json`). It records every batch; none is discarded as warm-up. These protocols are labeled separately and must not be conflated.

| Campaign | CPU rows/s | Metal rows/s | Fixed target |
|---|---:|---:|---|
| Initial 128-row, full window | 4.43 | 32.21 | 60 / 600 |
| Experimental 128-token window | 7.79 | 61.56 | unchanged |
| Larger batches + fused attention, 128-row samples | 6.84 | 55.43 | unchanged |
| Full-window sixteen-batch reuse, before fused normalization | 11.76 | 47.55 | unchanged |
| Fused normalization, sixteen-batch reuse | 15.16 | 52.82 | unchanged |
| Parallel CPU heads, sixteen-batch reuse | 37.58 | unchanged | unchanged |

The shorter-window experiment preserved short-passage recall but reduced long-tail recall from **10/10 to 1/10**. It is not used by the product.

The 200k fixture uses real native-source ingestion, keyword indexes, 4,000 source files and 200,000 vector references. Its vectors are disclosed deterministic perturbations of the ONNX corpus, not a claim of 200,000 model inferences. The first build took 57.43 s. The full-process hybrid campaign before the final refinements measured p50 917.71 ms; the verified-model campaign measured p50 768.57 ms and p95 17,770.06 ms. An independent semantic-only run measured p50 215.02 ms. These all retain full process exit and normal freshness checks. They do not establish the required 200 ms hybrid budget.

Host contention was substantial: the initial embedding campaign recorded load averages 483/736/423 on twelve logical CPUs; later campaigns record their own loads. This is context for the observations, not a budget multiplier or a pass.

## Intentional differences and remaining risks

- A single Candle profile replaces old ONNX/MLX profiles. Scores differ from old quantized ONNX; labeled ranking is checked. No earlier vectors, owners, manifests, daemons or IPC protocols are accepted.
- Missing-model `-s` follows the binding one-line setup instruction and exit 2. Automatic hybrid stays lexical without downloading. `index --semantic` is the complete explicit provisioning path in this branch.
- Coverage counts canonical prose rows, after all their chunks are published, rather than counting each chunk as another conversation. Numeric fields retain the reference names (`indexed`, `total`, `pending`, `fraction`, `complete`, `order`).
- Model verification is cached only after explicit SHA-256 provisioning. The cache binds the pinned hashes to file metadata; changed or unverified files are hashed before inference. Automatic queries never write it.
- Immutable unreferenced vector files are retained to protect old reader snapshots. Long-term disk reclamation is not implemented here.
- Windows compilation still needs validation with an actual Windows SDK; Linux targets were not installed on this host. RustCrypto's selected TLS provider remains an alpha dependency, as in the spike.
- Performance targets remain unmet in these runs. The keyword engine also materially affects full hybrid latency; its concurrent replacement must be integrated before claiming the final hybrid budget.
