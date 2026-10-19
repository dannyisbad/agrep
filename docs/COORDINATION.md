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

Removal is where exact ownership could go wrong: a remover judges a record dead
(pid liveness, age) and then removes it, and a live owner could take the slot
between those two steps. Every reap in a directory therefore holds the
directory's `.owner-mutex` kernel lock while it re-reads the slot and removes
the record, so relative to every other remover the check and the removal are
one step, and a record a remover removes is always the record it judged.
Under the lock the record is renamed to a tomb and the tomb unlinked, so the
slot is free the moment the rename lands: Windows keeps a record a reader
still holds open delete-pending, and an exclusive create against that name
fails with `ACCESS_DENIED`, which both acquirers treat as an occupied slot.
Creators stay lock-free: `O_EXCL` only ever creates into an empty slot, so a
creator can neither replace a record nor be replaced by one. The sidecar is
minted by the first reap in a directory and removed only with the directory
(a lone sidecar counts as empty), so it exists only where agrep has reaped a
foreign record; a process removing a record of its own (a release, the
cleanup of a failed exclusive create, which may be a teach target in a user's
directory) honours a sidecar a reap minted and never mints one, so a directory
that only ever sees its own records come and go stays clean. A release that
cannot finish (the mutex busy past every attempt, a removal refused by the
store) leaves the record naming its live owner, which no other process may
reclaim. Every holder a long-lived process re-acquires goes through
`ownerfile.create_held` / `ownerfile.stranded_own_record`: its token is
registered in the process before the record is created (so a sibling thread
never mistakes a record created but not yet returned for a stranded one) and
forgotten by the release whatever its outcome, and the owning process reclaims
a record naming its own pid and verifiable process-start identity under a token
it no longer holds on its next acquire. That is `.index.lock` (`IndexLock`),
`.<namespace>.publish.lock` (`EmbeddingPublishLock`), `archive/lock`,
`.settings.lock`, `<model dir>/.download.lock` and the semantic
`worker.request` slot. `.indexd.v{P}.spawn` keeps its own retained snapshot
(`_OWN_SPAWN_GUARD`) and clears it exactly. The remaining owner records are
either created once per process life (daemon, worker and removal-fence owners,
the embed and compaction claims of a one-shot child) or age out by their own
lease (start claim, cooldown), so a stranded one ends with its process or its
grace. What the mutex cannot cover: a remover from a build before it (0.4.2
and earlier) that moves a live record aside with the old rename-aside reap,
during the one upgrade in which both builds run against one data dir; and, in
a directory no reap has yet touched, the first age-based reap of a live
owner's record racing that owner's lock-free release (the tomb proof then
links a gap claimant back into the vacant slot). A holder notices either at
its next `verify()` (the daemon's heartbeat, the ingest's pre-commit checks)
and stops; the window in which it and a gap claimant both believe they hold the
slot is bounded by that cadence. Liveness misjudgement (a pid the OS reports
dead that is not) and a store that cannot lock at all keep the pre-mutex
exposure.

A verified foreign-family takeover atomically publishes the successor's durable
owner before source reparsing. The daemon retains its lifetime ownership while
an incompatible cache is reconstructed. This handoff does not certify source
freshness; source and event publication retain their normal postflight checks.

A foreground explicit index (`agrep index`, `agrep index --full`, the index
`agrep setup` builds, `agrep reindex`) waits up to three minutes for a
same-writer `derived-adoption` claim whose PID and process-start identity are
still exact and live; background freshens never wait. A companion daemon
record must match the caller's authorized generation; legacy, foreign,
malformed, or unverifiable owners do not qualify. Each record the fence's
directory scan lists is read once; one that vanished in between (its holder
released or reaped it) is dropped from the verdict rather than read as a
legacy or absent owner. Two same-writer claimers that each won one of the
two claim records are waited out and retried under the same budget
with jittered pacing; only a steady refusal is final. A claim is settled only
when both records are the claimant's, or the second is the authorized daemon
generation's: a record another claimer held at publish time and released
before the fence passed is published again, so no later claimer can slip into
the vacant slot during the holder's locked section. A local failure to publish
the claim (an unwritable data dir, no space) is the writer's own error, not an
ownership verdict: a foreground explicit index reports it and exits 1 at once,
a background pass discloses it and keeps its read-only exit 0. The Rust child
reports one wait line and retries exclusive claim acquisition without holding
either writer lock. The claim is named as the freshness daemon's only when the
daemon's child fence (`.indexd.v2.child.<gen>`) names that exact process;
any other same-build claim is reported as another agrep indexer, with no
promise that it finishes on its own. Expiry returns exit 1 and names the
incumbent. Read-only Rust callers retain exit 0; no waiting caller borrows
the incumbent's claim or bypasses the writer fence. After an explicit ingest,
one passing ownership probe is the verdict; `agrep index --full` decides
before any settlement wait whether a rerun repeats the cold reparse.

When that invocation discards an incompatible, foreign-owned parse cache, it
reconstructs the cache in the same ingest. The resulting missing cache does not
require a second invocation merely to repeat a source snapshot. Reconstruction
still uses the repair guards: takeover does not prove deletion, and missing or
unreadable source material cannot replace the last published generation.

Supported legacy cache generations are validated and atomically re-owned with
their fallback rows and required-reparse flags intact. An undecodable unowned
cache is retained and takeover is refused before the database or owner anchor
changes; absence of a cache owner is not proof that its bytes may be discarded.

If both the anchor and parse cache are absent but `corpus.db` explicitly names
the current writer, Python permits only the canonical Rust-ingest launch to
restore ownership. Daemon and semantic writers still refuse that state until
Rust publishes the anchor. A foreign, malformed, or unreadable database owner
does not qualify for this same-writer recovery.

An anchor whose integer `version` exceeds this build's was written by a newer
agrep: every writer in Python and Rust refuses exactly as it does for a
malformed record, but names the upgrade ("written by a newer agrep (ownership
record version N)") instead of calling the record malformed. Records that do
not parse, or carry this build's version with other defects, stay malformed.
Because this is the one fence the reader can end (upgrade, or point
`AGREP_DATA_DIR` elsewhere), every surface names it from one bounded read of
the anchor (`indexd_runtime.newer_anchor`): `doctor` (the freshness row and
`core.derived_owner` in `--json`), the status page and `status --json`, the
search hedge ("the freshness owner is blocked: ..."), and `setup`, which
changes nothing there: no agent block, hook, launcher or sentinel is written
or rewritten (the newer build enrolled them), no daemon is started, no
archive capture is enabled; it exits 0 when a corpus is searchable and 3
otherwise (docs/OUTPUT_CONTRACTS.md).

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
- **kernel mutex** - an advisory lock on an empty sidecar the kernel releases
  with its holder's descriptors; serializes the mutations named for it.
- **transient** - the tombstone a removal renames a record to before unlinking
  it, so the slot is free at once and the removal stays a compare-and-remove
  even where the kernel mutex is unsupported.

All paths are relative to the agrep data dir (`AGREP_DATA_DIR`, else the
per-OS user data dir) unless noted. `v{P}` is the daemon protocol version
(`INDEXD_PROTOCOL`, currently 2).

Protocol-10 semantic ownership and request serialization are the exception.
They live in the private per-user runtime namespace
`<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/`, using `/tmp` on
POSIX and the OS temporary directory on Windows. The data-dir digest binds
the namespace to one corpus without granting sandboxed readers corpus-write
authority. A namespace that holds no record and has not been touched for an
hour is removed by the next worker to start (`semworker.reap_stale_coordination_dirs`);
sandboxed data dirs mint one each and nothing else deletes it. The caller publication below shares that runtime root but not the
data-dir digest: the pi/omp extension that writes it knows nothing about
agrep's data dir, and a tool shell must find it from process ancestry alone
(`AGREP_CALLER_PUBLICATION_DIR` overrides the directory on both sides).

| Path | Owner module | Byte protocol | Reclaim policy | Class |
|---|---|---|---|---|
| `.index.lock` | `py/index_lock.py` `IndexLock`; `crates/agrep-cli/src/index_lock.rs` `IndexLock` | canonical ASCII line: `pid= start= token= label= time=`; legacy safety core is `pid` plus optional `start` | live or unverifiable holder never stolen at any age; dead, zombie, or pid-reused holder exactly reaped under the directory's removal mutex; malformed claims only inside a strict non-negative 3 s publication grace; oversized/non-regular claims fail closed; a holder re-verifies its record (`verify()`) before each derived commit, and a record found replaced or gone there refuses the commit; at release the same finding is disclosed on stderr and flagged on the lock (`displaced` / `displacement()`), never an error for work that was committed under verification and never a reason to touch whatever now holds the slot; a record a release could not remove is an error, stays with its live owner's identity, and only that process reclaims it (its own pid, process-start identity and a token no live `IndexLock` of its holds) on its next acquire; an exclusive create refused with Windows `ACCESS_DENIED` while the entry exists or is delete-pending waits like any contention | lifetime (ingest critical section) |
| `.indexd.v{P}.lock` | `py/indexd_runtime.py`; temporary derived-adoption claim in `crates/agrep-cli/src/main.rs` | daemon owner line: `pid= start= protocol= package= build= group= [tree=] token= time=`; adoption fence: `state=derived-adoption pid= start=unknown writer= token=`, staged as `<name>.tmp.<pid>.<nanos>` and hard-linked into place so it is never seen partial | exact-snapshot daemon-owner state machine; live compatible owner kept, dead/orphaned generation retired via its child fence; malformed record protected for a strict non-negative 3 s grace, after which the derived writer too exact-removes an incomplete record naming no live exact process; the derived writer exactly removes only its own temporary claim; owner mtime is not freshness authority (`.ingest.sig` is) | lifetime (daemon generation owner / derived-writer fence) |
| `.indexd.v{P}.ready.<gen>` | `py/indexd_runtime.py` | byte-identical copy of the owner record | valid only while byte-equal to the live owner record; removed with its generation | lifetime (readiness bound to owner) |
| `.indexd.v{P}.live.<gen>` | `py/indexd_runtime.py` (publish/read policy); `py/indexd.py` (resident publisher) | private regular JSON, at most 60 KiB: protocol/build, exact owner-generation token and owner SHA-256, publication time, every session state, and a disclosed count when only old feed events were trimmed | atomically replaced only while the exact compatible ready owner verifies before and after; readers re-verify that owner generation, reject records older than 12 s, and never mutate on fallback; the owner exactly removes its last publication on normal exit; crash leftovers are generation-ineligible and ignored | bounded cache (lifetime-bound live snapshot) |
| `.indexd.v{P}.child.<gen>` | `py/lifetime.py` (guard writes); policy in `py/indexd_runtime.py` / `py/indexer.py` | one line: `owner= guard= guard_start= target= target_start= group=` | guard releases after draining its owned process group; the daemon retires fences whose guard group is provably gone | lifetime (index-build child fence) |
| `.recovery_requests/<nonce>.json` and `<nonce>.done` | `py/indexd_runtime.py`; source/FTS execution in `py/indexer.py` | bounded JSON: `version, token, pid`; 32-hex nonce names each request and its completion receipt | daemon captures a batch before ingest and consumes exact snapshots; later arrivals require another pass; callers cancel pending requests and remove receipts on exit; cancellation during ingest removes the late receipt; daemon reaps receipts whose caller PID is dead | queued work and completion receipt (not a freshness lease) |
| `.indexd.v{P}.spawn` | `py/indexd_runtime.py` | one line: `state=launching pid= start= token=` (32-hex token, newline-terminated = complete) | exact-live or unverifiable launcher protected without an age limit; after launcher exit, its generation-scoped child handoff decides whether relaunch is safe; dead/reused holders are reclaimed exactly; incomplete records get a strict non-negative 3 s publication grace | lifetime (launch arbitration) |
| `.indexd.v{P}.spawn.<gen>.child` | `py/indexd_runtime.py` | one line: `state=spawned owner= pid= start=` | published while the launcher still owns `.spawn`; exact-live or unverifiable child protected at any mtime; dead/reused child exact-removed before the parent claim; malformed records get a strict non-negative 3 s publication grace | lifetime (launch-generation handoff) |
| `.indexd.lock` | `py/indexd_runtime.py` (legacy retire); temporary derived-adoption claim in `crates/agrep-cli/src/main.rs` | legacy v1 owner line (`pid= start= ...`) or temporary `state=derived-adoption pid= start=unknown writer= token=` fence, staged and linked like the v{P} claim | dead legacy pid with inactive group exact-removed; live provable legacy tree may be exactly terminated first; Windows orphan grace 3700 s; a record with no valid pid exact-removed by either runtime after a strict non-negative 3 s grace; the derived writer exactly removes only its own temporary claim | lifetime (legacy daemon owner / derived-writer fence) |
| `.background-removal` | `py/removal_fence.py` | JSON: `pid, process_start, started_at, nonce` | exact-live/unverifiable holder protected; dead or pid-reused holder exact-removed immediately; malformed protected only inside a strict non-negative 30 s grace | lifetime (removal fence) |
| `.background-removal.cooldown` | `py/removal_fence.py` | JSON: `completed_at, expires_at, nonce` | protected only during the strict non-negative interval from `completed_at` through `expires_at` (<= 30 s), then exact-removed by any reader | lease (bounded ownerless cooldown) |
| `.<namespace>.publish.lock` (`embeddings.f32`, `embeddings.meta`, `embeddings.refs-build`) | `py/embedding_store.py` `EmbeddingPublishLock` | JSON: `pid, token, process_start, created_at` | dead/reused holder reclaimed exactly; malformed record protected only for a strict non-negative 120 s grace | lifetime (embedding publication namespace) |
| `.semantic-embed.lock` | `py/embed.py` (path from `py/semantic.py`) | JSON: `pid, process_start, token, started_at` | exact-live/unverifiable holder wins; dead/reused removed; malformed protected only for a strict non-negative 30 s grace | lifetime (one embedder per data dir) |
| `.semantic-compaction.lock` | `py/semantic_segment_compact.py` | JSON: `pid, token, process_start` | same strict non-negative malformed-record grace as the embed claim | lifetime (compaction claim) |
| `<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/worker.lock` | `py/semworker.py` | JSON: `pid, started_at, process_start, nonce, tree_bound, named_job`; private namespace and record permissions | exact-live or unverifiable owner protected; dead, reused, or malformed-stale owner discarded exactly; a current worker will not acquire until the protocol-9 migration fence is clear | lifetime (resident worker owner) |
| `<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/worker.request` | `py/semworker.py` | JSON: `pid, process_start, nonce` | exact-live or unverifiable caller protected; dead, reused, or malformed-stale caller discarded exactly; every caller stays inside its existing end-to-end deadline | lifetime (one accepted semantic request at a time) |
| `<runtime-temp>/agrep-semantic-v1-<uid>-<data-digest>/retire-<owner-nonce>` | `py/semworker.py` | JSON: `pid, process_start, target_pid, target_start` | generation-named handoff is consumed and exactly removed by the matching worker; it cannot address a replacement generation | signal (generation-bound retirement request) |
| `<runtime-temp>/agrep-caller-v1-<uid>/<pid>.json` | writer `py/hooks/pi_postcompact.ts` (installed pi/omp extension); reader `py/session_context.py` `read_caller_publication` / `published_caller` | private JSON: `version, pid, sessions[], cwd, updated` (ms) and, on Linux, `start` (`proc_<start ticks>`, the same birth identity `py/hookless/proc.py` derives) | reader walks its own `getppid()` chain (bounded depth) for the first pid with a record; a record is refused when `pid` disagrees, `start` mismatches the live birth identity, or (without `start`) `updated` predates the live process birth; `sessions` is the set of sessions the process hosts, replaced atomically on every start/switch/compact and unlinked when the last session leaves | endpoint descriptor (identity, lifetime-bound to the publishing process) |
| `.semantic-worker.lock` | `py/semworker.py` (protocol-9 migration only) | legacy JSON: `pid, started_at, process_start, nonce, tree_bound, named_job` | exact-live, unverifiable, or fresh-malformed legacy owner fences protocol-10 acquisition; dead, reused, or malformed-stale legacy state is discarded exactly | lifetime (one-upgrade migration fence) |
| `.semantic-worker.starting` | `py/semworker.py` | JSON start claim: `pid, process_start, at, nonce` | protected only while the claimant is exact-live AND within `START_CLAIM_GRACE_S`; else exact-removed | lease (live-holder start claim) |
| `.semantic-worker.launch-<nonce>` | `py/semworker.py` | JSON: child `pid, process_start, nonce`; empty birth identity records an unverifiable startup | retained until registered ownership or child exit; teardown drains exact unregistered children, waits without signalling unverifiable live children, then stops registered owners; malformed regular records use `START_CLAIM_GRACE_S` before exact removal; oversized/non-regular records fail closed | launch handoff (covers log handles before worker registration) |
| `.semantic-worker.json` | `py/semworker.py` | JSON endpoint: `pid, port, token` (64-hex), `process_start, owner_nonce` | publication and every reclaim verified against the worker lock generation; dead/reused or own-nonce records replaced | endpoint descriptor (lifetime-bound) |
| `<model dir>/.download.lock` (`AGREP_MODEL_DIR` or `<user data dir>/models/<profile>`) | `py/embedder.py` | JSON: `pid, process_start, at, token` | live holder wins; dead holder reclaimed; malformed/bodyless protected only inside a strict non-negative 2 s create grace | lifetime (one downloader per model dir) |
| `archive/lock` | `py/archive.py` | one line: `pid= start= token= daemon=<pid>:<start>` | non-blocking: exact-live/unverifiable holder skips the pass; dead holder reclaimed instantly; malformed claim gets a strict non-negative 3 s publication grace | lifetime (archive pass) |
| `.settings.lock` | `py/settings.py` `_settings_lock` | one line: `pid= start= token=` | serializes the read-modify-write of the settings file only, never an index run: exact-live/unverifiable holder waited for at most 5 s, then `SettingsError`; a holder reclaims its own stranded record through `create_held` / `stranded_own_record`; dead or pid-reused holder exact-reaped; malformed claim protected only inside a strict non-negative 3 s publication grace; released with a tombstone | lifetime (one settings writer) |
| `.server` | `py/legacy_cleanup.py` `retire_removed_explorer` (retire-only; no in-tree writer) | JSON: `pid, port, process_start, mode` | dead owner compare-and-unlinked; exact-live legacy explorer terminated, then unlinked | endpoint descriptor (legacy) |
| `.agrep.search.v{P}` | `py/indexd_runtime.py` (touch), `py/indexd.py` (read) | content ignored; mtime only | none - stat-only demand beat for daemon idle-exit | signal (search demand) |
| `.ingest.sig` | Rust ingest writes under `.index.lock`; read by the Python freshness surfaces | generation signature text | rewritten (mtime refreshed) by every ingest run; readers independently disclose an unreadable, future-dated, or over-bound mtime even when the writable health ledger failed | signal (generation / freshness clock) |
| `.semantic-use.beat` | `py/semantic.py` | content ignored; mtime only | none - demand signal for background refs prewarming | signal (semantic demand) |
| `.owner-mutex` | `py/ownerfile.py` `_SlotMutex`; `crates/agrep-cli/src/index_lock.rs` `SlotMutex` | empty file, one per directory agrep has reaped a record in; locked with `flock` (POSIX) / `LockFileEx` and `msvcrt.locking` (Windows), never read | minted by the first reap in its directory and removed only with the directory (`ownerfile.remove_record_dir`: a lone sidecar counts as empty and goes under its own lock right before the rmdir), so every process locks the same inode; the kernel drops a crashed holder's lock; every reap re-checks the slot, renames the record to a tomb and unlinks the tomb while holding this lock, so a live record swapped into the slot after a remover's judgement is never removed and the slot is free the moment the rename lands; creators never take it (`O_EXCL` only ever creates into an empty slot); a process removing a record of its own (a release, the cleanup of a failed exclusive create) locks a sidecar a reap minted and never mints one, so no sidecar ever appears in a directory whose records only come and go with their owners (the semantic coordination namespace in normal use, teach targets); a lock held past 2 s makes the remover give up for this pass; a foreign-owned or non-regular sidecar, or a store that cannot lock, runs the tomb protocol below lock-less | kernel mutex (removal serialization) |
| `.<name>.owner-reap-<pid>-<hex>` | `py/ownerfile.py`; `crates/agrep-cli/src/index_lock.rs` | byte copy of the record being removed | every removal renames the record here (under the removal mutex where supported), unlinks the tomb once it proves to be the expected record, and otherwise links it back only into a vacant slot (a record that published into the gap is never overwritten; an unprovable tomb is left as litter); a freshness-daemon owner's tomb (`..indexd[.v{P}].lock.owner-reap-*`), which nothing reads back, is swept with the ingest staging temps once its reaper pid is dead; a holder whose record ends up in a tomb learns it at `verify()`/release | transient (reap tombstone) |

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
derived proofs; `.source_snapshot.seal` follows it, naming the identity of the
messages.jsonl it inventories. The publication guard consults the snapshot as
that generation's inventory only while no publication is pending or the seal
names the published messages.jsonl; the all-hit shortcut, having proven the
snapshot current, re-seals it. With neither, and no decodable cache to stand in,
a never-published unreadable scope refuses publication until it is readable:
held rows the snapshot never listed may lie under it. A first-use reader accepts
the generation only when the pending marker is absent and the nonempty regular
snapshot's identity and metadata stay stable across the coupled
derived-publication health check.

The parse cache is a base plus a journal of frames; every commit records the
journal position it established in `.ingest_cache.bin.head` (base instance and
frame sequence; compaction folds frames but keeps counting). A later decode
that replays that instance short of the recorded sequence has lost frames, and
with them rows a generation may have published: it is refused as a stale
journal in every lane and the cache rebuilt from sources, exactly as a missing
cache is. A base rewrite starts a new instance, which the head of an older one
never judges. Independently of the cache, every lane refuses to publish while
the published generation positively shows rows under an unreadable scope that
this pass neither serves nor saw go (`MaterialVerdict::Drops`), so a cache
older than the rows cannot license dropping them.

Family metadata stays staged while events and cache are updated. It is
published with the derived proofs immediately before `.ingest.sig`. Family
readers hold a read-only SQLite transaction and distinguish a moving marker
handoff from missing family data. Segmented semantic readers may serve the
previous verified generation only when its ingest-signature hash agrees with
the committed publication; missing proofs and mismatched signatures remain
unavailable. Recovery can read the published root packet without waiting for
the ingest lock.

The tool-event store uses SQLite rollback journaling (`DELETE`) rather than
WAL so read-only and sandboxed callers can open its committed main file
directly. During a normal write, a direct reader sees the prior committed
generation while the non-hot `-journal` exists. If a writer is interrupted
mid-commit, the journal may be hot and read-only SQLite callers cannot use the
store; they report unavailable events until the next writable ingest pass rolls
the journal back. Waiting alone never clears it: a background indexer runs that
pass from its health check (and a read that hits the journal asks for one), and
without a background indexer it is an explicit `agrep index`. There is
deliberately no reader-side workaround. A journal counts as interrupted only
while no live process holds the index lock, which every writer of the event
store and `corpus.db` holds; it is hot, as SQLite rules, when its first byte is
non-zero. `agrep doctor` and `agrep status` name a hot one on the event store,
and any one on `corpus.db` (searches keep serving its last complete update),
with `agrep index` as the fix; a journal under a live pass is that pass
writing, never another agrep version. `agrep audit` reports a `corpus.db`
journal left that way as a coverage gap (exit 1) naming `agrep index`, not as
live evidence.

A sidecar of either store that this user cannot both read and write, and that
no pass can clean up by itself, is not lost ownership. That is an unreadable
`-journal`, a hot one this user cannot write, a `-wal` (for the event store a
non-empty one, or any beside a WAL-mode header), a `-shm` beside a live WAL, or
an unreadable `corpus.db-shm` (the Rust ownership probe reads it). A readable
inert journal (debris the writer discards), an unused `-shm` the event-store
writer removes and its empty `-wal` beside a DELETE-mode header never block,
and a sidecar that cannot be opened never makes ownership uncertain: its
metadata still brackets the family. While one stands, every writer waits: a
daemon stays up and heartbeats its lock but launches no pass (indexd.log says
why once), a repair request starts none, and `agrep index` and `agrep reindex`
decline naming it. `agrep doctor`, `agrep status` and `agrep audit` name the
file and the fix - `sudo chown $USER <path>` for another user's file, `chmod
u+rw <path>` for one's own, or the flag and ACL commands when its mode already
allows access - then `agrep index`, or updating agrep while a newer agrep owns
the data dir; the freshness row and the search, `around` and `recall` footers
name the block instead of promising a pass. A sidecar is never opened to decide
this beyond a journal's first byte, which SQLite never locks: closing a probe
descriptor on a locked file would drop SQLite's own locks on it.

The writer keeps readers reading. Its 32 MiB page cache holds a typical pass's
changed payloads (the largest real session payload is about 25 MB), so it takes
EXCLUSIVE only at its commit. A write larger than that cache spills, and readers
are locked out from the spill until the commit. The one-time 0.4.2-to-0.4.3
upgrade pass rewrites nearly every session and holds readers out for about 12 s
at real-history scale; a repair pass that rewrites a large payload can do the
same for several seconds. Readers in that window report tool events
unavailable, and every surface that shows them says so: `around` (which serves a
tool-event handle's turn rather than calling the handle stale), `recall`, keyword
search in the transcript scan (which serves prose only), `summary` and the
explorer. Every busy wait of one write attempt (a spill or the commit) shares a
1 s budget, during which the waiting writer's PENDING lock keeps new readers
out; past it the attempt rolls back, releasing PENDING, and retries after a
backoff of at most 1 s. All attempts stop at a 30 s deadline (plus one attempt's
own work); the pass then fails with the store as it was, and the next pass
repairs it. Derived files are staged before the event write and renamed only
once its proof is complete. A pass that fails before the event commit (the
deadline above, the common case) or on a directory in the way of a derived file
leaves the published derived files and their proof as they were, and readers
keep serving them. A pass that fails after the commit (in the settle, the
marker or the proof, or on a derived rename, such as a Windows reader holding
`messages.jsonl` past the rename's retries) can leave event_stats.json, which
the proof covers, and any derived file renamed before the failure ahead of their
proof; direct readers then refuse until the next pass publishes one.

A passing writer leaves the same gap briefly: from the event_stats.json
rewrite to the proof and `.ingest.sig` publication (about 0.1-0.2 s per pass at
real-history scale). Direct transcript readers wait up to 1 s for a proof that
covers the published files before they report the generation unverifiable.

Readers wait out a commit's page writes and its marker publication (about 1 s
and 0.2 s at most) rather than reporting events unavailable. A marker still
behind the database after a whole wait (its writer died between the commit and
the marker) stays behind until the next pass, so later lookups against the same
store and marker report unavailable at once instead of waiting again. Long-lived
Python processes close their cached reader after 2 s idle; a fork waits for any
reader close in progress, and the child starts with no cached reader. On
Windows a held handle blocks the corrupt-store rebuild's rename, which retries
for about 37 s, then fails disclosed with the store unproven, so the next pass
rebuilds it.

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

## Source deletion and unavailability

Deletion is proven per store shape, never inferred from one absent walk of a
Stat root: a file NotFound beneath directories proven real is deleted, a root
listing nothing converges only after two consecutive complete, issue-free
preflights agree (`.source_absence_pending`), and an issue under a root blocks
that root's proof without vetoing its siblings. Whole-store (cline, gemini,
kimi, antigravity, pi) and token (crush, cursor) adapters hold one snapshot or
one conversation set per adapter: when one of several roots or databases
disappears while a sibling still reads complete, the removed root's rows go on
the next pass in every lane and return, reparsed, with the root. Nothing wedges
or keeps those rows forever; a temporary unmount of one such root costs its rows
for the unmount's duration only (`crates/agrep-cli/tests/multi_root_removal.rs`).
Absence is judged by directory shape, not mount identity: a mount gone from a
real directory leaves it empty and real, so for a Stat root the two-preflight
rule converges that as a deletion, and for the other shapes the next pass
does. The rows return, reparsed, when the mount does. Device numbers are not
recorded because they change across reboots and remounts on the same data, so a
device check would turn a legitimately emptied, remounted root into rows kept
until `--full`.

The stat cache (POSIX; Windows uses change-journal identity) re-verifies a file
next run when its stamp falls in `(scan start - margin, now + max(margin, 2 s)]`
on the stamping filesystem's clock, where `margin` is the stamp granule
(1 ns up to 1 s; 2 s for whole-second stamps) plus the local coarse-clock tick
(Linux `CLOCK_REALTIME_COARSE`, 0 on Apple, 20 ms elsewhere). A remote server
clock more than 2 s ahead of this machine, or behind it by more than the margin,
can stamp a write made during the scan outside that window; if a same-size
rewrite then lands in the same stamp granule, the cached rows stay stale until
the file's stamp, size or inode changes, or `agrep index --full` re-reads it.
No content digest closes this: distinguishing such a rewrite needs every remote
file hashed on every run, which quiet stores must not pay.
