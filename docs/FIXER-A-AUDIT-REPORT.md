# Fixer A audit corrections

Base: `6c99c51a`. All assigned findings were valid. No findings were rejected.
This report covers this audit round only; it does not claim the other branches'
pending work is complete.

## Commits

- `2ca2ec35` — Secure local data and restore explicit command contracts.
- `2b20a9fa` — Enforce complete performance and test-coverage gates.
- `ac1dda72` — Close skip-policy comment and literal loopholes.
- `3c80beb6` — Return keyword results after explicit semantic failures.

## audit-hacks-legacy.md

| Finding | Commit | Correction and regression coverage |
|---|---|---|
| 1 | `2ca2ec35`, `3c80beb6` | Installed-model failures log the full cause and disclose one stderr line. Automatic and explicit search/recall retain keyword answers. Pack stops retrying a failed lane during the same command. Status/doctor use verified hashes/stamps. Corrupt-model integration coverage checks answers, log, diagnostics, explicit requests and one-line disclosure; a query-thread panic test preserves the cause. |
| 4 | `2b20a9fa` | Every dense latency path uses half its ordinary budget, for median and p95. The gate test covers census, incremental, changed-source search, version control and dense keyword cases. |
| 5 | `2b20a9fa` | CI retains the semantic benchmark's downloaded model and runs `semantic_eval <model-dir> cpu` as a blocking command. The real-model CPU quality gate was also run locally. |
| 7 | `2b20a9fa`, `ac1dda72` | Both registry sections are enforced exactly. Body gates, early returns, inner attributes, out-of-line module gates and comments between attributes are covered. Python expected failures/aliases and JavaScript skip/todo options plus runner summaries are rejected. Four checker tests cover these cases. |
| 8 | `2b20a9fa` | The compiled Windows fixture now verifies omp selection and pi fallback. The doctor registry reason accurately records the remaining Windows interactive-consent coverage limitation. |
| 9 | `2ca2ec35` | An ordinary index command starts detached work without manually invoking `__work`; the test waits for the configured hook and then verifies work-lock release and cleared pending state. |
| 10, archive-test portion | `2b20a9fa` | Removed both 150 ms assumptions. Tests directly verify SQLite/file-lock contention, release the held lock and verify the operation succeeds. The explorer test belongs to B. |
| 11 | `2b20a9fa` | Every warm case checks median and p95; every first-index sample checks its limit. Raw samples cannot be hidden behind understated summaries. Regression tests cover slow tails, individual slow builds and forged summaries. |
| 12 | `2ca2ec35` | Digestless result handles are rejected by the shared parser and CLI entry points. Tests cover around, recall and chats, while positional around remains available. |
| 13 | `2ca2ec35` | `reindex --full` rebuilds rows, then clears all vector references/chunks/blocks and queues every visible row. Reset holds work.lock and writer.lock so an in-flight embedding batch cannot republish stale work. Integration coverage checks the resulting tables and queue. |
| 14, set/index/reindex portions | `2ca2ec35`, `3c80beb6` | Setting text describes downloads and meaning search. A shared public refresh serializer omits `skipped_lock` from index/reindex output. Integration assertions cover the text and both commands. Explorer wording belongs to B. |
| 15 | `2ca2ec35` | A leftover first-index file is reported as interrupted unless the writer lock is actually held. The test checks both states. |

## security.md

| Finding | Commit | Correction and regression coverage |
|---|---|---|
| 1 | `2ca2ec35` | POSIX data/archive/model/log directories are created 0700. Validation tightens an existing owned root; diagnostics report remaining exposure, including an overridden model directory. Tests check creation, tightening and doctor reporting. |
| 2 | `2ca2ec35`, `3c80beb6` | SECURITY describes current directory protection, sanitizer, model verification and fail-closed npm behavior. README and handle/output docs also remove stale implementation-status wording and document current behavior. |
| 4 | `2ca2ec35` | Native resume rejects invalid session identifiers and relative working directories before constructing arguments. A portable test covers hostile and valid values. |
| 6 | `2ca2ec35` | One sanitizer in surface handles C0/C1/DEL and format/bidi characters. Ingest delegates to it. A regression test covers the shared behavior. |
| 7 | `2ca2ec35` | Around and postcompact sanitize transcript-derived header fields. An integration test injects control/bidi content and checks both outputs. |
| 9 | `2ca2ec35` | Doctor repairs missing enrolled files but refuses changed files with “file has user edits.” Integration coverage checks preservation and subsequent repair of a missing file. |
| 10 | `2ca2ec35` | Hook edits splice the top-level hooks value, retaining unrelated JSON bytes, numeric spelling, order and indentation. preserve_order retains hook-entry order. The round-trip test checks CRLF, tab indentation, numeric spelling and exact unrelated content restoration. |

## Shared-file and merge notes

- Engine-owned `ingest/mod.rs`: only the duplicate terminal sanitizer was replaced
  with a call to `surface::safe`; parser behavior was not edited.
- Engine-shared `main.rs`: only reindex dispatch uses `full`, calls vector reset
  and emits public refresh JSON. `ready()` was not edited. No changes were made to
  store, query, intake, boundary, freshness or tool-index files.
- Semantic-owned `semantic.rs`: shared log opening, named query-thread spawning,
  panic-cause propagation, reset re-export and a panic regression test.
- Semantic-owned `semantic_model.rs`: shared hash/stamp verification used by
  diagnostics/loading, and private directory creation during provisioning.
- Semantic-owned `semantic_store.rs`: the explicit vector-reset transaction with
  work/writer locking. Inference, vector scanning and model architecture were not
  changed.
- `archive.rs`: only Archive::open directory creation changed to private_dir for
  the archive root and blobs. Source opening was not edited; preserve these calls
  when integrating B's source-opening fix.
- `surface::is_terminal_format` is public so B's JSON escaping can use the same
  character set. Ingest's human renderer already uses this shared implementation.
- `Cargo.toml` enables serde_json preserve_order and the lockfile records its
  indexmap dependency. Keep that feature when merging JSON-output changes.
- CI keeps the semantic benchmark data via `--data` so semantic_eval can open the
  verified model afterward. The existing workload size remains for the semantic
  thread to replace under audit finding 2.

## Validation

Final source tree:

- `cargo fmt --all --check`: passed.
- `cargo clippy --workspace --all-targets -- -D warnings`: passed on macOS.
- Offline Linux x64 and Windows GNU clippy checks with the requested compilers,
  targets and shared cross-check target directory: passed. The SQLite stub was
  used only for Linux type-checking.
- `cargo test --workspace --no-fail-fast`: 217 passed, zero failed, zero ignored;
  doc tests passed. The CLI tests used a pinned binary and cleared, temporary
  home/data/model/caller paths. All Cargo work used nice and six build jobs.
- `python3 -m unittest discover -s bench -p 'test_*.py'`: 18 passed.
- `python3 bench/check_skips.py`, including the final Rust test log: passed.
- `python3 bench/validate_repo_privacy.py --worktree --index`: passed.
- `python3 bench/third_party_licenses.py --check` with Cargo on PATH: passed.
- Actual pinned model, CPU `semantic_eval`: passed; minimum cosine
  0.9999999999985653, identical token IDs, top-five agreement 10/10, recall@5 1.0,
  reference recall@1 1.0 and long-tail recall@1 1.0. Model load was 1.291 s;
  evaluation was 16.541 s. Only aggregate results are retained here.

The new performance and skip-policy tests were observed failing before their
fixes. The expanded corrupt-model test also failed on the prior explicit `-s`
behavior, then passed after the correction. No budgets were loosened and no
tests were ignored. Temporary model, binary, data and logs were removed after
validation; shared build caches were retained.

## Remaining work outside this assignment

- Audit 2 remains with the semantic thread: owner-scale hybrid workload and its
  performance result. This report makes no scale-performance acceptance claim.
- Audit 3 and 6 remain with the engine thread: discriminating 2M corpus, query
  shapes, census/pending freshness and background refresh integration.
- B retains the explorer, live readers, JSON escaping, release workflow,
  archive source opening and explorer sleep-test findings. None were edited here.
- Native Windows runtime tests and the CI matrix were not executed locally;
  Windows type-checking passed. The registry explicitly records the Windows
  interactive doctor-consent limitation.
- Release cleanup of historical slice/fixer reports and other scaffolding stays
  with the integrator. User-facing docs edited here describe product behavior.

There are no known unresolved findings within fixer A's assigned scope.
