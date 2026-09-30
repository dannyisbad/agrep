# Changelog

## 0.3.2 — 2026-09-29

### Agent coverage

- The omp/pi advisor sidecar's own voice is indexed. Advisor streams hold
  synthetic transcript mirrors (skipped as sidechain duplicates) plus the
  advisor's assistant records; because assistant text previously attached
  only as a reply to the preceding user row, and every user row in an
  advisor stream is a skipped mirror, the advisor's entire analysis was
  unsearchable. Text-bearing advisor messages are now their own side-stream
  rows under the watched session's family; pure-thinking records and the
  mirrors still index nothing. On one real 71k-line sidecar: 2,542 advisory
  rows surface, 8,172 mirrors stay skipped.
- pi/omp sessions whose header id changed keep their filename id as an
  alias. Both ids resolve to the same chat for caller self-exclusion,
  `postcompact` and session-handle lookup, and side chats rejoin that family
  instead of staying attached to an orphaned filename id. Ambiguous aliases
  are ignored with a warning; result handles still verify their turn,
  content digest and tool event before serving anything.
- Codex Desktop's human submissions are searchable. The adapter recognizes
  `item_completed` events carrying `UserMessage` items as well as the CLI's
  older submission log, so Desktop turns no longer disappear for lack of a
  legacy `user_message` event. Desktop evidence must match the response's
  thread, turn and text; routed copies and injected input still do not
  count as human prose. Existing parse caches are reparsed.
- Claude sessions under a repository container such as
  `~/Desktop/projects/<repo>` or a macOS temp root (`/private/tmp/<repo>`,
  `$TMPDIR/<repo>`) are labelled by the repository (`shop`), not the
  container (`projects`, `private`), matching the other adapters, also on a
  machine whose own home directory is named `home` or `users`. Takes effect
  after `agrep reindex --full`.

### Search

- `agrep resume` accepts every reference agrep prints: result handles
  (`@01a06003:4.82c5`, with or without a `~event:lo-hi` suffix), turn ranges,
  bare or `@`-prefixed ids, full uuids and `ses_` ids - plus a project name or
  a fragment of a chat's first line; a hex fragment missing its leading
  characters still resolves when it is unique. Several matches open a picker
  on a terminal and are listed otherwise, and an ambiguous reference never
  launches an agent. Only an id prefix resolved before, although
  `resume --help` promised search-hit handles.
- `agrep around <session>` without a turn opens that chat's latest indexed
  turn, exactly like `agrep around @<session>`, instead of refusing with
  `need a turn`.
- A refused flag combination prints the command to run instead:
  `agrep around: --max-chars cannot be combined with --full, which uncaps
  indexed message text; run: agrep around @x:174 -C 0 --full`, across
  around, search, recall, pack, tail and doctor. Search corrections always
  spell `agrep search`, so a query word that names a command cannot change
  what runs.
- `--project` and `--exclude-project` (search, recall, pack, chats) match a
  chat's stored project label exactly, or its last path segment,
  case-insensitively, instead of as a substring: `--project shop` reaches a
  pi label of `~/projects/shop` and a claude label of `shop`, and no longer
  admits `shop-admin`. A `*` or `?` in the value makes it a glob
  (`--project 'shop*'`). One predicate (`surface_policy.project_label_matches`)
  backs the SQL lane, the JSONL scans, the semantic metadata filter, the
  zero-coverage disclosure and the `chats` identity filter. `recall`/`pack`
  gain `--exclude-project`.
- `--here` on search, recall, pack and chats is `--project <basename of the
  current directory>`; mutually exclusive with `--project`, refused at a
  filesystem root (including Windows drive and UNC roots); continuation and
  larger-page commands re-spell it as the resolved `--project=`.
- `--no-side` on search and recall hides side-chat sessions using the same
  hidden set `chats` builds, so `agrep <q> -l --no-side` lists no
  `[side chat]` rows. `chats --no-side` explicitly selects its default
  visibility instead of failing with `unrecognized arguments`, and is
  mutually exclusive with `--side`. `--no-who subagent` remains a
  speaker-row filter and its help no longer claims to hide side chats;
  `--all-side-chats` is documented as the semantic ranking-slot switch.
- `agrep chats` gains `--project`, `--exclude-project`, `--here`, `--since`,
  `--until`, `--self` and `--no-self`; the scope applies to the identity
  listing and the content lane, and the larger-page command carries every
  flag. In an agent shell the calling chat's live-window rows are dropped
  (the previous branch never fired), counted, and disclosed on stderr; kept
  rows from the caller's family carry `~self`. `chats --json` content rows
  carry `score`, `matched`, `who` and `match_ts`, so "best match first" is
  explainable from the output. The human age column shows the matched
  turn's age when a pattern was given; equal bands tie-break newest first.
  `chats --help` and the top-level help describe the lookup (adjacent phrase
  first, then all words anywhere, best turn per chat, top max(20, 2N) chats
  content-ranked). `AGREP_TIMING=1 agrep chats ...` attributes
  identity-index build, content query and render as separate laps.
- Session views (`-l --sort score` and content-ranked `chats`) pick and
  order chat heads by a lane-folded score (phrase x1.0, all-terms x0.7,
  content fallback x0.5) instead of lane-first, so a strong human prose hit
  represents a chat ahead of a weak phrase echo inside tool output.
  `-l --sort time` keeps recency order and represents each chat by its
  newest matching turn. Row search keeps its structural phrase-first order.
  Exposed as `run_query(session_view_rank=True)`.
- `-l` rows end with the head row's age, so `--sort time` is readable from
  the terminal. The piped `-l` footer no longer splices a row count into the
  chat sentence: it reads `showing 8 of 290 chats (941 matching rows are
  tool output)`; row-unit footers keep `(N of them in tool output)`;
  `--json` completeness carries the same number as `tool_rows`.
- Multi-word keyword search folds minimal singular/plural spellings (`s`,
  `es`, `ies`) in the all-terms lane, so `don calls` finds a chat that said
  `before this call`, and `tries` also finds `try`. The original spelling
  is always kept, single-word queries and the adjacent-phrase lane never
  fold, short and non-alphabetic tokens stay exact, and a token whose
  Unicode-normalized form differs (`Straße`, fullwidth or ligature
  spellings) keeps matching its literal spelling.
- All-terms scoring measures term proximity from the same best-aligned
  occurrence the boundary factor grades, scaled by that occurrence's edge
  quality. A word fragment next to another query term (`calls dont` for
  `don calls`) no longer earns a near-phrase bonus; the row keeps the 0.5
  proximity floor. Snippets anchor on the aligned occurrence rather than the
  first substring, in every engine: single-token snippets from the SQL lane,
  the JSONL scan and the Rust fallback scanner now pick the same occurrence.
  The Rust scanner and the Python scorer agree via the updated exact-score
  conformance fixture.
- The term-coverage retry reaches the pages it exists for. Search offers it on
  an empty or query-echo-only page without first proving the query holds
  filler words (it still needs a whitespace-shaped query of at least five
  distinct terms, and a page with weak lexical hits still needs narration
  evidence), and after an automatic meaning-only page. `recall` offers it on
  an empty prose page unless `--lexical` is set, and after a pack holding only
  meaning rows, weak scatter or echoes. Explicit semantic and lexical modes
  never trigger it. Its candidates respect `--exclude-project` inside the
  ranked scan itself, which they previously bypassed. Concise recall keeps the
  best recovered row with its measured term coverage and a command for the
  full coverage page, instead of replacing evidence it already retrieved with
  a request to search again, and never spends that slot on the caller's own
  `~self` tool rows. Generated `--coverage` commands keep the original scope
  and filters. Recovered rows stay a separate stderr block, outside the pack,
  counts and exit status.
- `recall --probe` no longer takes the caller's own words as confident past
  context. A phrase found only in the caller's command input - including
  relay messages and search arguments in `~self` tool rows - or in caller
  prose quoting a multi-term query cannot supply the pointer, and such
  echoes no longer suppress the tool or meaning fallbacks. The check uses
  the exact tool event and the active keyword, word or regex matcher; a
  match also present in retained tool output stays eligible, and a
  rejected echo gives way to the next eligible row of the same chat or, when
  results keep one chat per family, of a sibling chat in that family. Ordinary
  recall rows and explicit handle reads are unchanged.
- Compact query-echo demotion checks the actual result row, including its
  timestamp, content digest and tool-event identity when available. It no
  longer borrows the first row sharing a session, turn and speaker, which
  could mistake genuine output for a neighbouring query echo or let an echo
  lead the page.
- Regex search (`-E`) applies `AGREP_REGEX_TIMEOUT_S` to each regex
  operation, not the whole search: a large scan of cheap matches finishes,
  while a pathological match or highlight is still stopped in the isolated
  worker. The refusal says `a regex operation exceeded` the limit and
  suggests simplifying the pattern or raising the per-operation limit.
  Indexed scans narrow candidates with every required literal run instead
  of only the longest one.
- Copyable commands on POSIX shells print result handles bare
  (`agrep around @01a046db:10.2685~a8f7...:214-222`) instead of
  single-quoted; a `~` after a digit never tilde-expands. Matches the
  Windows renderer.
- `agrep around` gains `--whole` (also `-C all`): the entire chat, root prose,
  the usual per-message cap; `--whole --who user` is the compact transcript.
  Every `[+N chars - ...]` cap marker in `around` and `recall` points at the
  lever that lifts it, `agrep around <session> <turn> -C 0 --max-chars 0`
  (plus `--who <role>` for a recap, control, harness, synthetic or subagent
  message), instead of the forensic `--full` stream, and a tight `--budget`
  shrinks prose before it would ever cut that command; the tool-collapse
  pointer keeps `--full`. Under `--who`, the scope line points at the same
  read without `--who` rather than at `--full`.
- Recall headers keep the result handle, agent, project, age and meaning or
  provenance marks without repeating numeric scores and an `around` command
  on every row. Probe pointers and misses are shorter, and `around` prints
  omission counts and a widening command only when something is hidden or
  the conversation role needs disclosure, rather than adding a scope
  preamble to an ordinary whole-chat or latest-tail read.
- Search and recall misses describe the indexed snapshot, not an index
  current to the millisecond. A verified snapshot stays usable during a
  healthy background refresh, and ordinary semantic misses tolerate
  live-update lag while both embedding and accelerator coverage reach 99%;
  the allowance scales with the corpus rather than stopping at 64 rows.
  Such misses exit 1 without a catching-up warning. Larger or unknown gaps,
  failed integrity checks and `--no-auto` misses still exit 2, and exact
  counts still require complete coverage.
- Keyword search can serve a verified committed JSONL snapshot while an
  ownership adoption or indexing pass holds the writer lock and SQLite is
  unavailable; the lock alone no longer forces a publication wait. Missing,
  moving or damaged snapshots still get a bounded retry, and after one
  second the error says `no verified transcript snapshot became available
  within 1s`.
- The family index no longer degrades silently. While a store drifted past
  the published `family_stamp`, `-l`/search/chats rows lost `[side chat]`
  marks and printed full 36-char ids. Display readers now serve the last
  published family index and print one line (`family index behind:
  side-chat marks and short handles may lag`). Query-time family expansion
  stays generation-bound; compaction recovery may serve a coherent prior
  publication as a partial packet with an `index_freshness` disclosure.

### Semantic search

- Semantic search chunks long rows instead of embedding only their opening
  bytes. The embedder truncates at its model window, so a multi-megabyte
  row (a compaction recap, a giant paste) used to embed as its first ~4KB
  — for pi/omp recaps that is a fixed harness preamble, making every
  mega-recap score like generic instructions against unrelated queries and
  surface as a top hit. Rows beyond one window now embed as capped,
  overlapping `#cN` chunk vectors (head chunk keeps the unsuffixed id, the
  `#r` reply convention extended). Windows are sized by a UTF-8 byte bound
  the byte-level tokenizer can never exceed, so dense code, JSON, CJK and
  emoji text never fall between chunks. pi/omp recap rows additionally skip
  the structural resume-instruction preamble so their vectors carry
  content; Claude and Codex summaries embed unmodified.
  Query-side, chunk hits max-pool back to their logical row, which appears
  at most once in results. Historical long rows upgrade on
  `agrep reindex --full`.
- Meaning rows no longer displace exact-phrase hits on a weak cousin's
  say-so. In the automatic hybrid merge a semantic row counted as
  corroborated when any lexical row shared its conversation family,
  including scatter scoring 0.26, and every meaning row up to that one was
  emitted first; under `--project` that pushed both exact-phrase hits off a
  three-row page. Corroboration now requires a strong lexical row in the
  family whenever the lexical lane has one.
- Semantic rows are anchored on the query's content words. A row carrying
  less than half of the query's content-term weight is labeled a weak
  meaning match at any cosine and sorts after confident meaning rows,
  because one shared noun buys a strong-band score on its own (`never
  promise a login` scored 0.86 against `hello? login isnt working`). When
  complete corpus frequencies are available for a bounded multi-term query,
  rarer terms weigh more; otherwise terms count equally.
- Semantic score bands have a committed calibration baseline.
  `bench/semantic_calibration.py` with
  `bench/fixtures/semantic_calibration.json` measures unrelated, same-topic,
  shared-noun and paraphrase cosine levels on the pinned model and reports
  where the floor (0.82) and strong (0.84) bands sit against them; the
  numbers are recorded in `bench/SEMANTIC_SCALE.md`. The bands themselves
  are unchanged pending a rerun of the private 20-task fixture. Measured
  int8 batch-padding drift (up to 0.021 on a row's score) is documented
  beside the bands.
- Meaning search can use its last verified segmented index while the next
  ingest publishes, instead of treating the marker handoff as an
  unavailable lane. The saved index must match the committed ingest
  signature; missing proofs and mismatched signatures still refuse the
  read. These answers are marked as published coverage, and recovery no
  longer waits for an active writer when that snapshot is already
  queryable.
- An unavailable optional meaning lane no longer turns a completed keyword
  search into an error exit: search discloses `meaning unavailable;
  keyword-only` and keeps the keyword result's exit status. A recall/probe
  miss whose requested meaning lane never ran still exits 2.
- The keyword-only fallback says why. An automatic meaning-lane exception is
  reported instead of dropped, with a bounded, terminal-safe message for
  unknown causes; permission failures no longer blame a sandbox or
  prescribe an unsandboxed shell. A lane that answered with partial
  coverage reads `meaning coverage is partial`, not keyword-only, and probe
  miss classification no longer calls an incomplete answer an unavailable
  lane.
- Semantic and hybrid result totals use the same completeness checks as
  their absence verdicts: an answered lane with unknown completeness, a
  rejected generation or untrusted rows can no longer claim exact totals
  because another status field says complete, and hybrid results keep an
  inexact keyword total inexact.
- Bounded meaning-lane notices keep the beginning and end of a long
  diagnostic, cutting at word boundaries so a trailing recovery command
  survives. An unvalidated meaning index says `meaning index generation is
  not validated; retry with agrep -s` instead of leaving the refusal
  without a next step.
- Recall `--json` reports `matched: "semantic"` on semantic-only hits
  instead of the `"phrase"` default; `sem_score` remains the cosine and
  `score` the display prior.
- Forced meaning search (`-s`) can no longer wait forever in silence: when
  the resident semantic worker is unreachable the read-only local pass is
  bounded at 30 s, and after 2 s stderr names what it is waiting on.
  Waiting on another process's model download discloses the holder pid and
  the bound after 2 s.
- The semantic worker warms the store's embedding model before accepting
  requests, instead of spending the first query's short deadline loading it
  and being retired for the timeout. A launch that outlasts discovery keeps
  its startup claim while the child is still booting.
- Empty semantic coordination namespaces under `/tmp` are reaped. Every
  sandboxed data dir mints `agrep-semantic-v1-<uid>-<digest>/` and nothing
  deleted it (286 on one development box); a starting worker now removes
  sibling namespaces that hold no record and were last touched over an hour
  ago, never a non-empty or foreign-owned one.

### Post-compact recovery

- `agrep postcompact` leaves indexing to the freshness daemon. It requests
  a background source and search-database refresh - also when the daemon
  is already running - and waits up to eight seconds for that request's
  completion receipt, never running a foreground ingest that competes with
  the daemon for the ingest lock. A live daemon or unchanged family
  metadata no longer makes an older recap snapshot look fresh: recovery
  checks the full database source stamp. Once a completed refresh shows no
  boundary it refuses at once instead of retrying to the bound, and when
  the daemon's published coverage already vouches for the caller's
  transcript at its current size and mtime the refusal returns without
  waiting for the refresh; every evidence shortfall still waits for fresh
  proof, so absence stays verified absence. An unfinished refresh leaves an
  available committed packet marked partial, and a packet produced while a
  source stays unreadable remains partial with the source-health notice.
  The daemon acknowledges every clean refresh, reaps requests left by dead
  or recycled clients, and the pending request and any completion receipt
  are released on exit.
- Pi/OMP recovery names the compaction that triggered it:
  `agrep postcompact --boundary-ms <timestamp>` selects that exact recap,
  never an older or later one, and can serve it straight from a verified
  committed generation without waiting for a refresh (coverage reports
  `index_freshness: indexed-snapshot`). An unrelated source failure no
  longer blocks an already verified packet, and a partial fallback never
  substitutes a different recap. If the transcript flush misses the first
  refresh, another is requested within the same deadline; a missing target
  stays `boundary_pending`, and a missing session is reported as absent
  from the published snapshot, not as proof its source never compacted.
  Manual calls without a timestamp still select the newest recap. `agrep
  setup` recognizes the previously shipped recovery extension and upgrades
  it to the timestamp-scoped commands; edited extensions stay untouched.

### Caller identity and self-exclusion

- Caller identity reaches tool shells. Under oh-my-pi, `agrep
  search`/`recall` run from tool shells never knew which session was
  calling: omp's tool shells inherit a one-time environment snapshot, so the
  extension's `AGREP_PI_SESSION_ID` export never reached them, and every
  search returned the current conversation's own rows ranked first,
  silently. The pi/omp extension now also publishes `{pid, sessions[]}` for
  its process to `/tmp/agrep-caller-v1-<uid>/<pid>.json` (0600, atomic,
  deleted when the last session leaves) and agrep resolves the caller by
  walking its own parent chain; a record with a recycled pid is refused.
  Several sessions in one process (root, advisor, subagents) share one
  record instead of overwriting one env var. The publisher refuses
  symlinked or foreign-owned publication directories and tightens an owned
  one to 0700 before writing; the reader accepts only an owned 0700
  directory, so a directory pre-created by another user under `/tmp` never
  receives session ids. The parent-chain walk stops at a parent younger
  than its child, so a recycled Windows pid never makes an ordinary
  terminal adopt an unrelated agent session.
- Never-compacted sessions get a live window. Automatic self-exclusion
  required an indexed compaction recap, so it did nothing for ~95% of
  sessions even with a known caller. A resolved caller with no recap row is
  windowed from turn 0; a malformed recap row still fails open. `--self`
  help and the codex/Claude compaction payloads state that labeling depends
  on identifying the caller.
- Automatic self-exclusion covers the caller's descendants: chats spawned
  at or after the caller's latest recap are hidden (all of them for a
  never-compacted caller), older descendants stay searchable as `~self`, and
  a child caller never hides its parents or siblings. Previously only the
  caller's own turns were windowed, so freshly spawned subagents echoed the
  same live context. Exclusions already supplied by `--no-side` are kept,
  and meaning search accepts more than four excluded sessions instead of
  rejecting the query as `invalid semantic filter`.
- An agent shell with an unknown caller is disclosed: search and recall
  prose surfaces print one stderr line (`agent shell, caller unknown: ...`,
  or the identity-conflict variant) instead of failing open silently.
  `--no-self`, `--json` and `--self` renders are unchanged.

### Status, doctor and setup

- Every invocation starts faster: the CLI imports the index runtime and
  installer metadata only for commands that need them (import chain 37%
  faster; `--version` 13% and `search` 12% faster end to end on the
  reference Mac). `agrep --help` no longer exits 1 when the runtime manifest
  is unreadable.
- `agrep status` explains a stale wheel install: a package with no PEP 610
  local-source provenance is compared against the checkout named by
  `AGREP_SOURCE_DIR`; a lagging result lists the checkout's unreleased
  CHANGELOG entries beside the replace remedy, and a content-only
  comparison says the install `differs from the local checkout` rather than
  asserting it is older. Running from a checkout renders its own
  `source checkout` row.
- The `embedding lane` row on `agrep status`/`doctor` is informational
  (`[-- ]`) while the lane that built the store opens on this machine; it
  warns only when a metal store cannot open here. Every healthy box used to
  carry a permanent `[!! ]` for a lane fact. A CPU-built store on a Mac with
  Metal installed points to `agrep doctor --deep` to confirm the GPU lane
  opens instead of recommending a full rebuild on installation evidence
  alone, and setup and status stop presenting one benchmark host's GPU
  speedup as a property of this machine.
- `agrep doctor --deep` no longer aborts (SIGABRT) on a Mac without a Metal
  device: `mlx_embed.available()` imports `mlx.core` once per process and
  remembers the answer, so doctor's second capability check never re-runs
  the extension init that nanobind refuses. `--no-semantic` is accepted
  with `--deep` and keeps the semantic tier at routine depth, and
  `agrep doctor --deep --fix --no-semantic` now also skips the semantic
  model prefetch (`semantic tier untouched (--no-semantic).`) instead of
  downloading the model after the report.
- `agrep status` reuses the ingest binary identity recorded by the last
  writable indexing pass while the resolved binary's file identity still
  matches, instead of probing the binary again; writers still verify the
  bytes themselves. When the identity cannot be verified, status defers its
  daemon-compatibility and database-readiness verdicts and `doctor` reports
  the database `not verified` (`writer-identity-unavailable`) instead of
  treating a missing local identity as proof that another installation owns
  the database or that it is unreadable.
- `agrep doctor` no longer recommends installing Rust when an ingest binary
  is already available; that advice is reserved for a missing binary on a
  machine without the toolchain.
- `agrep setup` names the tiers its routine probe did not verify and points
  to `agrep doctor --deep`; its `tiers now` line previously listed only
  proven tiers, leaving unrun checks indistinguishable from unavailable
  capabilities.
- Unexpected CLI failures keep their exception class and message without
  `AGREP_DEBUG`. Every unclassified failure used to become `agrep hit an
  unexpected error` with a generic `doctor` command, hiding the cause;
  known failures keep their specific remedies.
- `agrep remove` waits for semantic workers that are still starting before
  reporting a successful teardown. A spawned child could outlive its launch
  claim without registering an owner, leaving a process and an open log
  handle behind; startup handoffs now stay tracked until ownership or exit,
  and an unsettled child blocks removal instead of being overlooked.

### Teaching your agents

- Instruction block v38 speaks to every agent in the second person (the
  per-agent name slots are gone, so each non-codex target receives
  identical bytes) and teaches the cross-project index: read the project and
  age on every `chats` and `-l` row, scope with `--project`/`--here`/
  `--since`, hide side chats with `--no-side`, treat meaning-match rows as
  leads and use `--lexical` for exact phrases, open whole chats with
  `around --whole`, and rely on the fixed self-echo behavior. Examples are
  generic. The codex block carries the same facts.
- `agrep setup` and the background reconcile tell an edited instruction
  block from a shipped one by hashing its body against the digests agrep
  has shipped (v37 onward). A same-version block whose text was edited is
  reported as `edited` in `teach-reconcile.json` and `agrep status` (kept
  as is, never rewritten in the background) instead of being classified
  clean; an explicit `agrep setup` that ships a newer version prints
  `replacing an edited v37 block with v38 in <path>`. The status remedy says
  what a re-sync does.
- Setup guidance states that search works without agent instruction blocks.
  Headless setup and the enrollment reminder claimed agents could not use
  agrep until setup wrote their instructions; the blocks teach the tool's
  existence and use, they do not enable search.
- The uninstall sentinel waits five minutes, not twenty seconds, before it
  treats a missing `cli.py` as an uninstall. A reinstall that rebuilds from
  source (`uv tool install --reinstall --from .`, an upgrade without a
  matching wheel) keeps the file gone for a minute or more, and the
  sentinel stripped every agent's taught block in that window; `agrep
  setup` then re-added them as new blocks, so an edited block's disclosure
  was lost.
- Cleanup sentinels are scoped to the resolved data directory on macOS,
  Linux and Windows, including the Windows watcher mutex, so setting up or
  removing a second data root no longer overwrites or removes the first
  root's scheduler job. An old unscoped job is retired only when its
  registered command names this root's sentinel.

### Index integrity and ownership

- Native upgrades keep durable ownership while rebuilding an incompatible
  parse cache, so the successor daemon does not lose its ownership fence
  mid-reparse. Supported older caches are adopted with their last-good rows
  intact until source reads succeed, and an incompatible foreign cache is
  reconstructed in the same ingest instead of needing another invocation.
  Missing or unreadable sources still cannot replace the published
  generation, and an undecodable unowned cache is kept with takeover
  refused. Upgrading from 0.3.0 or 0.3.1 adopts their parse cache with its
  last-good rows and republishes their session-family metadata in the
  current format on the first ingest, even when no message changed.
- Indexing can restore a missing durable owner when both the owner anchor
  and the parse cache are gone but `corpus.db` still names the current
  writer; that state used to block the very ingest needed to repair it.
  Only the canonical Rust ingest may restore ownership, daemon and semantic
  writers stay fenced until it succeeds, and a foreign or unverifiable
  database owner does not qualify.
- Parser exclusions can remove previously cached rows and tool events: a
  complete empty reparse of an unchanged, verified source now replaces its
  old material instead of keeping excluded content searchable and warning
  about source health indefinitely. Changed or unverified sources and
  incomplete reads keep the last-good guard, and an empty Claude parse with
  malformed JSONL counts as a read failure, not a policy exclusion.
- Malformed cached message or reply rows keep invalidating fallback search
  results on every read. The first read recorded the damage, but a cached
  retry could forget it and claim an exact answer from the surviving rows.
- Family lookups and `postcompact` pin a read-only SQLite transaction while
  reading the published corpus, so an in-place writer cannot change pages
  beneath a recovery read. Family proof metadata lands with the ingest
  commit markers instead of ahead of the event and cache writes, and
  readers tell that handoff apart from missing family data.

### Performance

- Large-corpus searches do less repeated work without changing results:
  identical text and match spans share one native boundary-score
  calculation, ranking cutoffs are recomputed only when scored candidates
  change, and a failed query term stops checking the rest - including for
  `-l` and content-ranked `chats`. Meaning searches skip the corpus-wide
  term-frequency pass when every returned row contains all or none of the
  query's content terms, grouped native scans skip candidates that cannot
  enter a family's retained hits, and compatible query vectors use an AVX2
  dot-product path.

## 0.3.1 — 2026-08-26

- pi/oh-my-pi advisor sidecars mirror every transcript row as a synthetic
  user-role record; the parser classified those mirrors as genuine user
  prompts. One observed box inflated a 588k-record store into 24.9M user
  rows, an 11.6GB parse cache, and a 15.5GB search db. Synthetic mirrors
  are sidechain now: skipped with their tool events intact, never rows.
- An ingest-cache delta too large for one journal frame now rebuilds the
  cache base atomically instead of erroring. Previously a recovery-scale
  publication (say, adopting a full corpus) could never commit: the
  freshness daemon retried a doomed append every cycle, discarding minutes
  of work each time, forever, while searches silently served the stale
  snapshot.
- `AGREP_HOME` without `AGREP_DATA_DIR` now resolves the data dir inside
  the overridden home (disclosed as `agrep-home-isolated`). A sandboxed
  invocation could previously read a synthetic home while writing the
  production derived stores - observed live, clobbering a real corpus
  census down to one session.
- The freshness daemon stamps its resolved home and data dir into its
  owner lock. A live daemon watching a divergent world (for example one
  spawned with a leaked environment) is now named with both worlds and the
  remedy, is retirable, and blocks inline writers - instead of posing as a
  generic version conflict.
- Doctor cross-checks the published corpus census against the parse
  cache: a corpus that lost most of its sessions renders a `[!!]` line
  carrying both numbers and the `agrep reindex --full` remedy, never a
  green checkmark. The recall/search blocked-owner hedge escalates the
  same way, naming the loss magnitude instead of whispering "history may
  be stale".
- Queries never wait on an in-flight publication when a last-good snapshot
  exists: they pin the published generation and serve it immediately with
  the standard freshness disclosure. The old behavior could stall up to 4s
  chasing a moving generation on a busy box and then refuse with "rerun".
  The bounded wait survives only for a box publishing its first-ever
  searchable snapshot, capped at 1s.
- Release plumbing: sealed npm tarballs publish as local files, the publish
  step skips identities that already exist (idempotent across partial
  publishes), the empty-token registry config that suppressed OIDC trusted
  publishing is gone, and a manual recovery job can verify and backfill a
  partially published release from surviving original build artifacts
  without rebuilding anything.

## 0.3.0 — 2026-08-15

- `agrep setup` renders its five steps with colored boundaries and opens with
  a one-line step map. Headless runs and env-detected agent contexts never
  sit on a consent prompt: they get the full write disclosure plus explicit
  agent instructions strongly recommending `agrep setup --yes`, and the
  archive question is deferred rather than asked. Nothing consent-gated is
  written without `--yes`, as before.
- Global npm installs now run `agrep setup --yes --no-semantic --no-archive`
  from postinstall, so `npm install -g` ends enrolled with a built index;
  `AGREP_NO_AUTO_SETUP=1` restores warm-only, `AGREP_SKIP_POSTINSTALL=1`
  skips postinstall entirely. The model fetch still waits for first semantic
  use. The uninstall-cleanup sentinel ships with the auto-written blocks by
  design - it is the undo mechanism for exactly those writes - and the
  postinstall log and both READMEs name it.
- The Windows uninstall sentinel now arms without elevation: `schtasks /SC
  ONLOGON` demands administrator rights (observed live: every unelevated
  setup - including npm postinstall - printed "sentinel could not be armed"),
  so registration goes through `Register-ScheduledTask` with an own-user
  logon trigger, keeping schtasks as the fallback. Verified end to end on
  Windows 11 ARM64: arm, doctor "armed", `agrep remove` deletes the task.
- The Windows sentinel task now references uv's managed base interpreter
  instead of the tool-run venv shim: `uv cache clean` deletes the ephemeral
  venv (leaving the logon task dangling) but never the managed install. The
  watcher is stdlib-only, so the base interpreter suffices.
- Bare `agrep` now ends its status page with the five newest indexed chats
  and their copyable `agrep around` follow-ups - the "what was I working on"
  answer without learning a command first.
- Instruction block v37 routes session questions by their tense: running or
  active right now is `agrep board --once`; recent, last, or latest sessions
  is bare `agrep chats` (newest first). Agents measurably reached for the
  live board when asked about recent history.
- Live observation now covers pi and oh-my-pi: `agrep board` and `agrep tail`
  see running omp agents (sessions, subagents, tool activity) by tailing
  their append-mode JSONL stores, the same hook-free route as Claude. The
  `pi.live` registry capability is flipped to supported and `--agent omp`
  aliases to pi everywhere. Previously the board was blind to a box full of
  working omp agents.
- opencode 2.x is supported alongside 1.x: opencode 2 migrates its SQLite
  store in place (session_v2/session_message with inline content parts,
  replacing session/message/part), which made the source unreadable and -
  because an unreadable source retains the whole prior generation rather
  than silently deleting its chats - froze ALL agents' freshness on any box
  that upgraded opencode. Ingest and the live reader now detect the schema
  per database and read both; verified against a real opencode 2 store.
- pi and oh-my-pi sessions resume natively: `agrep resume <id>` runs
  `omp -r <id>` (or `pi -r`, whichever fork owns the session's store root)
  from the session's recorded working directory.
- One unreadable store no longer freezes the whole index. The fail-closed
  rule stays exactly where it protects data - a generation that would DROP a
  store's published chats is still refused - but every other shape degrades
  to disclosed per-source staleness: warm passes keep publishing the guarded
  snapshot's last-good rows, complete passes publish past a deterministic
  parse failure whose rows are served or provably absent, and a fresh box
  whose only store is unreadable publishes an empty disclosed generation
  instead of failing its first index build. Absence still requires the
  two-stable-observation protocol before any deletion. Pinned by three new
  torture tests (warm survival, fresh-box first build, and the retained
  deletion fence).
- `postcompact` now recovers on pi/oh-my-pi compacted resumes. Four fixes,
  each observed live on omp: staleness-shaped boundary misses retry on a
  short bounded schedule (one immediate ingest, then ~1s and ~3s later)
  because the hook can fire before the compacting agent flushes its boundary
  row to disk; a freshly-resumed session whose recap is turn 1 serves its
  pre-boundary tail from the family root, capped at the boundary timestamp
  and disclosed as `window_source: family_root`; a live freshness story
  no longer refuses a proven packet - after the retries it serves as an
  explicitly partial packet carrying the story, since the compacting
  session's own churn kept the index "behind" at exactly the moment the
  packet exists for; and when that churn starves the generation-stable
  snapshot open outright (each retry ingest advances the generation the
  daemon is also advancing), the final attempt serves the boundary from the
  last published snapshot as a partial packet naming the churn.
- When weak keyword hits print the "chats about this semantically" block, the
  header now counts the neighbors held below the similarity floor, and a dim
  `deeper: agrep recall '<query>'` pointer names the lane-fusing surface.
  Machine modes are unchanged.
- The `meaning unavailable; keyword-only` story lectures once per cause per
  ten-minute window: the first occurrence carries the cause and retry lever,
  repeats of the same cause render the bare line. The lane state itself is
  disclosed on every render, machine `semantic_status` is never dampened,
  and read-only data dirs always get the full story.

## 0.2.0 — 2026-08-12

0.1.x shipped multi-agent ingest, keyword search, and an optional semantic tier
that required torch and a running local server. 0.2.0 keeps the ingest
foundation, replaces the semantic stack with a pinned in-process embedder, and
adds the evidence path that lets an agent use its own history without a human
driving the CLI: `recall`, digest-checked handles, `postcompact`, and the
setup-installed instruction block. Indexing and search remain local and
read-only; transcript content is never uploaded.

### Agent coverage

- Four new adapters bring coverage from six agent tools to eleven: crush
  (including per-project databases named by its registry), Gemini CLI,
  Cursor, and pi, whose adapter also walks oh-my-pi's store, a fork sharing
  pi's format byte for byte, under the same `pi` label. The adapter ingests
  those stores' own compaction records as boundary evidence; setup's optional
  shared extension shapes future summaries and exports the exact live session
  identity. 0.1.1 covered Claude Code, Codex, opencode, Antigravity, Kimi CLI,
  and Cline.
- Live tailing covers Claude, Codex, opencode, Antigravity, and Cursor. Native
  `resume` covers Claude, Codex, opencode, and Antigravity.
- Copilot CLI and qwen-code are now detected and counted by `doctor`, but not
  yet parsed.

### Search

- Keyword search carries over from 0.1.x; the result surface is rebuilt. Default
  matching is documented and pinned by a ranking contract
  (`docs/SEARCH_RANKING.md`, new): multi-word queries bridge punctuation and
  underscores, an independent any-order lane keeps scattered evidence
  reachable, and boundary quality affects rank rather than eligibility, so short
  fragments still behave like grep.
- Exit codes now distinguish two different absences: a current, exhaustive
  keyword miss exits 1 like grep, while an unverified or stale absence exits 2
  rather than claiming nothing matched.
- Agent-driven shells get a byte-budgeted page of one-line hits with
  digest-checked `@session:turn.digest` handles that paste directly into
  `around`, `recall`, and `resume`. The digest is a content claim: a handle
  whose row moved is rescued and disclosed, or refused, instead of silently
  serving different text. Stable turn assignment at ingest remains future work
  (`docs/HANDLE_IDENTITY.md`). `--more <handle>` pages from a short-lived frozen
  top-40 snapshot; `--classic` or `AGREP_PROFILE=classic` restores the human
  renderer.
- Ranked surfaces treat a root chat and its spawned children as one conversation
  family, so one large agent swarm cannot consume the result page; literal grep
  stays exhaustive across side chats, and `--all-side-chats` expands siblings
  into independent slots.
- Tool calls and their output are indexed alongside prose and ranked below it,
  so command echoes do not bury the conversation about them
  (`agrep set tools off` for prose only).
- `agrep around` gained handle input, root-prose defaults that keep generic tool
  events out of an agent's attention path, and `--full` for the same-window
  forensic stream. New sibling commands: `recall` (prose recall),
  `pack` (several recall queries, deduped, one budget), and `chats` (indexed
  main-chat history).

### Semantic search

- The semantic stack is replaced outright. 0.1.1 needed torch,
  transformers, sentence-transformers, and scikit-learn from a separate
  `requirements.txt`, ran Qwen3-Embedding-0.6B, and served `--semantic` only
  while a local server was running. 0.2.0 runs a pinned 47M-parameter embedder
  (`granite-embedding-small-english-r2`, int8 ONNX) in-process behind a
  short-lived worker lease, with no server and no torch. That is the CPU lane,
  which runs everywhere; on Apple silicon the Metal lane below runs the same
  weights on the GPU.
- Dependencies are now numpy, onnxruntime, and tokenizers, installed
  automatically wherever upstream ships compatible wheels. Platforms without
  them stay core-only instead of failing.
- The model is fixed by revision and per-file SHA-256, so a byte of drift is
  rejected rather than served. Sequence length is part of the vector-space
  identity: changing it invalidates old rows even when the weights are equal.
- Recall combines keyword and meaning evidence as independent lanes rather than
  fusing scores. On 20 frozen developer-recall tasks written before anyone saw a
  result page, keyword-only produced 9/20 definite answers, semantic-only 14/20,
  and the shipped hybrid 19/20, at 1.58 mean CLI turns per definite answer.
- The similarity floor is calibrated (0.82 floor, 0.84 strong): weak
  nearest-neighbor tails stay silent instead of padding the page, and meaning
  rows are labeled as such. `-s` forces meaning-only, `--lexical` opts out.
- Embeddings maintain themselves newest-first in the background, capped at 128
  rows for the first searchable publication and then reporting partial coverage
  while history converges. Stale or mismatched vectors are never served;
  incomplete artifacts fall back to keyword results rather than blocking.
- Measured initial-embed throughput is 155.6 rows/s (128-row fixture in 822.7
  ms) at the shipped six threads. CoreML runs ~2.4x slower on this export and
  FP16 CPU buys ~14% for nearly double the model bytes; both were rejected.

### Metal (MLX) lane on Apple silicon

- New in 0.2.0: a GPU embedding lane for Apple silicon, measured at ~10.9x the
  ONNX int8 CPU lane in an interleaved A/B: 8.62s versus 0.79s for the same
  work. 0.1.1 had no Metal lane at all.
- It is the default where it can actually open: the base install carries mlx
  on supported Apple silicon, and the lane engages when the machine is idle
  enough to share the GPU. No extra, no environment variable. `AGREP_MLX=off` opts out, and `AGREP_MLX=on` pins the lane
  through load, for a foreground index the owner is already waiting on.
- The idle check runs once, when a store first picks a lane, never per batch. A
  machine too busy to share the GPU starts a CPU store rather than one that
  alternates engines by load.
- The two lanes are close but not identical. The same weights in fp16 agree
  with int8 CPU to ~0.999 cosine, enough to flip results near a threshold. Each
  store records the lane that built it and is never served by the other.
  Existing stores keep their recorded lane; `agrep reindex --full` is the one
  sanctioned lane move, discarding every row, re-deciding from the machine
  default, and riding the same background rebuild notice as any other identity
  change.
- A predicted lane that cannot open — unreachable weights, a parity refusal —
  lands a new store on CPU instead of erroring. A lane a store already recorded
  still fails loudly, because silently serving CPU vectors against Metal rows is
  the failure worth being noisy about.
- `agrep setup`'s doctor step prints the lane state on Apple silicon and
  recommends the extra when it is absent. The extra costs exactly one dependency
  (`mlx`), Apple-silicon marked because mlx ships no wheels elsewhere.
  Everywhere else, and without the extra, every path is the ONNX CPU lane.

### Post-compact recovery

- New: `agrep postcompact` serves the same session's proven pre-boundary turns,
  bounded, so an agent resumed from a lossy summary recovers the exact values
  and paths the summary dropped. `--session <id>` names the session when it
  cannot be resolved; `--json` emits one bounded packet.
- `agrep setup` can install three compaction-only integrations: Claude's
  `PreCompact`, which shapes the summary; Codex's compact-only `SessionStart`,
  which supplies context to the resumed agent; and a shared pi/oh-my-pi
  extension, which shapes their summaries, exports the exact live session
  identity, and queues recovery only on a compacted resume or retry. None fires
  on an ordinary user message. `--no-hook` skips them, `agrep remove` takes
  them out, they are never auto-repaired, and existing hooks and extensions are
  never overwritten. Removing the package without running `agrep remove` first
  can leave Claude's self-contained entry behind; the cleanup sentinel removes
  the package-dependent Codex hook and the exact pi/oh-my-pi extension copies
  it enrolled.

### Teaching your agents

- New: `agrep setup` writes a short recall block into each detected agent's
  instructions file. A local CLI is absent from an agent's context unless
  something puts it there, and this block is what puts it there. It is plain
  instructions text the agent reads like any other. Setup lists every detected
  target with its reason on one consent screen and asks before writing the
  batch; `--yes` accepts, `--no-teach` declines. `agrep remove` takes the
  blocks, the hooks, and the cleanup sentinel back out.
- There are two block texts, selected by the host filename: an `AGENTS.md` host
  gets the Codex-shaped variant, and every other file gets the default block,
  addressed to the agent whose file it is by name. The two shapes follow the
  prompting styles the vendors document: the default block carries short
  principles with the reason inline (per Anthropic's constitution and prompting
  research), the Codex block is goal/constraints structure plus one worked
  example (per OpenAI's lean-prompt guidance). Both document the core motions
  (`agrep recall` to find a prior moment, `agrep around <handle>` to open it
  at its source, `agrep postcompact` for this session's own pre-boundary tail,
  `agrep board --once` for live activity) and delegate the rest to
  `agrep --help`, which lists the options shipped with the installed version.
  Claims come from the opened source rather than from scores or snippets.
- 0.1.1 had no setup command; installation ended at the package, and the index
  built on first use.

### Performance

- Recall rendering: a 6,395-row window went from ~4.4s to 0.013s (326x) after
  the block fitter was made linear.
- Zero-hit search: 6.19s to 0.36s warm. The self-exclusion probe collapsed from
  walking a 1,146-member conversation family to a single query.
- Emoji and CJK queries: 7.9s to 1.5s, by scoping the Unicode LIKE escape to
  caseful tokens.
- Pathological queries: a 10,000-character repeated-token query took 33s and is
  now bounded by distinct token count, because conjunctive consumers dedupe the
  multiset.
- Clean install from zero: `agrep setup` completes in 3.1s and hands the
  embedding work to the background. The pinned model download is 52.1 MiB across
  three SHA-256-verified files, plus a 90.9 MiB checkpoint where the Metal lane
  engages.
- New committed performance board on isolated generated fixtures — a
  100,000-row search corpus and a 4,600-file / 14,000-row ingest store — holding
  selective engine queries to 120ms, broad 100,000-hit queries to 900ms,
  two-character lanes to 350ms, cold ingest to 3.5s, and a cold command through
  exit to 3.75s. Those committed budgets, not one machine's point measurements,
  are the release contract.

### Privacy and safety

- No telemetry, accounts, or transcript uploads. The index lives in a per-user
  data dir (`AGREP_DATA_DIR` overrides); model weights use a shared per-user
  cache (`AGREP_MODEL_DIR`) so isolated indexes do not duplicate them.
- Indexing, search, and archive capture are read-only over agent stores.
  `agrep restore` is the one command that writes back, and it refuses to
  overwrite a live file without `--force`.
- Keyword search needs no model and makes no transcript-bearing network
  request. The only network fetches are the pinned, checksum-verified model on a
  semantic install (`--no-semantic` skips it) and, on a degraded source install,
  the checksummed Rust binary.
- The explorer is now read-only by construction. `agrep ui` and `agrep serve`
  are authenticated loopback with no resume, command, raw-output, or mutation
  controls, and `agrep board` is observational for the same reason. An
  unreadable store yields an explicit partial result rather than an affirmative
  all-quiet one.

### Removed

- The 0.1.x "smart tier" and its heavy dependency set: topic and concept
  clustering, mood arcs, and Ollama-generated titles and summaries, along with
  `requirements.txt` (torch, transformers, sentence-transformers,
  scikit-learn) and the generated HTML report. They served browsing rather than
  retrieval, and cost more to install than the retrieval path they sat beside.
- The `agrep warm` command, which preloaded semantic models into the long-running
  server. There is no server to warm: the embedder loads under a worker lease
  and releases itself.

### Also in this release

- `agrep archive` keeps compressed, deduplicated snapshots of every store file
  indexed, so an agent's own retention window or a careless `rm` does not end
  the history. Plain files are stored byte-for-byte; SQLite stores are captured
  through SQLite's backup API as consistent database snapshots, because a raw
  copy of a live database can be torn mid-transaction. Off by default, opt-in at
  setup or with `archive --on`; `agrep restore` verifies the archived bytes
  against their pinned SHA-256 before writing, and its path argument accepts any
  substring of the archived path.
- `agrep board` is a new bounded live-activity window across agents, with side
  agents nested under their root; `board --once --json` is its deterministic
  snapshot for agents, distinguishing snapshot completeness from page
  truncation and exiting 2 with the exact retry argv when partial. `agrep tail`
  carries over from 0.1.x and its event shapes are now a supported interface.
- `agrep run <agent>` launches Claude, Codex, opencode, or Antigravity with
  liveness capture from process start. `agrep resume` carries over.
- `agrep doctor` is now a bounded health check, with `--deep` running expanded
  integrity, attribution, and archive checks and printing a remedy only for a
  proven gap; individual deep probes keep their own safety timeouts. New:
  `agrep audit` cross-checks adapter-discovered files against per-file intake
  accounting and independently line-counts the JSONL stores where a dumb recount
  is well defined (Claude, Codex, Kimi). `agrep status --json` is a bounded
  machine-readable index summary.
- Search, recall, and doctor JSON now carry `self_exclusion`, `freshness`, and
  `semantic_coverage`. Search JSON emits them once in a leading
  `{"kind":"agrep-meta", ...}` record, so an empty or unavailable index cannot
  hide its state from a machine caller.
- Index freshness moved off the long-running server onto a lightweight daemon
  that any search can start and that exits after inactivity. Corpus updates
  publish atomically, so a failed refresh leaves the previous good index
  available; `--no-auto` opts out and exits 2 rather than reporting an
  unverified absence.

## 0.1.1 — 2026-06-14

- Multi-agent ingest foundation: a Rust reader for six stores (Claude Code,
  Codex, opencode, Antigravity, Kimi CLI, Cline) normalizing to one row shape,
  plus a derived index for fast cold searches.
- Keyword search over that index, with `around` for the conversation surrounding
  a hit, `resume` to reopen a past session in its own agent, and `tail` for live
  agent events as JSON lines.
- A browser explorer (`agrep ui`) served from a local read-only server on
  127.0.0.1, which also refreshed the index in the background.
- An optional "smart tier" installed separately from `requirements.txt`
  (torch, transformers, sentence-transformers, scikit-learn) adding semantic
  search over Qwen3-Embedding-0.6B, topic clustering, and mood arcs. Meaning
  search required a running server; titles and summaries required a local
  Ollama model. The core install stayed stdlib-only.
- Packaging hardening: prebuilt wheels carrying the Rust ingest binary, so
  `uvx agrep`, `pipx run agrep`, and a global npm install worked without a
  clone or Cargo.
