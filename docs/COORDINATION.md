# Coordination surface

Most cross-process ownership in agrep is arbitrated by a small record file
created with exclusive semantics (`O_CREAT|O_EXCL` on the Python side,
`create_new(true)` on the Rust side) whose exact bytes name the owner. The
table below also catalogs the non-arbitrating records that sit alongside them:
bounded caches, endpoint descriptors, freshness signals, and transient reap
tombstones. `py/ownerfile.py` is the single
Python primitive for creating, snapshotting, verifying, and exactly removing
these records; every Python module below goes through it. `IndexLock` in
`py/index_lock.py` shares its byte protocol and conformance fixture with the
Rust CLI's `IndexLock` (`crates/agrep-cli/src/index_lock.rs`, via
`create_new`). During every derived-store write, including ownerless adoption,
`crates/agrep-cli/src/main.rs` temporarily claims both daemon lock generations
to fence resident and legacy writers. The remaining Rust `create_new` sites are
`crates/agrep-core/src/ingest_cache.rs`, which stages unique-named temp files
for atomic publish, and `crates/agrep-core/src/ingest/mod.rs` plus
`crates/agrep-core/src/ingest/registry.rs`, which create private SQLite
snapshots. Those three staging/snapshot sites are not coordination.
`py/test_coordination_contract.py` enforces this map structurally.

Classes used below:

- **lifetime** - owned as long as the recorded process (or process group) is
  provably alive; a dead or pid-reused holder is reclaimed by exact
  compare-and-remove, never by age alone.
- **lease** - protection expires by time even if nobody reclaims it.
- **fence** - blocks an operation while present; removed only by its owner or
  an exact reap.
- **signal** - mtime or content is read for freshness; never arbitrated.
- **bounded cache** - a size-capped published snapshot, valid only while the
  owner record it names still verifies; readers fall back rather than mutate.
- **endpoint descriptor** - the address and credential a client needs to reach
  a running owner; its validity is bound to that owner's lifetime.
- **transient** - a short-lived tombstone written during an exact reap so a
  concurrent reader cannot mistake removal for absence.

All paths are relative to the agrep data dir (`AGREP_DATA_DIR`, else the
per-OS user data dir) unless noted. `v{P}` is the daemon protocol version
(`INDEXD_PROTOCOL`, currently 2).

Protocol-10 semantic ownership and request serialization are the exception.
They live in the private per-user runtime namespace
`<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/`, using `/tmp` on
POSIX and the OS temporary directory on Windows. The data-dir digest binds
the namespace to one corpus without granting sandboxed readers corpus-write
authority.

| Path | Owner module | Byte protocol | Reclaim policy | Class |
|---|---|---|---|---|
| `.index.lock` | `py/index_lock.py` `IndexLock`; `crates/agrep-cli/src/index_lock.rs` `IndexLock` | canonical ASCII line: `pid= start= token= label= time=`; legacy safety core is `pid` plus optional `start` | live or unverifiable holder never stolen at any age; dead, zombie, or pid-reused holder tombstoned and exactly reaped; malformed claims only inside a strict non-negative 3 s publication grace; oversized/non-regular claims fail closed | lifetime (ingest critical section) |
| `.indexd.v{P}.lock` | `py/indexd_runtime.py`; temporary derived-adoption claim in `crates/agrep-cli/src/main.rs` | daemon owner line: `pid= start= protocol= package= build= group= [tree=] token= time=`; adoption fence: `state=derived-adoption pid= start=unknown writer= token=` | exact-snapshot daemon-owner state machine; live compatible owner kept, dead/orphaned generation retired via its child fence; malformed record protected for a strict non-negative 3 s grace; the derived writer exactly removes only its own temporary claim; owner mtime is not freshness authority (`.ingest.sig` is) | lifetime (daemon generation owner / derived-writer fence) |
| `.indexd.v{P}.ready.<gen>` | `py/indexd_runtime.py` | byte-identical copy of the owner record | valid only while byte-equal to the live owner record; removed with its generation | lifetime (readiness bound to owner) |
| `.indexd.v{P}.live.<gen>` | `py/indexd_runtime.py` (publish/read policy); `py/indexd.py` (resident publisher) | private regular JSON, at most 60 KiB: protocol/build, exact owner-generation token and owner SHA-256, publication time, every session state, and a disclosed count when only old feed events were trimmed | atomically replaced only while the exact compatible ready owner verifies before and after; readers re-verify that owner generation, reject records older than 12 s, and never mutate on fallback; the owner exactly removes its last publication on normal exit; crash leftovers are generation-ineligible and ignored | bounded cache (lifetime-bound live snapshot) |
| `.indexd.v{P}.child.<gen>` | `py/lifetime.py` (guard writes); policy in `py/indexd_runtime.py` / `py/indexer.py` | one line: `owner= guard= guard_start= target= target_start= group=` | guard releases after draining its owned process group; the daemon retires fences whose guard group is provably gone | lifetime (index-build child fence) |
| `.indexd.v{P}.spawn` | `py/indexd_runtime.py` | one line: `state=launching pid= start= token=` (32-hex token, newline-terminated = complete) | exact-live or unverifiable launcher protected without an age limit; after launcher exit, its generation-scoped child handoff decides whether relaunch is safe; dead/reused holders are reclaimed exactly; incomplete records get a strict non-negative 3 s publication grace | lifetime (launch arbitration) |
| `.indexd.v{P}.spawn.<gen>.child` | `py/indexd_runtime.py` | one line: `state=spawned owner= pid= start=` | published while the launcher still owns `.spawn`; exact-live or unverifiable child protected at any mtime; dead/reused child exact-removed before the parent claim; malformed records get a strict non-negative 3 s publication grace | lifetime (launch-generation handoff) |
| `.indexd.lock` | `py/indexd_runtime.py` (legacy retire); temporary derived-adoption claim in `crates/agrep-cli/src/main.rs` | legacy v1 owner line (`pid= start= ...`) or temporary `state=derived-adoption pid= start=unknown writer= token=` fence | dead legacy pid with inactive group exact-removed; live provable legacy tree may be exactly terminated first; Windows orphan grace 3700 s; the derived writer exactly removes only its own temporary claim | lifetime (legacy daemon owner / derived-writer fence) |
| `.background-removal` | `py/removal_fence.py` | JSON: `pid, process_start, started_at, nonce` | exact-live/unverifiable holder protected; dead or pid-reused holder exact-removed immediately; malformed protected only inside a strict non-negative 30 s grace | lifetime (removal fence) |
| `.background-removal.cooldown` | `py/removal_fence.py` | JSON: `completed_at, expires_at, nonce` | protected only during the strict non-negative interval from `completed_at` through `expires_at` (<= 30 s), then exact-removed by any reader | lease (bounded ownerless cooldown) |
| `.<namespace>.publish.lock` (`embeddings.f32`, `embeddings.meta`, `embeddings.refs-build`) | `py/embedding_store.py` `EmbeddingPublishLock` | JSON: `pid, token, process_start, created_at` | dead/reused holder reclaimed exactly; malformed record protected only for a strict non-negative 120 s grace | lifetime (embedding publication namespace) |
| `.semantic-embed.lock` | `py/embed.py` (path from `py/semantic.py`) | JSON: `pid, process_start, token, started_at` | exact-live/unverifiable holder wins; dead/reused removed; malformed protected only for a strict non-negative 30 s grace | lifetime (one embedder per data dir) |
| `.semantic-compaction.lock` | `py/semantic_segment_compact.py` | JSON: `pid, token, process_start` | same strict non-negative malformed-record grace as the embed claim | lifetime (compaction claim) |
| `<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/worker.lock` | `py/semworker.py` | JSON: `pid, started_at, process_start, nonce, tree_bound, named_job`; private namespace and record permissions | exact-live or unverifiable owner protected; dead, reused, or malformed-stale owner discarded exactly; a current worker will not acquire until the protocol-9 migration fence is clear | lifetime (resident worker owner) |
| `<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/worker.request` | `py/semworker.py` | JSON: `pid, process_start, nonce` | exact-live or unverifiable caller protected; dead, reused, or malformed-stale caller discarded exactly; every caller stays inside its existing end-to-end deadline | lifetime (one accepted semantic request at a time) |
| `<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/retire-<owner-nonce>` | `py/semworker.py` | JSON: `pid, process_start, target_pid, target_start` | generation-named handoff is consumed and exactly removed by the matching worker; it cannot address a replacement generation | signal (generation-bound retirement request) |
| `.semantic-worker.lock` | `py/semworker.py` (protocol-9 migration only) | legacy JSON: `pid, started_at, process_start, nonce, tree_bound, named_job` | exact-live, unverifiable, or fresh-malformed legacy owner fences protocol-10 acquisition; dead, reused, or malformed-stale legacy state is discarded exactly | lifetime (one-upgrade migration fence) |
| `.semantic-worker.starting` | `py/semworker.py` | JSON start claim: `pid, process_start, at, nonce` | protected only while the claimant is exact-live AND within `START_CLAIM_GRACE_S`; else exact-removed | lease (live-holder start claim) |
| `.semantic-worker.json` | `py/semworker.py` | JSON endpoint: `pid, port, token` (64-hex), `process_start, owner_nonce` | publication and every reclaim verified against the worker lock generation; dead/reused or own-nonce records replaced | endpoint descriptor (lifetime-bound) |
| `<model dir>/.download.lock` (`AGREP_MODEL_DIR` or `<user data dir>/models/<profile>`) | `py/embedder.py` | JSON: `pid, process_start, at, token` | live holder wins; dead holder reclaimed; malformed/bodyless protected only inside a strict non-negative 2 s create grace | lifetime (one downloader per model dir) |
| `archive/lock` | `py/archive.py` | one line: `pid= start= token=` | non-blocking: exact-live/unverifiable holder skips the pass; dead holder reclaimed instantly; malformed claim gets a strict non-negative 3 s publication grace | lifetime (archive pass) |
| `.server` | `py/legacy_cleanup.py` `retire_removed_explorer` (retire-only; no in-tree writer) | JSON: `pid, port, process_start, mode` | dead owner compare-and-unlinked; exact-live legacy explorer terminated, then unlinked | endpoint descriptor (legacy) |
| `.agrep.search.v{P}` | `py/indexd_runtime.py` (touch), `py/indexd.py` (read) | content ignored; mtime only | none - stat-only demand beat for daemon idle-exit | signal (search demand) |
| `.ingest.sig` | Rust ingest writes under `.index.lock`; read by the Python freshness surfaces | generation signature text | rewritten (mtime refreshed) by every ingest run; readers independently disclose an unreadable, future-dated, or over-bound mtime even when the writable health ledger failed | signal (generation / freshness clock) |
| `.semantic-use.beat` | `py/semantic.py` | content ignored; mtime only | none - demand signal for background refs prewarming | signal (semantic demand) |
| `.<name>.owner-reap-<pid>-<hex>` | `py/ownerfile.py` | byte copy of the record being removed | transient inside `remove_exact`'s two-phase reap; restored on mismatch, else unlinked | transient (reap tombstone) |

Not coordination, deliberately excluded: uniquely-named staging temps that are
atomically renamed into place (`crates/agrep-core/src/ingest_cache.rs` staged
cache files), data manifests (`embeddings.meta`), and progress/state JSON
(`.semantic-embed-state.json`, `.semantic-embeddings-generation.json`).

## Ingest generation commit protocol

Two ownerless records form the publication boundary; they are commit markers,
not mutual-exclusion claims. Rust writes `.ingest_pending.bin` before changing
cache or output state. Its presence disables the all-hit shortcut and makes a
first-use reader wait or fail closed. It is retained only when the pass could
not publish a coherent generation: a run whose reads all succeeded publishes
its preflight view even if sources kept moving afterwards, a byte-stable
snapshot whose only defects are durable walk-recorded facts (denied roots,
symlinks, unsupported files, pre-epoch mtimes) publishes with those issues
serialized and disclosed on source health, and an emit-rows handoff whose
published-bytes residue is proven current is retired by the next run's all-hit
shortcut. `.source_snapshot.bin` is the validated source generation
and is committed last, after messages, replies, sessions, events, cache, and
derived proofs. A first-use reader accepts the generation only when the pending
marker is absent and the nonempty regular snapshot's identity and metadata stay
stable across the coupled derived-publication health check.

On macOS, a full process birth query may be denied across uid boundaries.
The owner observer then uses `PROC_PIDTBSD_SHORTINFO` to identify the other uid,
which makes a same-pid record from this user's private data dir a reused owner.

Index-lock acquisition remains bounded by its configured timeout. After one
second, both implementations name the observed pid/label and remaining bound
on stderr, repeating the notice every 30 seconds while the holder remains.

A caller's automatic semantic deadline is one end-to-end budget across worker
start, queue wait, connection, and execution. A queued caller cannot retire the
worker serving an earlier request; only an accepted request can synchronously
retire the exact worker generation that exceeded its deadline.

Resumed agents and foreground post-index hooks relay interruption to their
owned process trees and return the canonical `128 + signal` status. Daemon
shutdown cancels worker-thread hooks and drains their owned trees before exit.
