# Round 2 evidence

All inputs are committed public synthetic fixtures. These files contain numeric
aggregates, fixture identifiers, hashes and load readings, not conversation text,
rendered results, vectors, source paths from an owner store, or private history.
The reference is the read-only 0.4.9 checkout; the product has no Python, MLX or
ONNX Runtime dependency.

`plan.json` records the protocol before final sampling. Each completed process
and each batch is retained. `*.waits.jsonl` records quiet-start waits; it is not
timing data. Load arrays are the operating system's one-, five- and fifteen-minute
averages. Start-load checks do not guarantee an otherwise idle machine or an
uncontended GPU. No sample is removed because its result or later load is poor.

`matched-final.jsonl` interleaves the original reference embedder, the preceding
Candle algorithms selected in the same executable, and the new algorithms.
It is explicitly contended. `matched-quiet-partial.jsonl` preserves the ten
completed quiet-start processes and its separate wait log; the host stopped
providing quiet windows. The protocol amendment is recorded in `plan.json`.
`matched-before-arena.jsonl` is an earlier exploratory campaign, not the final
implementation. All other sweep files are diagnostic experiments, not additional
independent final samples. Batch throughput includes tokenization and the first
batch; model loading is recorded separately.

`tails-*.jsonl` uses fresh CLI processes, ordinary freshness checks and the native
200,000-reference fixture. Query strings are replaced by public fixture IDs.
`tail-attribution.json` identifies every reproduced sample above twice its own
campaign/mode/variant median. `historical-tail-audit.json` covers the original
round-one outliers: two page-in explanations are explicitly inferences, since
their original runs lacked the necessary counters. Other attributions include
the measured stage times and counters. Process CPU counters are process-wide;
overlapping hybrid spans must not be added together.

`profile-*.jsonl` and `reference-profile-*.json` are instrumented experiments.
The original Metal profile is split at record boundaries into the parts listed
in `profile-metal-manifest.json` to keep each repository file below the privacy
gate's size limit; the manifest records its original digest and record count.
For Candle, `encode_ms` measures the call before the explicit completion fence;
`complete_ms` includes that fence and scheduling/driver delays. These are not
hardware GPU timestamps. Whole-layer spans include their child spans. CPU
process time can exceed elapsed time because the pool has six workers. Reference
MLX spans also insert fences; ONNX uses its operator profiler. These profiles
explain work and synchronization but are not throughput benchmarks.

`quantized-projections-v3.json` is the valid per-channel Q8_0 projection probe.
The two preceding attempts are retained and explicitly invalidated in
`invalidated-attempts.json`; neither supports a speed or accuracy claim. The
quantized path was not enabled in the product.

`quality-final-*.json` records the unchanged independent-oracle, token-ID,
ordered-neighbor, labeled-recall and long-tail gates, including odd batch order.
`owner-ci.json` is the real blocking CI workload run locally. A failed gate is
retained as a failure. `validation.json` records the complete workspace and
benchmark-test results. Performance limits and their reasons are defined only
in `docs/perf-budgets.json`.
