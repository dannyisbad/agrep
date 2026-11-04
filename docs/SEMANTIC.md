# Meaning search

Meaning search runs a pinned embedding model inside agrep itself, with no model server,
IPC or Python. A search embeds its query on the CPU in the command's own process; the
detached `agrep __work` process embeds indexed rows in the background. Keyword
machinery is described in [ENGINE.md](ENGINE.md), output contracts in
[OUTPUT_CONTRACTS.md](OUTPUT_CONTRACTS.md), and how meaning rows join a keyword page in
[SEARCH_RANKING.md](SEARCH_RANKING.md).

## Model and provisioning

- The model is `ibm-granite/granite-embedding-small-english-r2` at revision
  `2ab6fa8ea2d674564defd37171ae19079b864b33` (ModernBERT, 384 dimensions), run with
  Candle 0.9.2. On macOS Candle uses Accelerate on the CPU and Metal on the GPU.
- `semantic_model::FILES` pins the length and SHA-256 of `config.json`, `tokenizer.json`
  and `model.safetensors`. They live in `model/granite-embedding-small-english-r2/`
  under the data root, or in `AGREP_MODEL_DIR`.
- Only `agrep setup` (after consent) and `agrep index --semantic` download the model
  (`semantic::provision`). Automatic hybrid search never downloads. The files come from
  huggingface.co over HTTPS (ureq with rustls), each into a `.partial` file that is
  fsynced and verified before it is renamed; a failed download leaves verified files in
  place. `download.lock` serializes provisioning.
- Provisioning records the verified files' sizes, times and identities in
  `verified.json`, so later loads hash again only when that metadata changed. It also
  writes F32 and F16 safetensors, a compact tokenizer and the query tokenizer's tables,
  bound to their sources by `converted.json`.
- `agrep set embeddings off` turns off automatic hybrid search and background embedding;
  `-s` still searches the vectors already stored, and `index --semantic` still embeds.

## Text preparation

- Inputs are NFC-normalized, framed with CLS/SEP and truncated on the right at 1,024
  tokens. The first token's final hidden state is the embedding, L2-normalized.
- A prose row is split into windows of at most 1,022 NFC UTF-8 bytes, preferring a
  newline in the window's second half, overlapping by up to 102 characters. A row with
  more than 32 windows keeps the first eight and 24 sampled evenly from the rest, the
  last included. A pi recap drops the preamble before a setext heading found in its
  first 4,096 characters.
- Bulk embedding sorts inputs by token count and batches them under a padded-token
  budget, an attention budget and at most 25% padding, then restores input order.

## Query path

Search uses the meaning lane when asked (`-s`) or automatically. Automatic hybrid runs
on the compact agent page for a natural-language query (`semantic::eligible`: two or
more ordinary words of four or more letters, or one with an acronym, identifier or
number) when the model is installed, `embeddings` is not off, the sort is by score, and
none of `--json`, `--flat`, `-l`, `--lexical`, `-w`, `-E`, `-c`, `--count-by-tier`,
`--coverage` or `--who tool` is given.

- The lane starts on its own thread while the keyword lane runs. It lists the snapshot's
  vector blocks and maps them while the query model loads; then it embeds the query,
  scans, and, if the keyword lane is still running, pre-reads the rows it will resolve.
- The query model runs on the CPU in F32. With converted weights it reads the projection
  and token-embedding matrices in place from the mapped file and uses the query
  tokenizer tables; its vectors equal the bulk CPU model's bit for bit.
- Before freshness work starts, a read-ahead thread samples the model's mapped pages and
  asks the kernel to load them when some are not resident (Unix only).
- Meaning rows come only from visible prose rows whose digest still matches the embedded
  text; control, synthetic, recap and harness rows are left out unless `--who` names
  them. Filters and caller exclusion apply as in the keyword lane.
- By default one result is kept per conversation family; `--all-side-chats` keeps one
  per session. A meaning row is labeled `meaning match`.
- `semantic_policy::FLOOR` (0.82) is the lowest cosine that can be shown. Merging
  meaning rows into a keyword page (`hybrid::merge`) takes at most three, needs 0.84
  unless the page has strong keyword evidence, and makes the total a floor; see
  [SEARCH_RANKING.md](SEARCH_RANKING.md).

Without the model, automatic hybrid quietly stays keyword-only and `status` says so;
`-s` prints `semantic model not installed: run agrep setup` and exits 2. In automatic
hybrid, a failure of an installed model prints one stderr line, logs the cause to
`log/agrep.log` (under `AGREP_DATA_READONLY`, the line carries the cause instead) and
keeps the keyword results. An explicit `-s` never answers with keyword
rows: a failed lane, or a query the meaning policy refuses, prints `semantic search
unavailable: <reason>` (under `--json`, a record with `error.code` `semantic-unavailable`)
and exits 2. A semantic-only search that finds nothing exits 1, also when coverage is
partial.

## Vector storage and scan

- Prose triggers keep `semantic_queue` current in the same transaction as the rows: a
  visible row is queued on insert and re-queued when its text, digest, visibility,
  speaker or agent changes; a deleted row leaves the queue and its references. A first
  build runs without them and queues every visible row in one statement before it
  commits.
- The worker embeds the queue newest-first in batches of 128 rows, and stops between
  batches while a writer or an explicit command waits. Identical content
  reuses its existing chunks: `semantic_chunks` is keyed by a content key (the row
  digest plus its preparation variant), and `semantic_refs` maps each row to it.
- A batch's vectors go into one immutable block file `vectors/<sha256>.q8f16`, named by
  the SHA-256 of its bytes. Each record is a little-endian F32 scale, 384 int8 values
  and 384 F16 values. The file is written as `.pending`, fsynced and renamed (and the
  directory fsynced on Unix) before one transaction under `writer.lock` records the
  block and attaches the batch's rows. A row attaches only if its id and digest are
  unchanged and it is still visible; otherwise it stays queued.
- Coverage is the trigger-maintained `semantic_ref_count` over the visible prose rows. A
  row counts once every chunk is published. Partial coverage is a number
  (`semantic N/M rows` on stderr; `semantic_coverage` with `indexed`, `total`,
  `pending`, `fraction`, `complete` and `order` in JSON), never an error. As in 0.4.9,
  stderr names it only below 99% of the rows embedded, and on a page of hits only on a
  terminal or a compact page; a miss always names it.
- The scan quantizes the query to int8 and takes int8 dot products with SIMD (AVX2 on
  x86_64, NEON with or without the dot-product extension on aarch64). A Cauchy-Schwarz
  bound covers both quantization errors; each row whose bound reaches the floor gets its
  exact cosine from the F16 copy, computed eight rows at a time with the same result as
  a row-at-a-time loop. Scans of 32,768 vectors or more use the Rayon pool.
- Candidates are resolved in descending bound order against the command's snapshot and
  the scan stops once no remaining bound can enter the page (`want` is the page size, at
  most 200), which makes the total a floor.
- `reclaim` deletes chunks no row references, blocks left without chunks, and block
  files nothing references once they are ten minutes old, so older snapshots can finish
  reading them. `reindex --full` drops every reference and block record and re-queues
  all visible rows.

## Quality gates

- `cargo test --workspace` runs `crates/agrep-core/tests/semantic_quality.rs` on a
  committed oracle (`tests/fixtures/semantic-eval.json`: 40 public synthetic texts with
  their token ids and full-precision ONNX vectors). For the ten labeled queries over 20
  documents, the int8 scan with F16 rerank must reproduce the exact ordered top five,
  and recall@5 must be 1.0. Chunking must match the reference windows in
  `semantic-chunks.json` exactly.
- `cargo run --release -p agrep-core --example semantic_eval -- MODEL_DIR provision`
  downloads the pinned model into an explicit directory. It never uses a user's data
  root.
- `semantic_eval MODEL_DIR cpu` checks the real model against the oracle: identical
  token ids, minimum cosine 0.99999999999, ordered top-five agreement on all ten
  queries, recall@5 of 1.0, rank one on the four tasks in
  `semantic-recall-reference.json`, and ten of ten long-document cases. It also checks
  that the query path maps its weights, that its tokenizer tables agree with the
  library, and that its vectors equal the bulk model's bit for bit. CI runs it on every
  target and it blocks.
- `semantic_eval MODEL_DIR metal` checks the F16 Metal path with a minimum cosine of
  0.9999 and the same recall checks. `semantic_eval MODEL_DIR cpu batch` measures bulk
  throughput on `fixtures/semantic-batch.json`; it is not a gate.
- `bench/semantic_owner.py` builds a store of at least 200,000 vector references with
  `semantic_scale` (synthetic vectors derived from the oracle) and runs 20 fresh
  semantic and hybrid queries with the real query model. Every query must return meaning
  rows; latency is printed against `docs/perf-budgets.json` and does not fail the check.
  Speed is gated by the matched comparison in [DISTRIBUTION.md](DISTRIBUTION.md).
- The Metal kernels have GPU-only tests; see the release check in
  [DISTRIBUTION.md](DISTRIBUTION.md).

## Limits

- English model; the query window is 1,024 tokens and a long row is represented by at
  most 32 windows.
- Coverage grows in the background, newest rows first; until a row is embedded it is
  found only by keywords.
- Tool output is not embedded.
- Background embedding uses Metal only on Apple GPUs that support every kernel the lane
  needs; other machines embed on the CPU.
