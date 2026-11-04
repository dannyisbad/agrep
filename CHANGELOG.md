# Changelog

## 0.5.0 — 2026-10-30

- One Rust executable handles search, context recovery, live views, integrations
  and local storage. Python wheels and npm are binary delivery mechanisms.
- A new `agrep/0.5` data directory and transactional SQLite index replace older
  agrep formats. There is no migration: build a fresh index and run `agrep setup`
  again. Before upgrading, run `agrep remove` with 0.4 to remove its blocks,
  hooks and uninstall sentinel. Save old archives before deleting old data.
- Search reads a committed snapshot and checks for changed native transcripts.
  Source updates are processed automatically; the first index is built inline,
  parsing sources while it writes earlier ones, so it holds a bounded share of
  the history in memory whatever the history's size.
- Meaning search runs in process with Candle and a pinned model; a missing model
  leaves automatic hybrid search on keywords and is shown by status. Explicit
  `-s` requires the model. Coverage is reported as indexed/total rows.
- Six platform wheels bundle the executable. npm verifies package-pinned hashes
  before caching and executing the matching GitHub release binary.
- Linux and Windows arm64 builds require ARMv8.2-A with FP16 (Raspberry Pi 5,
  Graviton2 and later). Raspberry Pi 3/4 and Graviton1 are no longer supported.
- CI runs tests and a blocking full-process performance comparison with agrep
  0.4.9 on Linux, macOS and Windows, including arm64 runners: every measured case
  must beat 0.4.9 at median and p95. Release publication requires it.
