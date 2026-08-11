# Semantic segmented-index design

Status: production contract. This replaces the monolithic semantic publication;
the v1 reader remains for migration and rollback.

## Why this shape

A small refresh currently rewrites the complete f32 matrix, ids, hashes, q8,
f16, group map, and candidate-refs database. Segmenting only the vectors would
leave another O(index) write in refs. The unit of publication is therefore one
immutable vector-and-metadata segment, not just one matrix fragment.

## Authoritative generation

`embeddings.meta` version 2 is the single publish-last authority. It binds:

- the exact transcript source generation;
- model identity and dimension, where identity includes the engine lane that
  produced the rows, so a store built on one lane is never served by the other;
- complete or partial coverage;
- an ordered immutable segment set;
- every shadow file that removes an older physical row;
- live and physical row counts plus the next row-reference high-water mark.

All segment files have unique generation names under `embedding-segments/`.
Readers load the manifest bytes, validate and map every referenced artifact,
then reread the manifest bytes. Movement retries the open. Query permission no
longer depends on a second marker for v2; one atomic manifest swap cannot expose
a vector generation without its source binding.

Each segment contains aligned f32, q8, f16, global-family group ids, ids,
hashes, and immutable candidate refs. Candidate refs store text and filter
metadata, not byte offsets into rewritten JSONL. A row reference is a monotonic
u64 assigned from the manifest high-water mark. A segment descriptor records
its contiguous row-reference base and row count.

## Update and shadow rules

One delta publication contains new or changed current rows plus a sorted unique
u64 shadow list:

- a new id appends one live row;
- a changed id shadows its currently live row and appends its replacement;
- a deleted id only shadows its currently live row;
- an unembedded changed row is shadowed immediately and makes coverage partial;
- repeated updates always shadow the latest live row.

Validation rejects duplicate live ids, duplicate shadows, self/future targets,
out-of-range targets, and an already-dead target. The native scanner builds one
small exclusion bitmap when it opens the immutable set and checks liveness
before scoring, so stale high-scoring rows cannot consume the candidate heap.
Python rechecks liveness and the stored text hash before rendering a result.

Family ids are stable global u32 values allocated by a serialized writer
catalog. Catalog state may be ahead after a crash, but ids are never reused and
the group artifacts in the manifest are query authority. Group zero is reserved
for roles excluded by the default semantic path.

## Publication and crash recovery

1. Snapshot and validate the current manifest.
2. Write, flush, and fsync every new artifact to unique temporary names.
3. Rename them to unique final names; never replace a segment pathname.
4. Revalidate the transcript and all descriptors.
5. Write and fsync the complete v2 manifest temporary.
6. Atomically replace only `embeddings.meta`, retrying bounded Windows sharing violations.
7. Advance pending-plan state and garbage-collect later.

A crash before step 6 leaves the old generation authoritative. A crash after
step 6 leaves the complete new generation authoritative. Orphan artifacts are
safe to scavenge by owner identity. On Windows, a sharing violation during
cleanup is deferred; publication never waits for an old mapped reader.

The v1 migration creates one immutable base segment with streaming copies. It
validates the complete bundle before the v2 swap, then retires the monolithic
matrix, row sidecars, and legacy accelerator. Windows sharing violations defer
deletion to a later refresh or resident-worker release.

## Query contract

The Rust worker opens the immutable segment-set manifest once, quantizes a
query once, scans every live q8 row, and returns global u64 row references.
There is no sound query-independent cosine ceiling below 1, so the scanner does not
pretend segments can be skipped and does not add ANN behavior.

Default family-diverse retrieval uses stable global group ids and one global
top-H accumulator. Exact f16 reranking is batched by segment, then merged with
score-descending, row-reference-ascending ties. Explicit metadata filters compile
to a cached global eligibility bitset; Rust intersects it with shadows before the
flat or grouped heap. A missing native scanner may use dense f32 only below 250k
rows. Larger generations fail to lexical instead of scanning gigabytes.

Missing, structurally corrupt, or generation-mismatched data invalidates the
whole set. Publication verifies full descriptors and payload checksums; normal
query opens validate structure and sizes without rehashing multi-gigabyte files.
A reader never silently drops one segment.

## Compaction and resource policy

The first implementation has one base and at most 16 deltas. Eight deltas or
dead physical rows reaching 5% of live rows is the soft compaction trigger.
Sixteen deltas is a hard cap: if the existing embedding governor refuses a
compaction, semantic refresh defers and the coherent prior generation remains.

Compaction admission and streamed copying run under the existing battery, CPU,
and memory governor; native derivation is timeout-bounded but not interruptible.
It writes a new base containing only live rows, validates row and artifact
alignment, and swaps one new manifest. Same-volume staging files are adopted
instead of copied a second time, and a free-space preflight bounds peak storage
to roughly the old and new generations. It never mutates or replaces mapped
inputs. Row references may be renumbered because no reader can observe two
manifests as one generation.

## Release proofs

- new, change, delete, repeated-update, and delete/re-add chains;
- a stale row that would rank above every live row;
- exact monolith-versus-segment row and family top-k parity with stable ties;
- arbitrary-filter parity and candidate-ref hash proof;
- process kill immediately before or after the manifest swap yields one coherent generation;
- a Windows reader can hold generation A while generation B publishes;
- cleanup failure on a mapped Windows artifact is harmless and later retryable;
- full rebuild replacement and deferred legacy-layout retirement;
- 1,000-row publication against a logical 10M base writes under 100 MiB and
  completes under 10 seconds;
- real native flat/grouped parity at 1, 8, and 16 deltas with stable ties;
- real q8 flat/grouped parity before and after compaction.

The scale proof constructs a sparse logical base and runs the production q8/f16/
refs preparation plus publication for a real 1,000-row delta. The sparse base
proves publication mechanics rather than searchable 10M recall or ONNX inference.
Linux and macOS exercise 10M logical rows; Windows uses a bounded 100k base because
a bare file extension is not an NTFS sparse-file proof.
