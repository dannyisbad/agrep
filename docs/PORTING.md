# 0.5 command port status

Every documented command is wired. No command invokes Python, an older agrep, a daemon, or a protocol worker. Reference paths below belong to the read-only 0.4.9 checkout, not this product.

| Command | Status | Reference for the remaining slice |
|---|---|---|
| search / bare pattern | Partial until engine merge: keyword engine plus JSON/TSV/classic/compact presentation, caller exclusion, counts, dimension coverage, frozen SQLite continuation pages | `cmd/search.rs`; rendering/identity details below |
| index | Implemented: census, changed-source parsing, transactional row diff | `py/indexer.py`, Rust adapters |
| reindex | Implemented: one-transaction rebuild of derived rows | `reindex.py`; semantic rebuild follows semantic slice |
| set | Done: reference listing/confirmation text; validated tools, embeddings and post_index; preserves other config fields | `cmd/set.rs`; executing post-index is owned by __work integration |
| status / bare invocation | Done: read-only format/disk, source errors, counts/refresh, model/coverage, integration drift, archive state | `cmd/status.rs`; [browse contracts](BROWSE.md) |
| chats | Done: newest-first metadata, ranked content heads, side/default filters, per-row follow-up and JSON envelopes | `cmd/chats.rs`; [browse contracts](BROWSE.md) |
| around | Implemented | `cmd/around.rs`, `evidence.rs`: digest/event verification, bounded moved-content rescue, positional clamping, role/tool selection, source and render truncation |
| recall | Implemented | `cmd/recall.rs`: ranked windows, family selection, direct handles, probe, one JSON object, UTF-8 byte budgets |
| pack | Implemented | `cmd/pack.rs`: query round-robin, turn deduplication, merged overlapping windows, shared recall budget |
| postcompact | Partial (single-session refresh pending) | `cmd/postcompact.rs`, `recovery.rs`: explicit/exported caller, structural boundary, root packet capped at eight blocks and 8000/16000 bytes |
| summary pending/time | Done: project briefings, evidence-backed pending states, capped interval union and local grouping | `cmd/summary.rs`, `briefing.rs`; [browse contracts](BROWSE.md) |
| board | Implemented: direct live observer, family filters, JSON/once, interactive terminal ownership | `docs/LIVE_PORT.md` |
| tail | Implemented: JSONL events, complete-census readiness, scoped snapshots, emit-before-ack | `docs/LIVE_PORT.md` |
| resume | Done: references/aliases, picker/list, parent resolution, native cwd/argv | `cmd/resume.rs`, `launch.rs` |
| run | Done: native argv, process lifetime, signals/terminal restoration, no persistent run records | `cmd/run.rs`, `launch.rs` |
| setup | Done: consent and opt-outs, owned blocks/skills, native hooks, model provisioning, archive policy | `cmd/setup.rs`, `integrations`; differences below |
| remove | Done: config enrollment plus known marked targets; edited helper protection; index and histories kept | `cmd/remove.rs`, `integrations`; no old receipts or sentinel |
| archive | Done: registry discovery, SQLite backups, immutable gzip chunks/catalog, retention, consent, status | `cmd/archive.rs`, `archive`; native sources only |
| restore | Done: hash verification, portable safe paths, --to, --force, SQLite writer refusal | `cmd/restore.rs`, `archive` |
| doctor | Done: integrity, safe refresh, enrolled-item repair for drift, confirmed corrupt-index rebuild | `cmd/doctor.rs` |
| audit | Done: independent census/row recount, full source reparsing, strict gaps | `cmd/audit.rs` |
| why | Done: version-1 verdicts, ambiguity/absence, indexed source paths | `cmd/why.rs` |
| ui / serve | Implemented: embedded original page, authenticated loopback HTTP, shared search/history/live APIs | `docs/LIVE_PORT.md`; semantic branch awaits shared semantic entry point |
| --more | Implemented | `pages.rs`: frozen rendered rows, per-root opaque handles, five-minute TTL, no source or query replay |
| __work | Archive due-pass wired under work.lock; semantic/post-index integration remains | Call `archive::capture_if_due(&root)` from the combined __work dispatcher |

Caller identity resolves exported Claude/Codex/pi sessions and private, birth-verified pi/omp process publications. Current-window exclusions are pushed through temporary SQLite views before retrieval; `--no-self` excludes the family, and `--self` includes the current window. `self` marks the caller transcript only. A missing or conflicting identity is disclosed once in agent shells. Explicit semantic search reports the prescribed missing-model error. Automatic optional meaning search has no installed lane in this foundation. The int8 SIMD scanner remains in the core.

Source adapters retain upstream historical formats. This is independent of the removal of every old agrep-owned cache, manifest, publication witness, owner record and process protocol. Historical documentation retained for ranking/output/handles describes the target behavior; this table is authoritative about what can execute in this slice.

Native replay copies retain their source contributions in SQLite. Only `visible=1` rows are searchable, and the public turn is `position` (the source-local `turn` stays available for incremental reconciliation). Future around/summary/audit code must use that canonical view for public results; audit can independently recount it instead of trusting cached counts. Deleting a winning source restores another stored contribution without reparsing unchanged files.


## Integrations and archive port

`config.json` owns integration enrollment and archive on/off/keep policy. Setup records each
successfully installed block, hook entry, or helper file, including created directories. Remove
removes those objects, preserves edited helpers and unrelated JSON values, and scans the known
instruction targets for any agrep-marked block. The existing `integrations: Vec<String>` field
remains available to diagnostics; `enrollment`, `archive`, and flattened extra settings are new.

Instruction targets come from the embedded client registry. CODEX_HOME and XDG_CONFIG_HOME are
honored, and OMP_PROFILE/PI_PROFILE selects the OMP extension directory. Claude and Codex hooks
invoke the same executable's hidden `__hook` command; pi/OMP use the retained native TypeScript
extension. No Python runtime, sentinel, undo snapshots, historical digests, or owner checks ship.

Intentional 0.4.9 differences:

- Setup replaces every agrep-marked block, including an edited/current one, as SPEC §7 and the slice
  instructions require. Unmarked existing skills and edited helper files remain protected.
- Hook registration merges only the owned entry into existing hook arrays. Other hooks and JSON
  values survive; JSON whitespace may be reformatted with the original LF/CRLF convention.
- Consent and completion output omit removed dependency/daemon/owner/sentinel stages. All public
  switches and mutation exit codes remain. Invalid-setting errors use the common Rust CLI prefix.
- Archives use a fresh version-1 SQLite catalog and immutable 1 MiB SHA-256 gzip chunks. No 0.4.x
  manifest or blob is read. Appends reuse equal chunks, including validating the old partial tail;
  rewrites are captured as new versions. Reported new-content bytes are compressed bytes actually
  stored, rather than the old uncompressed-byte counter.
- An explicit capture snapshots a coherent SQLite view immediately through the backup API.
  Scheduled captures run at most once per ten minutes inside __work. Old settle delays,
  subprocess capture children, progress sentinels and deferred-state machinery are not retained.
- A forced restore into an existing SQLite file uses the backup API under SQLite's write lock,
  preserving live-reader/WAL coherence and refusing a concurrent writer. Plain-file restoration
  is byte-exact; SQLite recovery preserves database contents, not original WAL file bytes.

Integration seams: merge the archive __work arm with semantic work (one work.lock acquisition).
The call is `archive::capture_if_due(&DataRoot) -> Result<Option<Stats>>`. Setup's provisional
`model_provision::provision(&DataRoot) -> Result<()>` duplicates the verified provisioning-only
portion of the semantic slice's `semantic_model::provision`; replace the setup call and directory
reference with semantic_model when integrating, then remove model_provision.rs. The bridge is
fully functional, pins the same three sizes/hashes/revision, and introduces no model inference
implementation. `Adapter::archive_roots/archive_content` default to freshness roots/content;
Codex, Cline, Crush, Cursor, Gemini and opencode extend these for native recovery inputs. No query fields or
engine/store/intake implementation changed.

Validation and side-by-side evidence: [INTEGRATIONS-ARCHIVE.md](INTEGRATIONS-ARCHIVE.md).

## Distribution, CI, release and documentation

Implemented in the distribution slice: six binary-only platform wheels with a
minimal Python exec launcher; a first-run npm downloader with package-pinned
SHA-256 verification; six-target release assembly; native binary and installed
package smoke tests; portable builds with private paths remapped; dependency
notices; version, wheel, npm, manylinux, privacy and test-skip validators.
`ci.yml` runs fmt, strict clippy, workspace tests and blocking mixed, hybrid and
2M dense process benchmarks on Linux, macOS and Windows (including arm64 Linux
and Windows), plus checks every release target. `release.yml` is tag-only and
publishes via existing trusted-publishing identities after its gates.

The implementation is done; release acceptance still depends on the other
command/semantic slices and the engine meeting its budgets. The current
foundation cannot pass `around` or semantic installed-package smoke tests.
This is not bypassed. See [RELEASE_VALIDATION.md](RELEASE_VALIDATION.md) for
local evidence and remaining integration checks.

Intentional 0.4.9 differences: no sdist (source installs use Cargo); no runtime
Python modules/dependencies; npm downloads the standalone binary without a
Python bootstrap; no distribution/runtime/build identity in `--version`; no
legacy install recovery or process machinery. Existing output/ranking/handle
docs now describe the 0.5 interface and link to this implementation status.
The inherited `target-cpu=native` configuration was removed: both performance
checks and distributable binaries must use the portable target baseline.

## Browse and diagnostics port

Browse/diagnostics validation and intentional differences are in [BROWSE.md](BROWSE.md).
These commands add no fields to `query::Filters`, `Options`, `Hit`, or `Results` and
make no changes to intake, store, ranking, or boundary code. The live display can
Use the live process reader for running sessions. Run records were removed.
Diagnostics read semantic coverage directly and check typed integration enrollment.
`doctor --fix` repairs only drifted enrolled items in-process.

## Slice A integration surface

`main.rs` only dispatches the Slice A commands and contains their clap types. Search
`max`, recall `hits`, and around `context`/`max_chars` retain option presence; around positional
turns are signed. Shared `Scope` still constructs the foundation `Filters`.
`lib.rs` exports `caller`, `caller_process`, `coverage`, `evidence`, `handles`,
`hybrid`, `pages`, `recovery`, and `surface`. No store/query/intake/boundary module
was edited and no `Filters`, `Options`, `Hit`, or `Results` field was added.

The engine can replace the temporary-view bridge in `caller::search` with native
caller predicates. Inputs are `Caller::{agent, session, members, live_members,
boundary, resolved}`, `include_self`, and `no_self`; apply the exclusion before
ranking/counts, and mark only the exact caller transcript. `cmd/postcompact.rs`
currently calls `store::refresh(&root, false, false)` before opening its reader;
replace that call with the engine's single-session inline refresh. `Style` has a
`pending_sources` input; the foundation has no such count in `Refresh`, so its
current callers pass zero. Wire the engine's factual pending count there. The broader retrieval requested by
`Search::coverage` also needs the new planner hook; the current surface reports
filter-dimension coverage, but does not invent additional matches outside the
foundation query API.

`hybrid::merge(keyword, optional_semantic, limit, family_diverse, Lane)` is the
semantic seam. Semantic `Hit::score` is cosine, `matched` becomes `semantic`, weak
neighbors cannot lead a weak-only lexical page, mixed-lane totals are floors, and family/text/turn duplicates
are suppressed. Search, recall and pack now call in-process semantic retrieval. Missing-model
refusal applies only when the model is absent.

Intentional 0.4.9 differences: the engine name is `sqlite`; the obsolete publication
age/owner/generation fields are absent; fresh snapshots describe a committed census
without old daemon states. Internal query-work counters are not part of the public envelope. Continuations live in a small separate SQLite store and survive changes to source content
until their TTL, as required by the 0.5 frozen-page contract. Source reply truncation
is now known from retained reply metadata rather than reported as unknown. The
structured recall hit also carries its verified handle and `self` bit. No old cache,
page, model, owner, or runtime format is read.

Known engine integration limit: foundation `events.body` omits timestamps and turn
addresses for control events, and does not retain an input/output boundary inside
multiline tool inputs. Tool rows are decoded from canonical indexed text (the first
newline separates the normal input summary and output). To recover every structural
control marker and unusual multiline-input preview without source reads, retain
`ts`, assigned `turn`, and an input/output separator offset in event metadata. The
surface has no query-path source parser.

Validation and output comparison evidence: `measurements/surface/README.md`;
`tests/surface.rs` and `tests/golden/pi-*.txt`. `scripts/compare-surface.py` is a
development-only reference comparison, always using disposable homes/data roots.


Slice A final host validation: 60 tests passed with no skips, formatting passed,
and Clippy passed with warnings denied. On the foundation's synthetic 100k corpus,
release medians were 39.94 ms around and 72.99 ms recall. Around's headroom is only
0.06 ms; the retained first sample also documents a cold source-path re-identification
refresh. See the full samples, comparison matrix, and explicit caveats in
`measurements/surface/README.md`. Linux and Windows execution still belongs to CI.
