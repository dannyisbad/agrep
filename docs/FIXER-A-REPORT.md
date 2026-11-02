# Fixer A integration report

The semantic branch is merged. The assigned command fixes are committed. Release
acceptance is still blocked by the explicitly deferred engine work and fixer B's
owned explorer/privacy work; this report does not claim a green final release tree.

## Commits

- `c11629ae` — merge semantic retrieval through the surface renderer, shared fusion,
  setup provisioning and ordered background tasks.
- `e645ed20` — reclaim unreferenced vector blocks after ten minutes; retain referenced
  and recent files. Includes the reclamation regression.
- `a764a4f3` — isolated command regressions, explicit test model paths, and a real
  Windows child fixture. `AGREP_TEST_BINARY` lets shared-target validation pin the
  executable produced by this checkout; absent executables fail.
- `7672bc37` — typed enrollment, in-process doctor repair, preserved archive choices,
  safe dotfile links, explicit archive/restore waiting, empty capture rejection,
  model provisioning, help and config fixes.
- `d3cc56cb` — shared caller/session resolution, semantic caller scope before limiting,
  bounded evidence and recall budgets, independent continuation storage, public
  output cleanup and shared freshness wording.
- `aa5a6f16` — canonical briefing events, native process lifecycle without run records,
  audit digest keys/count queries, readable diagnostics and removal of the duplicate
  browse benchmark.
- `984e78a0` — normal-exit and stopped-child process tests, Windows resume fixture,
  synthetic-user literals and regular placeholder-file capture.
- `6a7d50ef` — portable corrupt-index publication regression and missing-index repair.

All commits carry the required co-author trailer. No branch was pushed or rebased.

## surface.md

| Finding | Disposition / commit |
|---|---|
| 1 | Deferred to engine merge: stat-only query census and detached refresh. |
| 2 | Fixed: `c11629ae`, `d3cc56cb`; search/recall/pack use semantic retrieval and `hybrid::merge`. |
| 3 | Deferred to engine merge: blocking caller-session refresh before postcompact. |
| 4, 5 | Fixed: `d3cc56cb`; zero-limit pages advance; follow-up targets use the complete session index. |
| 6 | Deferred to engine merge: actual checked/pending-source result from census. |
| 7 | Deferred to engine merge: replace the temporary-view scope bridge with native query predicates. |
| 8, 9 | Fixed: `d3cc56cb`; disclosures respect explicit inclusion and JSON, metadata retains scope/session, budgets preserve hit turns before discarding hits. |
| 10 | Deferred to engine merge: coverage recovery planner hook. |
| 11 | Fixed: `d3cc56cb`, `a764a4f3`; no public work counters; engine-work assertions call the query API. |
| 12 | Fixed for owned harnesses: `a764a4f3`, `d3cc56cb`; caller publication directories are isolated. Common bench harness isolation belongs to B. |
| 13, 14 | Fixed: `a764a4f3`, `d3cc56cb`; snapshot test commits the concurrent change; pages use their own bounded SQLite store and skip writes in read-only mode. |
| 15 | Fixed: `7672bc37`, `d3cc56cb`; both reader and pi extension use a 0.5-specific publication directory. |
| 16, 17 | Fixed: `d3cc56cb`; one effective include-self decision; zero-hit totals use stored counters. |
| 18 | Fixed for explicit index/setup and semantic initialization: `7672bc37`, `e645ed20`; covering model index. Engine first-build integration must also create it. |
| 19, 20 | Fixed: `d3cc56cb`; one metadata-based resolver; shared boundary spans and bounded fancy-regex matching, bounded tool previews. |
| 21 | Fixed in owned surfaces: `d3cc56cb`; removed concept/marker placeholders and renamed unbound handles. B must remove explorer's concept placeholder. |
| 22, 23 | Fixed: `d3cc56cb`; no warning for an unindexed exported caller; shared read-only-aware checked calculation. |
| 24 | Fixed in owned code: `d3cc56cb`; deleted the duplicate surface benchmark. Common CI budgets and resolved-caller benchmark are B/engine integration work. |
| 25, 26 | Fixed: `d3cc56cb`; continuation argv/command and broader-rerun label; shared Windows-aware project leaf. |
| 27 | B-owned measurement/privacy cleanup; no measurement files edited here. |
| 28 | Fixed documentation: `aa5a6f16`; outstanding engine integration is marked partial. |
| 29 | Empty title deleted in `d3cc56cb`. Rejected deletion of `render::safe`: it is a used public adapter selecting `surface::safe(text, false)`, including for B-owned callers. |
| 30 | Fixed: `7672bc37`; compact profile uses `caller::ENV_KEYS`. |
| 31 | Fixed: `d3cc56cb`; chats/summary/around use the same caller implementation. |

## browse-diag.md

| Finding | Disposition / commit |
|---|---|
| 1 | Fixed for tool/subagent events: `aa5a6f16`; briefing consumes `evidence::load_events`. Control-event time/turn remains an explicit engine handoff. Native transcript regression replaces the fabricated event shape. |
| 2, 3, 4, 5 | Fixed: `7672bc37`; typed enrolled-item checks, in-process scoped repair, index-only corruption classification, incremental healthy refresh. |
| 6 | Fixed: `aa5a6f16`; removed run records, heartbeat, data-root creation and read-only refusal. The reference/live implementation has no reader requiring this new format. |
| 7 | Fixed: `aa5a6f16`, `984e78a0`; wait with stop reporting, suspend/resume propagation, foreground-only terminal handoff, pipeline group preservation. |
| 8 | Fixed: `d3cc56cb`; removed separate history self-policy. |
| 9 | Shared wording fixed in `d3cc56cb`; checked/pending facts remain deferred to engine merge. |
| 10 | Fixed: `aa5a6f16`; removed the duplicate browse benchmark and its acceptance claim. |
| 11, 12 | Fixed: `aa5a6f16`, `984e78a0`; Windows suffix selection; TERM-resistant descendant plus normal-exit and stopped-child regressions. |
| 13 | Windows argv/resume uses an actual compiled helper (`a764a4f3`, `984e78a0`); rebuild publication has a portable test (`6a7d50ef`). B owns the central platform/skip policy update. |
| 14, 15, 16 | Fixed: `d3cc56cb`, `7672bc37`; removed obsolete fields, use semantic coverage/model APIs, boolean archive enabled. |
| 17, 18 | Fixed: `7672bc37`, `6a7d50ef`; retain corrupt WAL/SHM, copy harness rules, clean stale repair directories and build missing indexes. |
| 19, 20 | Fixed: `7672bc37`, `d3cc56cb`; first-build wording and skipped-midnight handling. |
| 21 | Fixed: `aa5a6f16`, `984e78a0`; retain unreaped leader identity through descendant cleanup and allow a TERM grace period. |
| 22 | Memory/count work fixed in `aa5a6f16`. Tool-text extraction remains an engine handoff because its other implementation is inline in protected store.rs. |
| 23, 24 | Fixed: `7672bc37`, `aa5a6f16`, `a764a4f3`; readable human diagnostics, no inert Windows helper test. |
| 25, 26 | Fixed: `d3cc56cb`, `7672bc37`; one metadata resolver and DataRoot validation. |

## setup-archive.md

| Finding | Disposition / commit |
|---|---|
| 1 | Fixed: `a764a4f3`, `984e78a0`; targets/model provisioning accept explicit fixture paths; synthetic users only. |
| 2, 3 | Fixed: `7672bc37`; no Config.extra or redundant integrations list; typed board preference survives settings/setup. |
| 4 | Semantic, setup, post-index and task-list integration fixed in `c11629ae`, `7672bc37`, `e645ed20`. Census spawning/source refresh is deferred to engine merge. |
| 5 | Fixed: `7672bc37`; explicit archive operations and SQLite restore wait; scheduled capture yields silently. |
| 6, 7, 8 | Fixed: `7672bc37`; removed unused installed Claude contract, reject zero-byte versions, preserve archive opt-out/default No. |
| 9, 10 | Fixed: `7672bc37`, `984e78a0`; dotfile links and relative/symlinked data paths work; restore stays strict; capture uses regular-file traversal. |
| 11 | Fixed: `7672bc37`; production opencode teaching uses the adapter's store-root resolver, not an invented environment variable. |
| 12, 13 | Fixed: `7672bc37`; directory cleanup errors do not abort enrollment persistence; settings read after locking. |
| 14, 15, 16 | Fixed: `7672bc37`; source instruction text, trigger-rich Cursor description, reduced client registry. |
| 17 | Fixed: `c11629ae`; deleted competing model provisioner and unused setup seam. |
| 18, 19 | Fixed: `7672bc37`; root-relative archive filters, pruned traversal, reuse existing blobs and matching full chunks. |
| 20 | Process narrative removed in `7672bc37`; log/measurement deletion is B-owned. |
| 21, 22 | Fixed: `7672bc37`; command help/examples and no stale pi documentation reference. |

## Required integration work

- Keep `cmd/work.rs` as the sole ordered background entry point. Add refresh first,
  call `semantic::mark_index_changed` when rows changed, then leave embedding,
  reclamation, archive and post-index processing in order. Census scheduling must
  include due archive and hook work, not only embedding backlog.
- Native query caller predicates must be used for semantic resolution too. Until
  that merge, `caller::scoped` applies the existing bridge before either lane's
  result limit. `filter_results` supplies shared semantic citation/self metadata.
- Add `prose_model` during engine first-build/schema creation. Explicit index/setup
  already do so; avoid losing it on engine rebuilds.
- Store control-event timestamps/turns as planned. Tool/subagent events already
  join their owning rows. Extract the inline store tool-text formatting into a
  shared function and make audit consume it; store.rs was deliberately untouched.
- B's explorer must call history/evidence rather than its fork. `evidence::Display`
  now carries Caller; Window carries a collision-safe target. Use those fields.
- B must retain the typed Config/enrollment API, new 0.5 caller publication namespace,
  and removal of `model_provision` and run-record APIs.
- B's platform policy must reflect the removed inert Windows helper and portable
  rebuild test. Native Windows execution was not available on this host.
- B's privacy scrub must include newly merged semantic measurements. The final
  privacy check here has 50 findings across 25 B-owned logs/measurement files;
  no owned source/test file remains on that list.

## Validation

Every cargo invocation used nice 10, four build jobs, and the required shared target.
Tests use temporary homes/data roots and explicit model paths. A temporary copy of
this checkout's built CLI prevents another worktree from replacing the executable
mid-suite; it is deleted after validation. Early unpinned shared-target attempts
were invalidated by exactly that collision, and their failures were retained.

- `cargo fmt --check`: passed.
- `cargo clippy --workspace --all-targets -- -D warnings`: passed.
- Last full workspace attempt against the pinned CLI, before adding the final two
  portable regressions: 185 passed, one failed, zero ignored.
  The failure is B-owned `explorer::socket_tests::accepted_nonblocking_socket_waits_for_request`
  at its nonblocking socket read (`WouldBlock`). It also failed in an earlier attempt;
  it has not been rerun unchanged to manufacture a pass.
- Owned command suites: browse, foundation, integrations/archive, semantic and
  surface passed; the regression suite passed. Updated lifecycle testing also
  passed for normal exit and suspend/resume. The final focused run passed 34 tests: two CLI units, 22 integrations/archive
  tests and ten review regressions, including portable rebuild and missing-index repair.
- Semantic labeled q8/F16 quality tests passed. Actual model throughput acceptance
  remains open as recorded by the semantic branch; no budget was changed.
- `python3 bench/validate_repo_privacy.py --worktree --index`: failed only on the
  25 B-owned files above. This is an outstanding final-tree gate, not a waiver.

No engine files, B-owned runtime/UI files, CI/perf budgets or measurement files
were modified. The whole workspace and privacy gates must run after the engine
and B merges. No release acceptance is claimed by this branch report.

Safety note: the initial history-tool lookup inherited the host environment instead
of fixture overrides and returned existing-index metadata. No returned history was
written into the repository. Because that older executable may refresh on a read,
that one invocation cannot be certified as read-only. Subsequent runtime checks
used isolated homes/data directories. This exception is recorded rather than hidden.
